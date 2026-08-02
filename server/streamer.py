"""Sirve la playlist y los segmentos al navegador.

El navegador nunca habla con B2 directo (bucket privado): pasa por aqui.
Dos cosas que hay que hacer bien o el player se atasca:

- **404-retry.** El player pide un segmento que la playlist ya anuncia pero que aun esta
  aterrizando en B2. En vez de devolver 404 (hls.js aborta el nivel), se reintenta ~2 s.
- **Sin cache.** La playlist crece; cualquier cacheo intermedio congela el stream.

Fuente de datos: B2 primero, disco local (`data/hls/`) como fallback inmediato. En la
demo el fallback importa: un timeout de red no puede cortar la reproduccion.

Interfaz estable (la usa server/app.py):
    await read_playlist(job_id) -> bytes | None
    await read_range(job_id, name, range_header) -> (body, status, headers) | None
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

RETRY_TOTAL_S = 2.0
RETRY_SLEEP_S = 0.25
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _ct(name: str) -> str:
    n = name.lower()
    if n.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if n.endswith(".ts"):
        return "video/mp2t"
    if n.endswith((".mp4", ".m4s")):
        return "video/mp4"
    return "application/octet-stream"


def _safe(name: str) -> str | None:
    """Ni rutas ni '..' — esto se sirve a internet."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        return None
    return name


def _local_path(job_id: str, name: str | None = None) -> Path:
    from server import assembler

    base = assembler.HLS_DIR / job_id
    return base / "seg" / name if name else base / "index.m3u8"


def _fetch_sync(key: str, byte_range: str | None = None):
    from server import b2

    if not b2.available():
        return None
    try:
        return b2.get_bytes(key, byte_range)
    except Exception:
        return None


# ------------------------------------------------------------------ playlist
async def read_playlist(job_id: str) -> bytes | None:
    """La playlist se regenera desde la DB: siempre es la version mas fresca.

    Si el job aun no tiene segmentos devuelve None (404) y el front reintenta.
    """
    from server import assembler, db

    try:
        db.init()
        segs = db.segments(job_id)
    except Exception:
        segs = []
    if segs:
        job = db.get_job(job_id)
        finished = bool(job and job["status"] in ("in_review", "approved", "rejected", "failed"))
        return assembler.build_playlist(job_id, finished=finished).encode()

    lp = _local_path(job_id)
    if lp.is_file():
        return lp.read_bytes()
    got = await asyncio.to_thread(_fetch_sync, f"incoming/{job_id}/index.m3u8")
    return got[0] if got else None


# ------------------------------------------------------------------ segmentos
async def read_range(job_id: str, name: str, range_header: str | None = None):
    """Devuelve (body, status, headers) o None si el segmento no aparece a tiempo.

    Reintenta ante ausencia porque el segmento puede estar todavia subiendose:
    la playlist va por delante del bucket a proposito.
    """
    name = _safe(name)
    if not name:
        return None
    key = f"incoming/{job_id}/seg/{name}"
    deadline = asyncio.get_event_loop().time() + RETRY_TOTAL_S

    while True:
        # 1) disco local (instantaneo, es donde el assembler los escribe primero)
        lp = _local_path(job_id, name)
        if lp.is_file():
            return _serve_bytes(lp.read_bytes(), name, range_header)
        # 2) B2
        got = await asyncio.to_thread(_fetch_sync, key)
        if got:
            return _serve_bytes(got[0], name, range_header)
        if asyncio.get_event_loop().time() >= deadline:
            return None
        await asyncio.sleep(RETRY_SLEEP_S)


def _serve_bytes(data: bytes, name: str, range_header: str | None):
    total = len(data)
    headers = {
        "Content-Type": _ct(name),
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=31536000, immutable",
        "Access-Control-Allow-Origin": "*",
    }
    if not range_header:
        headers["Content-Length"] = str(total)
        return data, 200, headers

    m = _RANGE_RE.search(range_header)
    if not m:
        headers["Content-Length"] = str(total)
        return data, 200, headers
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "":                       # bytes=-N  (ultimos N)
        n = int(end_s or 0)
        start, end = max(0, total - n), total - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
    end = min(end, total - 1)
    if start > end or start >= total:
        return b"", 416, {"Content-Range": f"bytes */{total}", **headers}
    chunk = data[start:end + 1]
    headers["Content-Length"] = str(len(chunk))
    headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    return chunk, 206, headers


# ------------------------------------------------------------------ demo()
def demo() -> None:
    """Autocomprobacion: rangos, sanitizado de nombres y 404-retry."""
    import tempfile
    import time

    body = bytes(range(256)) * 8      # 2048 bytes
    b, st, h = _serve_bytes(body, "00001.ts", None)
    assert st == 200 and len(b) == 2048 and h["Content-Type"] == "video/mp2t"
    b, st, h = _serve_bytes(body, "00001.ts", "bytes=0-511")
    assert st == 206 and len(b) == 512 and h["Content-Range"] == "bytes 0-511/2048", h
    b, st, h = _serve_bytes(body, "00001.ts", "bytes=2040-")
    assert st == 206 and len(b) == 8, len(b)
    b, st, h = _serve_bytes(body, "00001.ts", "bytes=-16")
    assert st == 206 and len(b) == 16 and b == body[-16:]
    b, st, h = _serve_bytes(body, "00001.ts", "bytes=9999-")
    assert st == 416, st
    print("1. rangos: completo, 0-511, sufijo abierto, ultimos-N, fuera de rango OK")

    assert _safe("00001.ts") == "00001.ts"
    assert _safe("../../etc/passwd") is None
    assert _safe("a/b.ts") is None
    assert _safe("") is None
    assert _safe("x" * 100) is None
    print("2. sanitizado de nombres (path traversal) OK")

    async def _t():
        from server import assembler

        tmp = Path(tempfile.mkdtemp())
        assembler.HLS_DIR = tmp
        t0 = time.time()
        res = await read_range("j_nope", "00001.ts")
        el = time.time() - t0
        assert res is None, res
        assert RETRY_TOTAL_S <= el < RETRY_TOTAL_S + 1.5, el
        print(f"3. segmento inexistente: reintenta {el:.1f} s antes de rendirse OK")

        # aparece a mitad del retry: el player no llega a ver un 404
        seg = tmp / "j_late" / "seg"
        seg.mkdir(parents=True)

        async def late():
            await asyncio.sleep(0.6)
            (seg / "00001.ts").write_bytes(b"tarde pero llego")

        asyncio.get_event_loop().create_task(late())
        t0 = time.time()
        res = await read_range("j_late", "00001.ts")
        assert res and res[1] == 200 and res[0] == b"tarde pero llego", res
        print(f"4. segmento que aparece a los 0.6 s: servido 200 en {time.time()-t0:.1f} s OK")

    asyncio.run(_t())
    print("streamer.demo OK")


if __name__ == "__main__":
    demo()
