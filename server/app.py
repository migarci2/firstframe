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
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
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
    # Los dos almacenes de chaos (sqlite para la UI, data/chaos.json para el pipeline)
    # se sincronizan al arrancar: si el proceso murio con un proveedor muerto, la UI y
    # el pipeline tienen que coincidir antes del primer job.
    for p in db.dead_providers():
        _mirror_chaos(p, True)
    jobs.resume_orphans()
    jobs.warm_runner()
    events.start(app)
    jobs.start_retry_thread()


@app.on_event("shutdown")
def _shutdown() -> None:
    if _stub():
        return
    from server import events

    events.stop()


# ---------------------------------------------------------------- helpers
def _err(status: int, msg: str, **extra):
    return JSONResponse({"error": msg, **extra}, status_code=status)


# ---------------------------------------------------------------- muro de acceso
# La instancia es publica y el jurado entra por la URL, asi que hay un codigo
# compartido (server/auth.py). Lo que NO puede quedar detras del muro:
#   * la portada "/" y sus assets — es material de marketing, tiene que indexar y abrir,
#   * /api/health — es la sonda del contenedor y del script de arranque,
#   * /webhooks/b2 — lo llama Backblaze, que obviamente no trae cookie (va firmado),
#   * la propia pantalla de acceso (/access) y el endpoint que la valida.
# El muro protege /app/*, /api/* y /stream/*, que es la sala; la portada, no.
_PUBLIC_EXACT = {
    "/", "/access", "/gate.html", "/landing.html", "/landing.css", "/landing.js",
    "/api/health", "/api/access", "/favicon.ico", "/robots.txt",
}
_PUBLIC_PREFIX = ("/assets/", "/vendor/", "/webhooks/")


def _is_public(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX)


@app.middleware("http")
async def _access_gate(request: Request, call_next):
    from server import auth

    path = request.url.path
    if (not auth.enabled() or _is_public(path)
            or auth.check_cookie(request.cookies.get(auth.COOKIE))):
        return await call_next(request)
    # Una peticion de datos recibe un error de datos; una de navegacion, la pantalla.
    if path.startswith(("/api/", "/stream/")):
        return _err(401, "This workspace needs an access code.", gate="/access")
    # Se recuerda a donde iba: tras meter el codigo se vuelve ahi, no a la raiz.
    nxt = path + (("?" + request.url.query) if request.url.query else "")
    return RedirectResponse("/access?next=" + quote(nxt, safe=""), status_code=302)


# ---------------------------------------------------------------- rutas de pagina
# Lo primero que ve alguien que llega de Devpost es la portada, no la sala:
#
#   /                          portada (publica)
#   /app                       la sala -> /app/projects
#   /app/projects              rejilla de proyectos
#   /app/p/<proyecto>          un proyecto abierto
#   /app/p/<proyecto>/<spot>   un spot abierto
#   /access                    la pantalla del codigo
#
# Todo /app/* devuelve el mismo documento: el router vive en el cliente
# (web/app.js) con history.pushState, asi que recargar mantiene el sitio y el
# boton atras del navegador funciona.
_LANDING_CACHE: dict[str, str] = {}


def _landing_html() -> str:
    """La portada, con sus CTAs apuntando a /app.

    web/landing.html es de otro workstream y sus botones dicen href="/". Desde
    que la raiz ES la portada eso se muerde la cola, asi que se reescribe al
    vuelo en vez de tocar un fichero que no es de aqui.
    """
    p = WEB_DIR / "landing.html"
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return "<h1>FirstFrame</h1><p><a href=\"/app\">Open the app</a></p>"
    key = str(p.stat().st_mtime_ns)
    if _LANDING_CACHE.get("k") != key:
        _LANDING_CACHE["k"] = key
        _LANDING_CACHE["v"] = raw.replace('href="/"', 'href="/app"')
    return _LANDING_CACHE["v"]


_NO_STORE = {"Cache-Control": "no-store"}


@app.get("/", include_in_schema=False)
def page_landing():
    return HTMLResponse(_landing_html(), headers=_NO_STORE)


@app.get("/landing.html", include_in_schema=False)
def page_landing_old():
    return RedirectResponse("/", status_code=301)


@app.get("/gate.html", include_in_schema=False)
def page_gate_old():
    return RedirectResponse("/access", status_code=301)


@app.get("/index.html", include_in_schema=False)
def page_index_old():
    return RedirectResponse("/app", status_code=301)


