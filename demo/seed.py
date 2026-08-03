#!/usr/bin/env python3
"""Deja la sala de revisión presentable: purga la basura y precarga datos.

Un juez que abre la URL y encuentra la sala vacía puntúa lo que ve, que es nada.
Este script garantiza que siempre haya:

  * **1 job aprobado** — con su manifest de provenance legible y, cuando la cuota de
    B2 lo permita, su badge de Object Lock GOVERNANCE.
  * **1 job en revisión** — listo para que un juez pulse Approve o Reject y vea
    pasar algo (el rechazo dispara el juez de visión + AgentLoop).

Y purga lo que ensucia:

  * todos los jobs en `failed` (restos de los intentos previos al arreglo del preflight),
  * los jobs de prueba por título ("retest live", "modo emergencia", "medicion…", …).

Uso:
    .venv/bin/python demo/seed.py                 # purga + siembra lo que falte
    .venv/bin/python demo/seed.py --purge-only    # solo limpia
    .venv/bin/python demo/seed.py --seed-only     # solo siembra
    .venv/bin/python demo/seed.py --reset         # purga TODO y siembra de cero
    .venv/bin/python demo/seed.py --url http://localhost:8000

Habla con la API HTTP (no con la DB) para sembrar: así los jobs sembrados recorren
exactamente el mismo camino que los del juez — pipeline real, assembler, HLS, approve.
La purga sí toca sqlite directamente, que es lo único que la API no ofrece.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.getenv("FIRSTFRAME_DB", ROOT / "data" / "firstframe.db"))
WORK = Path(os.getenv("FIRSTFRAME_WORK", ROOT / "data" / "work"))
RUNS = Path(os.getenv("FIRSTFRAME_RUNS", ROOT / "runs"))
HLS = Path(os.getenv("FIRSTFRAME_HLS", ROOT / "data" / "hls"))

# Títulos que delatan un job de prueba. Se comparan en minúsculas, sin acentos.
JUNK_TITLE = re.compile(
    r"retest|modo emergencia|medicion|medición|prueba|^test\b|spot en vivo \d+ escenas"
    r"|smoke|probe|scratch",
    re.I,
)

# Lo que ve el juez. Briefs cortos: en cámara se leen enteros.
# En ingles: el jurado del hackathon es de Backblaze y la submission va en ingles.
# Y son briefs de PRODUCTO a proposito — el generador saca caras plastificadas en
# cuanto el brief invita a poner una persona (ver docstring de pipeline/prompts.py).
SEED_APPROVED = {
    "title": "Aeron Runner — dawn on the beach",
    "brief": "15s spot of an Aeron running shoe at dawn on an empty beach: product in "
             "the foreground, wet sand, warm raking light.",
    "scenes": 3,
}
SEED_IN_REVIEW = {
    "title": "Nova Buds — floating product",
    "brief": "15s teaser of Nova earbuds on a clean backdrop: the product floats and "
             "turns slowly, studio light, closing on the claim.",
    "scenes": 3,
}

TABLES_BY_JOB = ("scenes", "decisions", "provider_events", "segments", "events")


# ------------------------------------------------------------------ HTTP
# La instancia va detrás de un código de acceso (server/auth.py). El sembrado entra
# por la MISMA puerta que un juez —POST /api/access— en vez de saltársela: si el muro
# se rompe, el sembrado se entera aquí y no en la demo.
_COOKIE: str | None = None


def _req(url: str, method: str = "GET", body: dict | None = None, timeout: float = 30.0):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"}
    if _COOKIE:
        headers["cookie"] = _COOKIE
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else {})


def login(base: str) -> None:
    global _COOKIE
    code = os.getenv("DEMO_ACCESS_CODE", "FIRSTFRAME")
    if not code.strip():
        return                                  # muro desactivado: nada que hacer
    req = urllib.request.Request(f"{base}/api/access", data=json.dumps({"code": code}).encode(),
                                 method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.headers.get("set-cookie") or ""
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[seed] el codigo de acceso no vale (HTTP {e.code}). "
                         f"Revisa DEMO_ACCESS_CODE.")
    _COOKIE = raw.split(";", 1)[0]
    if not _COOKIE:
        raise SystemExit("[seed] /api/access no devolvio cookie")


def wait_for_server(base: str, timeout: float = 60.0) -> dict:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            _, h = _req(f"{base}/api/health", timeout=5)
            return h
        except Exception as e:      # noqa: BLE001 — arrancando todavía
            last = e
            time.sleep(0.5)
    raise SystemExit(f"[seed] el servidor no respondió en {timeout:.0f}s ({last})")


# ------------------------------------------------------------------ purga
def _junk(row: sqlite3.Row, reset: bool) -> str | None:
    if reset:
        return "reset"
    if row["status"] == "failed":
        return "failed"
    if row["title"] and JUNK_TITLE.search(row["title"]):
        return "título de prueba"
    return None


def purge(*, reset: bool = False, keep: set[str] | None = None, dry: bool = False,
          keep_recent: int = 4) -> list[str]:
    """Borra jobs fallidos y de prueba, con sus filas hijas y sus ficheros en disco.

    `keep_recent` recorta además la cola: una sala con nueve jobs casi idénticos se lee
    como un cajón de sastre. Se conservan los N más recientes (y nunca se toca el
    aprobado más nuevo ni el en-revisión más nuevo, que son los que ve el juez).
    """
    keep = set(keep or ())
    if not DB.is_file():
        print(f"[seed] no hay DB en {DB}: nada que purgar")
        return []
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = list(c.execute("SELECT id, title, status FROM jobs ORDER BY created_at DESC"))
    if not reset:
        alive = [r for r in rows if not _junk(r, False)]
        keep |= {r["id"] for r in alive[:max(0, keep_recent)]}
        for st in ("approved", "in_review"):
            nxt = next((r["id"] for r in alive if r["status"] == st), None)
            if nxt:
                keep.add(nxt)
    doomed: list[tuple[str, str]] = []
    for row in reversed(rows):
        if row["id"] in keep:
            continue
        why = _junk(row, reset) or "sobra en la cola"
        doomed.append((row["id"], f'{row["status"]:<10} {why:<16} {row["title"] or ""}'))
    for jid, why in doomed:
        print(f"[seed] purga {jid}  {why}")
        if dry:
            continue
        for t in TABLES_BY_JOB:
            try:
                c.execute(f"DELETE FROM {t} WHERE job_id = ?", (jid,))
            except sqlite3.OperationalError:
                pass                      # la tabla puede no tener job_id
        c.execute("DELETE FROM jobs WHERE id = ?", (jid,))
        for d in (WORK / jid, RUNS / jid, HLS / jid):
            shutil.rmtree(d, ignore_errors=True)
    if not dry:
        c.commit()
    c.close()
    print(f"[seed] purgados {len(doomed)} jobs" + (" (simulacro)" if dry else ""))
    return [j for j, _ in doomed]


# ------------------------------------------------------------------ siembra
def _jobs(base: str) -> list[dict]:
    return _req(f"{base}/api/jobs")[1]["jobs"]


def _wait_status(base: str, jid: str, wanted: tuple[str, ...], timeout: float) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = _req(f"{base}/api/jobs/{jid}")[1]["job"]
        if j["status"] in wanted:
            return j
        if j["status"] == "failed":
            raise SystemExit(f"[seed] {jid} falló: {j.get('error')}")
        time.sleep(1.5)
    raise SystemExit(f"[seed] {jid} sigue en '{j['status']}' tras {timeout:.0f}s")


def render(base: str, spec: dict, timeout: float = 240.0) -> dict:
    t0 = time.time()
    _, out = _req(f"{base}/api/jobs", "POST", spec)
    jid = out["id"]
    print(f"[seed] {jid} '{spec['title']}' — generando {spec['scenes']} escenas…")
    j = _wait_status(base, jid, ("in_review",), timeout)
    print(f"[seed] {jid} listo en {time.time() - t0:.1f}s "
          f"(first frame {j['first_frame_ms']} ms, total {j['total_render_ms']} ms)")
    return j


def approve(base: str, jid: str) -> dict:
    # El badge de Object Lock depende de un `get_object_retention` (Class B). Con la
    # cuota agotada el backend entra en enfriamiento y ni lo intenta, asi que se sale
    # del enfriamiento justo antes: es la unica ventana en la que el lock puede salir.
    try:
        _req(f"{base}/api/health/reset-b2-stats", "POST", {}, timeout=10)
    except Exception:
        pass
    _, out = _req(f"{base}/api/jobs/{jid}/decision", "POST",
                  {"action": "approve", "note": "aprobado para la demo"}, timeout=180)
    j = out["job"]
    lock = j.get("lock")
    print(f"[seed] {jid} aprobado — lock={lock or 'PENDIENTE (cuota B2 agotada)'}")
    if not lock:
        print("[seed] AVISO: sin badge de Object Lock. El master SI esta subido "
              "(las subidas son Class A); falta la lectura de la retencion. "
              "Vuelve a intentarlo cuando la cuota se recupere.")
    return j


def seed(base: str, *, force: bool = False) -> dict:
    """Garantiza 1 aprobado + 1 en revisión. Idempotente: no duplica si ya están."""
    have = _jobs(base)
    approved = [j for j in have if j["status"] == "approved"]
    in_review = [j for j in have if j["status"] == "in_review"]
    made = {}

    if force or not approved:
        j = render(base, SEED_APPROVED)
        made["approved"] = approve(base, j["id"])
    else:
        made["approved"] = approved[0]
        print(f"[seed] ya hay aprobado: {approved[0]['id']}")

    if force or not in_review:
        made["in_review"] = render(base, SEED_IN_REVIEW)
    else:
        made["in_review"] = in_review[0]
        print(f"[seed] ya hay uno en revisión: {in_review[0]['id']}")

    # El manifest es lo que enseña el panel de provenance en cámara: si no se sirve,
    # el tramo 1:45–2:20 del guion no es grabable. Se comprueba aquí, no en directo.
    for tag, j in made.items():
        code = _manifest_status(base, j["id"])
        flag = "OK" if code == 200 else f"HTTP {code}"
        print(f"[seed] manifest {tag} {j['id']}: {flag}")
        if tag == "approved" and code != 200:
            print("[seed] AVISO: el job aprobado no sirve manifest — revisa "
                  "server/jobs.py:get_manifest")
    return made


def _manifest_status(base: str, jid: str) -> int:
    try:
        return _req(f"{base}/api/jobs/{jid}/manifest")[0]
    except urllib.error.HTTPError as e:
        return e.code


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.getenv("FIRSTFRAME_URL", "http://localhost:8000"))
    ap.add_argument("--purge-only", action="store_true")
    ap.add_argument("--seed-only", action="store_true")
    ap.add_argument("--reset", action="store_true",
                    help="purga TODOS los jobs (no solo los fallidos) y siembra de cero")
    ap.add_argument("--dry-run", action="store_true", help="enseña qué purgaría y sale")
    ap.add_argument("--keep-recent", type=int, default=4,
                    help="cuántos jobs sanos se conservan además de los sembrados (def. 4)")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    if not args.seed_only:
        purge(reset=args.reset, dry=args.dry_run, keep_recent=args.keep_recent)
    if args.dry_run or args.purge_only:
        return 0

    h = wait_for_server(base)
    login(base)
    if h.get("degraded"):
        print(f"[seed] AVISO B2 degradado: {h.get('warning')}")
    made = seed(base, force=args.reset)
    print()
    print(f"[seed] sala lista en {base}")
    print(f"[seed]   aprobado   {made['approved']['id']}  {made['approved']['title']}")
    print(f"[seed]   en revisión {made['in_review']['id']}  {made['in_review']['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
