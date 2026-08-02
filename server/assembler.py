"""Assembler: mp4 de escena -> segmentos HLS -> B2, playlist regenerada por segmento.

Esta es LA pieza. El pipeline genera las escenas en secuencia (Genblaze es secuencial
siempre); en vez de esperar al render completo, cada escena entra aqui en cuanto existe
y sale a B2 troceada en segmentos de ~4 s. La playlist `incoming/{job}/index.m3u8` se
regenera y se re-sube **tras cada segmento**, con `#EXT-X-PLAYLIST-TYPE:EVENT` mientras
el job vive y `#EXT-X-ENDLIST` al cerrar. El navegador reproduce el segundo 0:00 mientras
la escena 5 aun se esta generando.

Decisiones que importan:
- Todas las escenas se transcodifican a los MISMOS parametros (§2 del plan) antes de
  segmentar. Si no, el player rompe al cruzar de escena.
- Los segmentos se suben **conforme ffmpeg los va cerrando** (se vigila la playlist local
  que ffmpeg reescribe), no al final: eso es lo que baja el first-frame a segundos.
- Cada escena es un encode independiente, asi que sus timestamps arrancan en 0 =>
  se emite `#EXT-X-DISCONTINUITY` en la frontera de escena. hls.js lo maneja; sin ella
  el player se atasca.
- Copia local en `data/hls/{job}/` ademas de B2: si B2 va lento el streamer sirve de disco.
  La demo no se cae por un timeout de red.

Autocomprobacion (no necesita B2):
    .venv/bin/python -m server.assembler
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HLS_DIR = Path(os.getenv("FIRSTFRAME_HLS", ROOT / "data" / "hls"))
# 2 s en vez de 4: el primer segmento no se puede publicar hasta que se cierra, asi que
# el tamano del segmento ES el suelo del "first frame". 2 s da margen y hls.js lo traga.
SEG_SECONDS = float(os.getenv("HLS_SEG_SECONDS", "2"))

# Parametros comunes de §2 del plan. Todo mp4 que sale del pipeline pasa por aqui.
VIDEO_ARGS = [
    "-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "main",
    "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,"
           "pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=24,format=yuv420p",
    "-g", "48", "-keyint_min", "48", "-sc_threshold", "0",
    "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
]
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]

# RLock: start_leader coge el lock del job y luego llama a feed(), que lo
# vuelve a coger. Con un Lock normal eso es un deadlock.
_job_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _job_lock(job_id: str) -> "threading.RLock":
    with _locks_guard:
        return _job_locks.setdefault(job_id, threading.RLock())


def local_dir(job_id: str) -> Path:
    d = HLS_DIR / job_id / "seg"
    d.mkdir(parents=True, exist_ok=True)
    return d


def playlist_key(job_id: str) -> str:
    return f"incoming/{job_id}/index.m3u8"


def segment_key(job_id: str, name: str) -> str:
    return f"incoming/{job_id}/seg/{name}"


# ------------------------------------------------------------------ ffmpeg
def _has_audio(path: str | Path) -> bool:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=index", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False).stdout
        return bool(json.loads(out or "{}").get("streams"))
    except Exception:
        return False


def duration_of(path: str | Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, check=False).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def _ffmpeg_cmd(src: Path, out_dir: Path, start_number: int, playlist: Path) -> list[str]:
    audio_in = _has_audio(src)
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", str(src)]
    if not audio_in:
        # pista de silencio: sin audio consistente entre segmentos el player se lia
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    cmd += VIDEO_ARGS + AUDIO_ARGS
    cmd += [
        "-f", "hls",
        "-hls_time", str(SEG_SECONDS),
        "-hls_list_size", "0",
        "-hls_flags", "append_list+independent_segments",
        "-hls_playlist_type", "event",
        "-start_number", str(start_number),
        "-hls_segment_filename", str(out_dir / "%05d.ts"),
        str(playlist),
    ]
    return cmd


def _parse_local_playlist(path: Path) -> list[tuple[str, float]]:
    """[(nombre_segmento, duracion)] de la playlist que ffmpeg va reescribiendo."""
    if not path.is_file():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out, dur = [], None
    for ln in lines:
        ln = ln.strip()
        if ln.startswith("#EXTINF:"):
            try:
                dur = float(ln[len("#EXTINF:"):].split(",")[0])
            except ValueError:
                dur = SEG_SECONDS
        elif ln and not ln.startswith("#"):
            out.append((Path(ln).name, dur if dur is not None else SEG_SECONDS))
            dur = None
    return out


# ------------------------------------------------------------------ playlist
def build_playlist(job_id: str, finished: bool = False) -> str:
    from server import db

    segs = db.segments(job_id)
    target = max([int(s["duration"] + 0.999) for s in segs] or [int(SEG_SECONDS)])
    out = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:EVENT" if not finished else "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    prev_scene = None
    for s in segs:
        # cada escena es un encode aparte: sus PTS arrancan en 0 -> discontinuidad
        if prev_scene is not None and s["scene"] != prev_scene:
            out.append("#EXT-X-DISCONTINUITY")
        prev_scene = s["scene"]
        out.append(f"#EXTINF:{float(s['duration']):.3f},")
        out.append(f"seg/{s['name']}")
    if finished:
        out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


_last_playlist_upload: dict[str, float] = {}
PLAYLIST_UPLOAD_EVERY_S = float(os.getenv("B2_PLAYLIST_UPLOAD_EVERY_S", "5"))


def publish_playlist(job_id: str, finished: bool = False) -> str:
    """Regenera la playlist, la deja en disco **siempre** y la sube a B2 con freno.

    Lo que sirve al player es la version de disco/DB, que se regenera en cada segmento.
    La copia de B2 es el artefacto durable (y lo que se enseña al jurado), asi que no
    necesita subirse 1 vez por segmento: se sube como mucho cada
    `B2_PLAYLIST_UPLOAD_EVERY_S`, y siempre al cerrar. Menos transacciones, mismo
    resultado observable.
    """
    body = build_playlist(job_id, finished)
    lp = HLS_DIR / job_id / "index.m3u8"
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(body)
    now = time.time()
    if finished or now - _last_playlist_upload.get(job_id, 0) >= PLAYLIST_UPLOAD_EVERY_S:
        if _upload(playlist_key(job_id), body.encode(), "application/vnd.apple.mpegurl"):
            _last_playlist_upload[job_id] = now
    return body


def _upload(key: str, data: bytes, ct: str) -> bool:
    from server import b2

    if not b2.available():
        return False
    try:
        b2.put_bytes(key, data, ct)
        return True
    except Exception as e:  # la demo no se cae por un timeout de red
        print(f"[assembler] WARN subida fallida {key}: {e}")
        return False


# ------------------------------------------------------------------ API publica
def feed(job_id: str, scene_path: str, *, scene_no: int | None = None) -> dict:
    """Segmenta un mp4 de escena y lo publica en B2 segmento a segmento.

    Idempotente por (job_id, scene_no): si esa escena ya tiene segmentos, no repite.
    Sincrona; el runner la llama desde su thread.
    """
    from server import db, events

    db.init()
    src = Path(scene_path)
    if not src.is_file():
        raise FileNotFoundError(scene_path)

    with _job_lock(job_id):
        existing = db.segments(job_id)
        if scene_no is not None and any(s["scene"] == scene_no for s in existing):
            return {"segments": 0, "skipped": True,
                    "playlist_key": playlist_key(job_id)}
        if scene_no is None:
            scene_no = (max([s["scene"] or 0 for s in existing] or [0])) + 1

        out_dir = local_dir(job_id)
        start = db.next_seq(job_id)
        tmp_pl = out_dir.parent / f".scene-{scene_no}.m3u8"
        cmd = _ffmpeg_cmd(src, out_dir, start, tmp_pl)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        published: set[str] = set()
        count = 0
        total = 0.0
        t0 = time.time()

        def flush(final: bool) -> None:
            nonlocal count, total
            entries = _parse_local_playlist(tmp_pl)
            # mientras ffmpeg corre, el ultimo entry puede estar aun escribiendose:
            # solo se publica cuando ya hay otro detras (o cuando ffmpeg ha terminado).
            usable = entries if final else entries[:-1]
            for name, dur in usable:
                if name in published:
                    continue
                seg = out_dir / name
                if not seg.is_file():
                    continue
                published.add(name)
                seq = start + count
                data = seg.read_bytes()
                key = segment_key(job_id, name)
                _upload(key, data, "video/mp2t")
                db.add_segment(job_id, seq, name, dur, scene_no, key)
                count += 1
                total += dur
                publish_playlist(job_id, finished=False)   # <- por CADA segmento
                events.publish("segment_landed", {
                    "job_id": job_id, "at": db.now_ms(), "seq": seq, "key": key,
                    "name": name, "duration": dur, "scene": scene_no,
                })
                if scene_no != 0 and not db.get_job(job_id)["first_frame_ms"]:
                    # el cronometro de "first frame" para en cuanto el primer segmento
                    # de CONTENIDO REAL esta disponible (la cabecera no cuenta)
                    job = db.get_job(job_id)
                    if job and job["started_at"] and not job["first_frame_ms"]:
                        db.update_job(job_id,
                                      first_frame_ms=db.now_ms() - job["started_at"])
                    events.publish("render_started", {"job_id": job_id, "at": db.now_ms(),
                                                      "scene": scene_no})

        while proc.poll() is None:
            flush(final=False)
            time.sleep(0.25)
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg fallo ({proc.returncode}): {err[-600:]}")
        flush(final=True)
        if count == 0:
            raise RuntimeError(f"ffmpeg no produjo segmentos para {scene_path}: {err[-400:]}")
        tmp_pl.unlink(missing_ok=True)

        return {
            "segments": count,
            "duration": round(total, 3),
            "first_seq": start,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "playlist_key": playlist_key(job_id),
            "scene": scene_no,
        }


def start_leader(job_id: str, text: str = "preparando la escena 1") -> dict | None:
    """Publica una cabecera de 2 s ("slate") en cuanto se crea el job.

    Por que existe: el player tiene que engancharse SIEMPRE, y la primera escena real
    puede tardar 10 s o mas (el pipeline con providers de verdad). Verificado en Chrome:
    si el primer m3u8 llega en 404 (o vacio), hls.js muere y no reintenta. Con esta
    cabecera la playlist existe ~1 s despues del POST, el player entra en LIVE y las
    escenas reales se van apendando detras. El rotulo dice literalmente que se esta
    generando: no simula contenido, informa.
    """
    import tempfile

    from server import db

    db.init()
    # el lock del job garantiza que la cabecera se lleva el seq 1 aunque la escena 1
    # llegue mientras se genera: feed() espera a que esta suelte.
    with _job_lock(job_id):
        if db.segments(job_id):
            return None
        return _leader_inner(job_id, text)


def _leader_inner(job_id: str, text: str) -> dict | None:
    import tempfile

    tmp = Path(tempfile.mkdtemp()) / "leader.mp4"
    safe = text.replace("'", "").replace(":", " ")[:60]
    try:
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=0x101014:s=1280x720:r=24:d=2",
            "-vf", f"drawtext=text='FirstFrame · LIVE':fontcolor=white:fontsize=48:"
                   f"x=(w-tw)/2:y=280,"
                   f"drawtext=text='{safe}':fontcolor=0x9aa0a6:fontsize=28:"
                   f"x=(w-tw)/2:y=360",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(tmp),
        ], check=True, capture_output=True, timeout=30)
        return feed(job_id, str(tmp), scene_no=0)
    except Exception as e:
        print(f"[assembler] WARN leader fallo: {e}")
        return None


def finish(job_id: str) -> None:
    """Cierra la playlist con #EXT-X-ENDLIST y la sube por ultima vez."""
    with _job_lock(job_id):
        publish_playlist(job_id, finished=True)