@app.get("/access", include_in_schema=False)
def page_access():
    return FileResponse(str(WEB_DIR / "gate.html"), media_type="text/html",
                        headers=_NO_STORE)


@app.get("/app", include_in_schema=False)
def page_app_root():
    return RedirectResponse("/app/projects", status_code=302)


@app.get("/app/{rest:path}", include_in_schema=False)
def page_app(rest: str):
    return FileResponse(str(WEB_DIR / "index.html"), media_type="text/html",
                        headers=_NO_STORE)


def _https(req: Request) -> bool:
    """Detras del proxy de Fly la app ve http; la verdad viene en la cabecera."""
    return (req.headers.get("x-forwarded-proto") or req.url.scheme) == "https"


@app.post("/api/access")
async def access(req: Request):
    from server import auth

    body = await _json(req)
    if not auth.check_code(body.get("code")):
        return _err(401, "That code is not valid.")
    res = JSONResponse({"ok": True})
    res.set_cookie(auth.COOKIE, auth.token(), max_age=auth.MAX_AGE, path="/",
                   httponly=True, secure=_https(req), samesite="lax")
    return res


@app.post("/api/access/exit")
def access_exit():
    from server import auth

    res = JSONResponse({"ok": True})
    res.delete_cookie(auth.COOKIE, path="/")
    return res


# ---------------------------------------------------------------- projects
# Un proyecto agrupa spots. Se persiste en su propia tabla para que uno recien
# creado y todavia vacio siga existiendo al recargar.
@app.get("/api/projects")
def list_projects():
    if _stub():
        from server import stubdata

        names: dict[str, int] = {}
        for j in stubdata.jobs():
            names[j.get("project") or "Untitled Project"] = \
                names.get(j.get("project") or "Untitled Project", 0) + 1
        return {"projects": [{"name": n, "spots": c, "created_at": 0}
                             for n, c in names.items()]}
    from server import jobs

    return {"projects": jobs.list_projects()}


@app.post("/api/projects")
async def create_project(req: Request):
    body = await _json(req)
    name = (body.get("name") or "").strip()
    if not name:
        return _err(400, "name is required")
    if len(name) > 64:
        name = name[:64]
    if _stub():
        return JSONResponse({"project": {"name": name, "spots": 0, "created_at": 0}},
                            status_code=201)
    from server import jobs

    return JSONResponse({"project": jobs.create_project(name)}, status_code=201)


@app.patch("/api/projects/{name:path}")
async def rename_project(name: str, req: Request):
    body = await _json(req)
    new = (body.get("name") or "").strip()
    if not new:
        return _err(400, "name is required")
    if _stub():
        return {"project": {"name": new[:64], "from": name}}
    from server import jobs

    got = jobs.rename_project(name, new)
    if got is None:
        return _err(409, "another project already uses that name")
    return {"project": got}


@app.delete("/api/projects/{name:path}")
def delete_project(name: str):
    """Borra el proyecto CON sus spots. La UI confirma antes; aqui no se pregunta."""
    if _stub():
        return {"ok": True, "deleted": []}
    from server import jobs

    return {"ok": True, "deleted": jobs.delete_project(name)}


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
        j["project"] = body.get("project") or "Untitled Project"
        return JSONResponse({"id": j["id"], "job": j}, status_code=201)
    from server import jobs

    j = jobs.create_job(brief, title=body.get("title"), scenes=body.get("scenes"),
                        project=body.get("project"))
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


@app.patch("/api/jobs/{job_id}")
async def edit_job(job_id: str, req: Request):
    """Renombrar el spot, corregir su brief o moverlo a otro proyecto."""
    body = await _json(req)
    if _stub():
        from server import stubdata

        j = dict(stubdata.job(job_id) or stubdata.jobs()[0])
        j.update({k: v for k, v in body.items() if k in ("title", "brief", "project")})
        return {"job": j}
    from server import jobs

    j = jobs.edit_job(job_id, title=body.get("title"), brief=body.get("brief"),
                      project=body.get("project"))
    if j is None:
        return _err(404, "no such job")
    return {"job": j}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    if _stub():
        return {"ok": True}
    from server import jobs

    if not jobs.delete_job(job_id):
        return _err(404, "no such job")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/regenerate")
