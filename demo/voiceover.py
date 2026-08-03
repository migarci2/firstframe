#!/usr/bin/env python3
"""Genera la locucion del video con ElevenLabs, un fichero por tramo.

Lee los seis tramos de `demo/VOICEOVER.md` y saca un mp3 por tramo mas uno
concatenado, para que puedas montar el video pegando cada pista a su plano en vez
de pelearte con una locucion de tres minutos de una pieza.

    export ELEVENLABS_API_KEY=...           # ya esta en .env
    .venv/bin/python demo/voiceover.py --voice <VOICE_ID>
    .venv/bin/python demo/voiceover.py --voice <VOICE_ID> --only 3   # solo el tramo 3

La key del proyecto solo tiene permiso de text-to-speech: no puede listar voces ni
modelos, asi que el voice id se pasa a mano. `eleven_v3` verificado devolviendo
audio con esta key.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "demo" / "VOICEOVER.md"
OUT = ROOT / "demo" / "vo"
MODEL = "eleven_v3"

# Pausa entre tramos al concatenar: el montaje respira y no encadena frases.
GAP_SECONDS = 1.2


def tramos() -> list[tuple[str, str]]:
    """[(titulo, texto)] leyendo los encabezados `## N · ...` de VOICEOVER.md."""
    if not SCRIPT.is_file():
        sys.exit(f"falta {SCRIPT}")
    out, title, buf = [], None, []
    for line in SCRIPT.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if title and buf:
                out.append((title, " ".join(buf).strip()))
            title, buf = line[3:].strip(), []
        elif title and line.strip() and not line.startswith(("#", "---", ">", "**")):
            buf.append(line.strip())
    if title and buf:
        out.append((title, " ".join(buf).strip()))
    return out


def speak(text: str, voice: str, key: str, dest: Path) -> Path:
    body = json.dumps({
        "text": text,
        "model_id": MODEL,
        # Estable y sin dramatismo: es una demo tecnica, no un trailer.
        "voice_settings": {"stability": 0.55, "similarity_boost": 0.75, "style": 0.0},
    }).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        data=body, method="POST",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            dest.write_bytes(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"ElevenLabs {e.code}: {e.read()[:300].decode(errors='replace')}")
    return dest


def duration(p: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(p)],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", required=True, help="voice id de ElevenLabs")
    ap.add_argument("--only", type=int, help="generar solo ese tramo (1-6)")
    ap.add_argument("--no-concat", action="store_true")
    a = ap.parse_args()

    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not key:
        sys.exit("falta ELEVENLABS_API_KEY (esta en .env: set -a && . ./.env && set +a)")

    OUT.mkdir(parents=True, exist_ok=True)
    parts = tramos()
    if not parts:
        sys.exit("no encontre tramos en VOICEOVER.md")

    made, total = [], 0.0
    for i, (title, text) in enumerate(parts, 1):
        if a.only and i != a.only:
            continue
        dest = OUT / f"{i:02d}.mp3"
        speak(text, a.voice, key, dest)
        d = duration(dest)
        total += d
        made.append(dest)
        print(f"  {i}. {title[:44]:46} {d:5.1f}s  {len(text):4d} car  -> {dest.name}")

    if made and not a.no_concat and not a.only:
        # Silencio entre tramos: se genera aparte y se concatena, asi cada pista
        # sigue existiendo suelta para el montaje.
        gap = OUT / "_gap.mp3"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", f"anullsrc=r=44100:cl=mono", "-t", str(GAP_SECONDS),
                        str(gap)], check=True)
        listing = OUT / "_concat.txt"
        listing.write_text("".join(
            f"file '{p.as_posix()}'\nfile '{gap.as_posix()}'\n" for p in made))
        full = OUT / "voiceover.mp3"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat",
                        "-safe", "0", "-i", str(listing), "-c", "copy", str(full)],
                       check=True)
        print(f"\n  total hablado {total:5.1f}s  ·  con pausas {duration(full):5.1f}s"
              f"  ->  {full.relative_to(ROOT)}")
        if total > 175:
            print("  AVISO: pasas de 2:55 de voz sola. Recorta texto o el video no cabe en 3:00.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