def concat_master(job_id: str, out_path: str | Path) -> Path:
    """Une todos los segmentos en un mp4 unico (para approve / final.mp4)."""
    from server import db

    # la cabecera (scene 0) es andamiaje del preview: fuera del master aprobado
    segs = [s for s in db.segments(job_id) if s["scene"] != 0]
    if not segs:
        raise RuntimeError(f"job {job_id} no tiene segmentos")
    d = local_dir(job_id)
    listing = d.parent / "concat.txt"
    listing.write_text("".join(f"file '{(d / s['name']).as_posix()}'\n" for s in segs))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out)],
        check=True, capture_output=True)
    return out


def reset(job_id: str) -> None:
    shutil.rmtree(HLS_DIR / job_id, ignore_errors=True)


# ------------------------------------------------------------------ demo()
def demo() -> None:
    """Autocomprobacion sin B2: 2 escenas -> playlist creciente -> ENDLIST -> master."""
    import tempfile

    global HLS_DIR
    tmp = Path(tempfile.mkdtemp())
    HLS_DIR = tmp / "hls"

    from server import db

    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.pop("conn", None)
    db._initialized = False
    db.init()
    db.create_job("j_asm", "b", "t", 2)

    scenes = []
    for i, color in enumerate(("red", "blue"), start=1):
        p = tmp / f"scene{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", f"testsrc2=size=640x360:rate=30:duration=9",
             "-vf", f"drawbox=color={color}@0.3:t=fill", "-c:v", "libx264",
             "-pix_fmt", "yuv420p", str(p)], check=True)
        scenes.append(p)
    print("1. 2 escenas de 9 s generadas con ffmpeg (params distintos a los comunes: 640x360@30)")

    r1 = feed("j_asm", str(scenes[0]), scene_no=1)
    assert r1["segments"] >= 2, r1
    pl1 = build_playlist("j_asm")
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in pl1 and "#EXT-X-ENDLIST" not in pl1
    assert pl1.count("seg/") == r1["segments"]
    print(f"2. escena 1 -> {r1['segments']} segmentos en {r1['elapsed_ms']} ms, "
          f"playlist EVENT sin ENDLIST OK")

    # idempotencia: re-alimentar la misma escena no duplica nada
    again = feed("j_asm", str(scenes[0]), scene_no=1)
    assert again["skipped"] and again["segments"] == 0, again
    print("3. feed repetido de la escena 1 -> no-op (idempotente) OK")

    r2 = feed("j_asm", str(scenes[1]), scene_no=2)
    pl2 = build_playlist("j_asm")
    assert pl2.count("seg/") == r1["segments"] + r2["segments"]
    assert len(pl2) > len(pl1), "la playlist tiene que CRECER"
    assert "#EXT-X-DISCONTINUITY" in pl2, "falta la discontinuidad entre escenas"
    print(f"4. escena 2 -> playlist crece a {pl2.count('seg/')} segmentos "
          f"con #EXT-X-DISCONTINUITY OK")

    # todos los segmentos tienen los params comunes
    seg0 = local_dir("j_asm") / db.segments("j_asm")[0]["name"]
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,avg_frame_rate,pix_fmt", "-of", "json", str(seg0)],
        capture_output=True, text=True, check=True).stdout
    st = json.loads(probe)["streams"][0]
    assert (st["width"], st["height"]) == (1280, 720), st
    assert st["pix_fmt"] == "yuv420p", st
    assert st["avg_frame_rate"] in ("24/1", "24000/1000"), st
    print(f"5. segmento normalizado a {st['width']}x{st['height']} "
          f"{st['avg_frame_rate']} {st['pix_fmt']} OK")

    finish("j_asm")
    pl3 = (HLS_DIR / "j_asm" / "index.m3u8").read_text()
    assert pl3.rstrip().endswith("#EXT-X-ENDLIST"), pl3[-200:]
    print("6. finish() -> #EXT-X-ENDLIST OK")

    master = concat_master("j_asm", tmp / "final.mp4")
    dur = duration_of(master)
    assert dur > 15, dur
    print(f"7. concat_master -> {master.name} {dur:.1f} s OK")
    print("assembler.demo OK ->", tmp)


if __name__ == "__main__":
    demo()