async def regenerate_job(job_id: str, req: Request):
    """Vuelve a generar el MISMO spot con el brief que tenga ahora.

    Se conserva el id: el spot no cambia de sitio en el arbol ni pierde su historial
    de decisiones, que es justo lo que se espera al corregir un brief.
    """
    body = await _json(req)
    if _stub():
        from server import stubdata

        return {"job": stubdata.jobs()[0]}
    from server import jobs

    j = jobs.regenerate(job_id, brief=(body.get("brief") or None),
                        scenes=body.get("scenes"))
    if j is None:
        return _err(404, "no such job")
    return {"job": j}


@app.get("/api/jobs/{job_id}/poster.jpg")
def job_poster(job_id: str):
    """Miniatura del spot. 404 si todavia no hay ninguna escena en disco."""
    if _stub():
        return _err(404, "no poster")
    from server import jobs

    p = jobs.poster(job_id)
    if p is None:
        return _err(404, "no poster yet")
    return FileResponse(str(p), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=60"})


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
        return _err(409, "job not reviewable yet", job_status=str(e))
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
    _mirror_chaos(provider, state)
    events.publish("chaos", {"provider": provider, "dead": state,
                             "at": int(time.time() * 1000), "job_id": None})
    return {"provider": provider, "dead": state}


def _mirror_chaos(provider: str, dead: bool) -> None:
    """El pipeline NO lee la DB.

    `pipeline/chaos.py` guarda los flags en `data/chaos.json` a proposito (el runner
    corre en otro thread y no queremos acoplar `pipeline/` a sqlite). Sin este espejo
    el boton de chaos era decorativo: la UI ponia el proveedor en MUERTO, la DB tambien,
    y `ChaosWrapper` seguia delegando tan feliz -> el failover no se veia nunca.
    """
    try:
        from pipeline import chaos as pchaos

        pchaos.kill(provider) if dead else pchaos.revive(provider)
    except Exception as e:      # noqa: BLE001 — nunca tumbar el endpoint por esto
        print(f"[app] WARN chaos no propagado a pipeline/chaos.py: {e!r}")


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
    from server import b2, jobs

    # El presign es una FIRMA LOCAL: cuesta 0 transacciones. Antes esto se colgaba de
    # un b2.head(), que es Class B: con la cuota diaria agotada head() devuelve None y
    # la descarga de un job REALMENTE aprobado respondia "not approved yet". La fuente
    # de verdad de si esta aprobado es la DB, no una lectura a B2.
    j = jobs.public_job(job_id)
    if j is None:
        return _err(404, "no such job")
    if j["status"] != "approved":
        return _err(409, "job not approved", job_status=j["status"])
    key = f"approved/{job_id}/final.mp4"
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
    """Estado + **consumo de transacciones de B2** desde el arranque.

    El contador existe porque la cuenta free tiene tope diario y ya nos lo comimos una
    vez: antes de grabar se mira aqui cuanto llevamos gastado.
    """
    if _stub():
        return {"ok": True, "mode": "mock", "events_mode": "both", "stub": True,
                "b2": False, "jobs": 3, "b2_transactions": {"total": 0, "by_op": {}}}
    from server import b2, db, events

    try:
        n = len(db.all_jobs())
    except Exception:
        n = -1
    tx = b2.stats()
    return {
        "ok": True,
        "mode": os.getenv("DEMO_MODE", "mock"),
        "events_mode": events.mode(),
        "stub": False,
        "b2": b2.has_credentials(),
        "b2_capped": tx["capped"],
        "degraded": tx["capped"] or None,
        "warning": ("B2 sin cuota de transacciones: se sirve todo desde disco local; "
                    "las subidas y los presigns vuelven solos al enfriarse")
                   if tx["capped"] else None,
        "jobs": n,
        "b2_transactions": tx,
        "poller": events.poller_stats(),
        "hls_served_from": os.getenv("HLS_SERVE_FROM", "auto") + " (disco local primero)",
    }


@app.post("/api/health/reset-b2-stats")
def reset_b2_stats():
    """Pone a cero el contador (y sale del enfriamiento) — para medir un job limpio."""
    if _stub():
        return {"ok": True}
    from server import b2

    b2.reset_stats()
    return {"ok": True, "b2_transactions": b2.stats()}


async def _json(req: Request) -> dict:
    try:
        b = await req.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- estaticos
# Se monta al final para no tapar /api ni /stream.
# check_dir=False: web/ lo escribe otro workstream y puede no existir todavia al
# arrancar; sin esto el servidor entero se niega a levantar por un directorio que falta.
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True, check_dir=False), name="web")
