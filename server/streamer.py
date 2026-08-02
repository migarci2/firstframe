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
import os
import re
from pathlib import Path

RETRY_TOTAL_S = 2.0
RETRY_SLEEP_S = 0.25
# Cuanto aguanta el servidor la PRIMERA peticion del m3u8 esperando al primer segmento.
# Por debajo del manifestLoadingTimeOut de hls.js (10 s) con margen.
PLAYLIST_WAIT_S = 6.0
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

    VERIFICADO EN CHROME (hls.js 1.5), y las dos alternativas obvias no valen:
      - devolver 404 en la primera peticion -> `manifestLoadError` FATAL y hls.js **no
        vuelve a intentarlo nunca**, aunque el segmento aparezca 2 s despues;
      - devolver una playlist EVENT vacia pero valida -> `levelEmptyError`, mismo final.
    Como el caso normal de esta app es justo ese (el usuario crea el job y el player se
    engancha antes de que exista el primer segmento), el servidor **retiene** la primera
    peticion hasta PLAYLIST_WAIT_S esperando al segmento 1. El first frame llega a los
    ~3 s y el limite de hls.js son 10 s, asi que la primera respuesta ya trae contenido.
    404 solo para jobs que no existen o que tardan mas de la cuenta (el front reintenta).
    """
    from server import assembler, db

    deadline = asyncio.get_event_loop().time() + PLAYLIST_WAIT_S
    while True:
        try:
            db.init()
            segs = db.segments(job_id)
            job = db.get_job(job_id)
        except Exception:
            segs, job = [], None
        if segs:
            finished = bool(job and job["status"] in ("in_review", "approved",
                                                      "rejected", "failed"))
            return assembler.build_playlist(job_id, finished=finished).encode()
        # job vivo pero aun sin segmentos: esperar en vez de romper el player
        alive = job is not None and job["status"] in ("queued", "rendering")
        if not alive or asyncio.get_event_loop().time() >= deadline:
            break
        await asyncio.sleep(RETRY_SLEEP_S)

    # la playlist se sirve de la DB o del disco; B2 solo si el proceso perdio ambos
    lp = _local_path(job_id)
    if lp.is_file():
        return lp.read_bytes()
    if not _b2_usable():
        return None
    got = await asyncio.to_thread(_fetch_sync, f"incoming/{job_id}/index.m3u8")
    return got[0] if got else None


# ------------------------------------------------------------------ segmentos
async def read_range(job_id: str, name: str, range_header: str | None = None):
    """Devuelve (body, status, headers) o None si el segmento no aparece a tiempo.

    **Disco local primero, y casi siempre solo disco local.** El assembler escribe cada
    segmento en `data/hls/` ANTES de subirlo a B2, asi que el fichero ya esta aqui
    cuando el player lo pide. Servirlo desde B2 costaba una transaccion Class B por
    segmento **y por espectador** (un job de 3 escenas son ~10 segmentos), que es lo que
    reventaba la cuota de la cuenta free. B2 sigue siendo el almacen durable —los
    segmentos se suben igual, y lo que se sirve DESDE B2 con presigned URL son los
    assets aprobados, que son pocos— pero la reproduccion ya no lo toca.

    B2 queda como fallback de **una sola llamada**, al final del reintento, para el caso
    de un proceso reiniciado que perdio el disco local. Con cap activo ni eso.
    """
    name = _safe(name)
    if not name:
        return None
    deadline = asyncio.get_event_loop().time() + RETRY_TOTAL_S

    while True:
        # 1) disco local: instantaneo, gratis, es la ruta normal
        lp = _local_path(job_id, name)
        if lp.is_file():
            return _serve_bytes(lp.read_bytes(), name, range_header)
        if asyncio.get_event_loop().time() >= deadline:
            break
        # el segmento puede estar cerrandose ahora mismo: la playlist va por delante
        await asyncio.sleep(RETRY_SLEEP_S)

    # 2) fallback: UNA lectura de B2, solo si el disco local no lo tiene.
    # HLS_SERVE_FROM=local lo prohibe del todo (modo ahorro de cuota).
    if os.getenv("HLS_SERVE_FROM", "auto") == "local" or not _b2_usable():
        return None
    got = await asyncio.to_thread(_fetch_sync, f"incoming/{job_id}/seg/{name}")
    return _serve_bytes(got[0], name, range_header) if got else None


def _b2_usable() -> bool:
    from server import b2

    return b2.available()


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

        # LO QUE REVENTO LA CUOTA: servir cada segmento desde B2. Ahora, cero.
        from server import b2

        before = b2.stats()["total"]
        for i in range(20):
            (seg / f"{i:05d}.ts").write_bytes(b"x" * 128)
            r = await read_range("j_late", f"{i:05d}.ts")
            assert r and r[1] == 200
        assert b2.stats()["total"] == before, "reproducir NO puede tocar B2"
        print("5. 20 segmentos servidos -> 0 transacciones de B2 OK")

    asyncio.run(_t())
    print("streamer.demo OK")


if __name__ == "__main__":
    demo()
