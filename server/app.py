"""FirstFrame — API HTTP. Contrato exacto en server/API.md.

Arranque:
    STUB=1 .venv/bin/uvicorn server.app:app --port 8000      # datos fake, sin B2 ni pipeline
    .venv/bin/uvicorn server.app:app --port 8000             # real (DEMO_MODE=mock por defecto)

Los modulos pesados (b2, jobs, assembler) se importan de forma perezosa para que
STUB=1 arranque aunque falte cualquier cosa.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


def _stub() -> bool:
    return os.getenv("STUB", "0") not in ("0", "", "false", "no")


app = FastAPI(title="FirstFrame", docs_url="/api/docs", redoc_url=None)


# ---------------------------------------------------------------- lifecycle
@app.on_event("startup")
def _startup() -> None:
    if _stub():
        return
    from server import db, events, jobs

    db.init()
    jobs.resume_orphans()
    events.start(app)


@app.on_event("shutdown")
def _shutdown() -> None:
    if _stub():
        return
    from server import events

    events.stop()


# ---------------------------------------------------------------- helpers
def _err(status: int, msg: str, **extra):
    return JSONResponse({"error": msg, **extra}, status_code=status)


# ---------------------------------------------------------------- jobs
@app.get("/api/jobs")
def list_jobs():
    if _stub():
        from server import stubdata

        return {"jobs": stubdata.jobs()}
    from server import jobs

    return {"jobs": jobs.list_jobs()}


@app.post("/api/jobs")
async def create_job(req: Request):
    body = await _json(req)
    brief = (body.get("brief") or "").strip()
    if not brief:
        return _err(400, "brief is required")
    if _stub():
        from server import stubdata

        j = dict(stubdata.jobs()[0])
        j["brief"] = brief
        j["title"] = body.get("title") or brief[:48]
        return JSONResponse({"id": j["id"], "job": j}, status_code=201)
    from server import jobs

    j = jobs.create_job(brief, title=body.get("title"), scenes=body.get("scenes"))
    return JSONResponse({"id": j["id"], "job": j}, status_code=201)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if _stub():
        from server import stubdata

        j = stubdata.job(job_id) or stubdata.jobs()[0]
        return {
            "job": j,
            "provider_events": stubdata.provider_events(job_id),
            "decisions": [],
            "objects": [{"key": f"approved/{job_id}/final.mp4", "size": 4210233,
                         "at": int(time.time() * 1000)}] if j["status"] == "approved" else [],
        }
    from server import jobs

    detail = jobs.get_job_detail(job_id)
    if detail is None:
        return _err(404, "no such job")
    return detail


@app.post("/api/jobs/{job_id}/decision")
async def decide(job_id: str, req: Request):
    body = await _json(req)
    action = body.get("action")
    if action not in ("approve", "reject"):
        return _err(400, "action must be approve or reject")
    if _stub():
        from server import stubdata

        j = dict(stubdata.job(job_id) or stubdata.jobs()[1])
        if action == "approve":
            j["status"] = "approved"
            j["lock"] = {"mode": "GOVERNANCE", "retain_until": "2026-09-02T08:23:00Z"}
        else:
            j["status"] = "rendering"
        return {"ok": True, "job": j}
    from server import jobs

    try:
        j = jobs.decide(job_id, action, note=body.get("note"), scene=body.get("scene"))
    except jobs.NotFound:
        return _err(404, "no such job")
    except jobs.NotReviewable as e:
        return _err(409, "job not reviewable yet", status=str(e))
    return {"ok": True, "job": j}


# ---------------------------------------------------------------- SSE
@app.get("/api/events")
async def sse(request: Request):
    if _stub():
        return StreamingResponse(_stub_sse(request), media_type="text/event-stream",
                                 headers=_SSE_HEADERS)
    from server import events, jobs

    async def gen():
        q = events.subscribe()
        try:
            yield _sse("hello", {"jobs": jobs.list_jobs(), "at": int(time.time() * 1000)})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    kind, payload = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield _sse("ping", {})
                    continue
                yield _sse(kind, payload)
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_SSE_HEADERS)


_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse(kind: str, payload: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"


async def _stub_sse(request: Request):
    from server import stubdata

    yield _sse("hello", {"jobs": stubdata.jobs(), "at": int(time.time() * 1000)})
    while not await request.is_disconnected():
        kind, payload = stubdata.next_event()
        yield _sse(kind, payload)
        await asyncio.sleep(2)


# ---------------------------------------------------------------- stream HLS
@app.get("/stream/{job_id}")
def stream_root(job_id: str):
    return RedirectResponse(f"/stream/{job_id}/index.m3u8", status_code=307)


@app.get("/stream/{job_id}/index.m3u8")
async def stream_playlist(job_id: str):
    from server import streamer

    data = await streamer.read_playlist(job_id)
    if data is None:
        return _err(404, "no playlist yet")
    return Response(
        content=data,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


@app.get("/stream/{job_id}/seg/{name}")
async def stream_segment(job_id: str, name: str, request: Request):
    from server import streamer

    rng = request.headers.get("range")
    res = await streamer.read_range(job_id, name, rng)
    if res is None:
        return _err(404, "no such segment")
    body, status, headers = res
    return Response(content=body, status_code=status, headers=headers)


# ---------------------------------------------------------------- chaos
@app.post("/api/chaos")
async def chaos(req: Request):
    body = await _json(req)
    provider = body.get("provider") or "gmicloud"
    dead = body.get("dead")
    if _stub():
        return {"provider": provider, "dead": True if dead is None else bool(dead)}
    from server import db, events

    state = db.set_chaos(provider, dead)
    events.publish("chaos", {"provider": provider, "dead": state,
                             "at": int(time.time() * 1000), "job_id": None})
    return {"provider": provider, "dead": state}


# ---------------------------------------------------------------- provenance
@app.get("/api/jobs/{job_id}/manifest")
def manifest(job_id: str):
    if _stub():
        from server import stubdata

        return stubdata.manifest(job_id)
    from server import jobs

    m = jobs.get_manifest(job_id)
    if m is None:
        return _err(404, "no manifest yet")
    return m


@app.get("/api/verify/{job_id}")
def verify(job_id: str):
    if _stub():
        return {"ok": True, "verified": True, "exit_code": 0,
                "output": "manifest embedded: OK\nsignature: OK\n6 scenes verified"}
    from server import jobs

    res = jobs.verify(job_id)
    if res is None:
        return _err(409, "job not approved")
    return res


@app.get("/api/download/{job_id}")
def download(job_id: str):
    if _stub():
        return {"url": f"https://s3.eu-central-003.backblazeb2.com/genblaze-review-migarci2/"
                       f"approved/{job_id}/final.mp4?X-Amz-Signature=stub", "expires_in": 3600}
    from server import b2

    key = f"approved/{job_id}/final.mp4"
    if not b2.head(key):
        return _err(404, "not approved yet")
    return {"url": b2.presign_path_style(key, expires=3600), "expires_in": 3600}


# ---------------------------------------------------------------- webhooks
@app.post("/webhooks/b2")
async def webhook(req: Request):
    raw = await req.body()
    sig = req.headers.get("X-Bz-Event-Notification-Signature", "")
    if _stub():
        return {"ok": True, "queued": 0}
    from server import events

    ok, queued = events.handle_webhook(raw, sig)
    if not ok:
        return _err(401, "bad signature")
    return {"ok": True, "queued": queued}


# ---------------------------------------------------------------- health
@app.get("/api/health")
def health():
    n = 3
    b2ok = False
    if not _stub():
        try:
            from server import db

            n = len(db.all_jobs())
        except Exception:
            n = -1
        b2ok = bool(os.getenv("B2_KEY_ID"))
    return {
        "ok": True,
        "mode": os.getenv("DEMO_MODE", "mock"),
        "events_mode": os.getenv("EVENTS_MODE", "both"),
        "stub": _stub(),
        "b2": b2ok,
        "jobs": n,
    }


async def _json(req: Request) -> dict:
    try:
        b = await req.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- estaticos
# Se monta al final para no tapar /api ni /stream.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:  # el frontend aun no existe (W3 en marcha)
    @app.get("/")
    def _placeholder():
        return {"ok": True, "note": "web/ todavia no existe; API viva en /api/health"}
