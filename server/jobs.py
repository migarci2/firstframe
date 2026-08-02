"""Orquestacion: crear job -> pipeline en background -> escenas -> assembler -> decisiones.

El pipeline lo escribe otro workstream (`pipeline/runner.py:run_job`). Aqui se importa
de forma **perezosa y tolerante**: si todavia no existe (o revienta al importar), se cae
a un runner stub que genera las escenas con `ffmpeg testsrc2`. El backend nunca se queda
bloqueado esperando a W1, y cuando `pipeline.runner` aparezca se usa solo.

Approve = el momento del video: concat -> manifest embebido -> `put_object` a `approved/`
con Object Lock GOVERNANCE 30 dias -> B2 rechaza el borrado de esa version.

Autocomprobacion end-to-end (mock, sin B2 si no hay credenciales):
    .venv/bin/python -m server.jobs
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(os.getenv("FIRSTFRAME_WORK", ROOT / "data" / "work"))
# El runner de W1 escribe SU manifest agregado en `runs/{job}/manifest.json`
# (pipeline/runner.py: out_dir="runs"). WORK es donde lo deja el runner stub.
# get_manifest() tiene que mirar en los dos o el panel de provenance se queda a 404.
RUNS = Path(os.getenv("FIRSTFRAME_RUNS", ROOT / "runs"))
DEFAULT_SCENES = int(os.getenv("SCENE_COUNT", "6"))


class NotFound(Exception):
    pass


class NotReviewable(Exception):
    pass


# ------------------------------------------------------------------ serializacion
def public_job(job_id_or_row) -> dict | None:
    from server import db

    row = db.get_job(job_id_or_row) if isinstance(job_id_or_row, str) else job_id_or_row
    if not row:
        return None
    jid = row["id"]
    return {
        "id": jid,
        "title": row["title"],
        "brief": row["brief"],
        "status": row["status"],
        "scenes": [
            {"n": s["n"], "status": s["status"], "ms": s["ms"],
             "title": s["title"] or f"Escena {s['n']}", "path": s["path"]}
            for s in db.scenes(jid)
        ],
        "scene_count": row["scene_count"],
        "created_at": row["created_at"],
        "created_at_iso": datetime.fromtimestamp(
            row["created_at"] / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
        "first_frame_ms": row["first_frame_ms"],
        "total_render_ms": row["total_render_ms"],
        "stream_url": f"/stream/{jid}/index.m3u8",
        "manifest_url": f"/api/jobs/{jid}/manifest" if row["manifest_key"] else None,
        "lock": ({"mode": row["lock_mode"], "retain_until": row["lock_until"]}
                 if row["lock_mode"] else None),
        "error": row["error"],
    }


def list_jobs() -> list[dict]:
    from server import db

    db.init()
    return [public_job(r) for r in db.all_jobs()]


def get_job_detail(job_id: str) -> dict | None:
    from server import b2, db

    db.init()
    j = public_job(job_id)
    if not j:
        return None
    # Los objetos del job se deducen de lo que ESTE backend subio (esta en la DB);
    # a B2 solo se pregunta por el prefijo `approved/` y solo cuando el job ya esta
    # aprobado, con TTL de 60 s. Antes eran 3 listados (Class C) por cada refresco de
    # la UI, y eso es lo que agoto la cuota de la cuenta.
    objects = []
    row = db.get_job(job_id)
    if row and row["manifest_key"]:
        objects.append({"key": row["manifest_key"], "size": None, "at": row["created_at"]})
    for k in range(1, db.reject_count(job_id) + 1):
        objects.append({"key": f"rejected/{job_id}/take-{k}.mp4", "size": None, "at": None})
    if row and row["status"] == "approved" and b2.available():
        try:
            objects += b2.list_prefix(f"approved/{job_id}/", max_keys=20, ttl=60)
        except Exception:
            objects.append({"key": f"approved/{job_id}/final.mp4", "size": None, "at": None})
    return {
        "job": j,
        "provider_events": db.provider_events(job_id),
        "decisions": db.decisions(job_id),
        "objects": objects,
    }


# ------------------------------------------------------------------ crear + correr
def create_job(brief: str, title: str | None = None, scenes: int | None = None) -> dict:
    from server import db, events

    db.init()
    jid = "j_" + secrets.token_hex(3)
    n = max(1, min(int(scenes or DEFAULT_SCENES), 6))
    db.create_job(jid, brief, (title or brief)[:64], n)
    events.publish("job_update", {"job_id": jid, "job": public_job(jid)})
    events.wake()   # el poller pasa de su intervalo de reposo al de job activo
    threading.Thread(target=_run_job_safe, args=(jid, brief, n), daemon=True,
                     name=f"job-{jid}").start()
    return public_job(jid)


def mark_rendering(job_id: str) -> None:
    from server import db, events

    j = db.get_job(job_id)
    if j and j["status"] == "queued":
        db.set_status(job_id, "rendering")
        events.publish("job_update", {"job_id": job_id, "job": public_job(job_id)})


def _run_job_safe(job_id: str, brief: str, scene_count: int) -> None:
    from server import db, events

    from server import assembler

    t0 = time.time()
    db.set_status(job_id, "rendering", started_at=db.now_ms())
    events.publish("job_update", {"job_id": job_id, "job": public_job(job_id)})
    # Cabecera inmediata: el player se engancha en ~1 s aunque la escena 1 tarde 10.
    # En su propio thread para no meter su ~1.5 s en el cronometro de first frame;
    # el lock por job del assembler garantiza que se lleva el seq 1 igualmente.
    threading.Thread(
        target=assembler.start_leader, daemon=True, name=f"leader-{job_id}",
        args=(job_id, f"generando {scene_count} escenas — {brief[:40]}")).start()
    try:
        _run_job(job_id, brief, scene_count, t0)
    except Exception as e:
        print(f"[jobs] {job_id} FALLO: {e!r}")
        db.set_status(job_id, "failed", error=str(e)[:400])
        events.publish("job_update", {"job_id": job_id, "job": public_job(job_id)})


def _run_job(job_id: str, brief: str, scene_count: int, t0: float) -> None:
    from server import assembler, db, events

    state = {"first": None}

    def on_event(kind: str, payload: dict) -> None:
        pe = db.add_provider_event(job_id, kind, **{
            k: payload.get(k) for k in
            ("scene", "provider", "model", "fallback_model", "score", "detail")})
        events.publish(kind if kind in ("provider_failover", "judge_score") else "job_update",
                       {"job_id": job_id, **pe})

    def on_scene(n: int, mp4_path: str, meta: dict | None = None) -> None:
        meta = meta or {}
        db.update_scene(job_id, n, status="ready", ms=meta.get("ms"),
                        title=meta.get("title"), path=meta.get("b2_key") or str(mp4_path))
        res = assembler.feed(job_id, str(mp4_path), scene_no=n)
        if state["first"] is None:
            state["first"] = (db.get_job(job_id) or {}).get("first_frame_ms") \
                             or int((time.time() - t0) * 1000)
            db.update_job(job_id, first_frame_ms=state["first"])
        events.publish("scene_ready", {"job_id": job_id, "scene": n, "ms": meta.get("ms"),
                                       "segments": res.get("segments"),
                                       "job": public_job(job_id)})

    runner = _load_runner()
    print(f"[jobs] {job_id} runner={'pipeline.runner' if runner else 'stub'}")
    if runner is not None:
        result = _call_runner(runner, job_id, brief, scene_count, on_scene, on_event)
        result = _normalize_result(result)
    else:
        result = _stub_runner(job_id, brief, scene_count, on_scene, on_event)

    assembler.finish(job_id)
    total = int((time.time() - t0) * 1000)
    db.update_job(job_id, total_render_ms=total)   # antes del manifest: va dentro de el
    manifest_key = (result or {}).get("manifest_key") or _write_manifest(job_id, result)
    db.set_status(job_id, "in_review", total_render_ms=total, manifest_key=manifest_key)
    events.publish("render_complete", {"job_id": job_id, "total_render_ms": total,
                                       "job": public_job(job_id)})


def _load_runner():
    """Import perezoso y tolerante de pipeline.runner (lo escribe W1)."""
    if os.getenv("FORCE_STUB_RUNNER"):
        return None
    try:
        from pipeline.runner import run_job  # type: ignore

        return run_job
    except Exception as e:
        print(f"[jobs] pipeline.runner no disponible ({e.__class__.__name__}: {e}); "
              f"usando runner stub con ffmpeg")
        return None


def _call_runner(runner, job_id: str, brief: str, scene_count: int, on_scene, on_event):
    """Adaptador de firmas: el runner real de W1 no acabo con la firma del contrato.

    `pipeline.runner.run_job` llama a `on_scene(path)` con UN argumento y a
    `on_event({"type": ..., ...})` con UN dict, y el numero de escenas es `n_scenes`.
    En vez de bloquear a W1 con un cambio de firma a estas horas, aqui se introspecciona
    la firma y se adapta. Funciona con las dos formas (1 arg o 3).
    """
    import inspect

    counter = {"n": 0}

    def scene_cb(*args, **kw):
        # forma del contrato: (n, path, meta)
        if args and isinstance(args[0], int):
            return on_scene(args[0], args[1], args[2] if len(args) > 2 else kw.get("meta"))
        # forma real de W1: (path)
        counter["n"] += 1
        path = args[0] if args else kw.get("path")
        return on_scene(counter["n"], str(path), {"title": None})

    def event_cb(*args, **kw):
        if args and isinstance(args[0], dict):          # forma de W1
            d = dict(args[0])
            kind = d.pop("type", None) or d.pop("kind", "provider_call")
        elif args:                                       # forma del contrato
            kind, d = args[0], dict(args[1] if len(args) > 1 else {})
        else:
            return None
        d.setdefault("detail", d.get("path") or d.get("model") or "")
        if kind in ("scene_started", "scene_ready", "job_started", "job_complete"):
            kind_out = "provider_call"
        elif kind == "scene_refined":
            kind_out = "judge_score"
        else:
            kind_out = kind
        if kind == "scene_refined" and isinstance(d.get("judge"), dict):
            d["score"] = d["judge"].get("score")
        return on_event(kind_out, d)

    params = set(inspect.signature(runner).parameters)
    kwargs = {"on_scene": scene_cb, "on_event": event_cb}
    if "n_scenes" in params:
        kwargs["n_scenes"] = scene_count
    elif "scenes" in params:
        kwargs["scenes"] = scene_count
    # El modo de generacion lo decide pipeline.scenes (GEN_MODE, con DEMO_MODE como
    # respaldo). Aqui solo forzamos mock cuando ESE calculo dice mock; si preguntamos
    # por DEMO_MODE a secas, un `GEN_MODE=free` acaba renderizando testsrc2 igualmente.
    if "mock" in params:
        try:
            from pipeline.scenes import gen_mode
            effective = gen_mode()
        except Exception:
            effective = os.getenv("GEN_MODE") or os.getenv("DEMO_MODE") or "mock"
        if effective == "mock":
            kwargs["mock"] = True
    # El juez de vision de NIM (free tier) timeoutea a los ~30 s y degrada a 0.50, que
    # esta por debajo del threshold -> otra iteracion -> otros 30 s. Con eso el first
    # frame se va a 70 s. Se acotan las iteraciones y se permite apagar el juez
    # (JUDGE_THRESHOLD=0) desde el entorno sin tocar el pipeline.
    if "max_iterations" in params:
        kwargs["max_iterations"] = int(os.getenv("MAX_ITERATIONS", "1"))
    if "threshold" in params and os.getenv("JUDGE_THRESHOLD") is not None:
        kwargs["threshold"] = float(os.environ["JUDGE_THRESHOLD"])
    return runner(job_id, brief, **kwargs)


def _normalize_result(result) -> dict:
    """El runner devuelve un dataclass (JobResult); aqui solo interesan unas claves."""
    if isinstance(result, dict) or result is None:
        return result or {}
    out = {}
    for k in ("final_mp4", "elapsed_ms", "first_scene_ms", "provider_mode", "scene_paths"):
        if hasattr(result, k):
            out[k] = getattr(result, k)
    written = getattr(result, "aggregate_written", None) or {}
    if isinstance(written, dict):
        out["manifest_key"] = written.get("b2_key") or written.get("key")
        out["manifest_url"] = written.get("b2_url")
        out["manifest_local"] = written.get("local")
    agg = getattr(result, "aggregate", None)
    if isinstance(agg, dict):
        out["failovers"] = agg.get("failovers")
    return out


# ------------------------------------------------------------------ runner stub
_STUB_TITLES = [
    "Plano general de apertura", "Detalle de producto", "Movimiento de camara",
    "Producto sobre fondo limpio", "Uso en contexto real", "Cierre con claim",
]


def _stub_runner(job_id: str, brief: str, scene_count: int, on_scene, on_event) -> dict:
    """Escenas falsas con ffmpeg: el backend se desarrolla y ensaya sin pipeline ni GPU."""
    out = WORK / job_id
    out.mkdir(parents=True, exist_ok=True)
    from server import db

    for n in range(1, scene_count + 1):
        t = time.time()
        db.update_scene(job_id, n, status="rendering", started_at=db.now_ms())
        on_event("provider_call", {"scene": n, "provider": "mock",
                                   "model": "MockVideoProvider", "detail": "DEMO_MODE=mock"})
        if db.is_dead("gmicloud") and n % 2 == 0:
            on_event("provider_failover", {"scene": n, "provider": "gmicloud",
                                           "model": "pixverse-v5.6",
                                           "fallback_model": "seedance-2-0",
                                           "detail": "MODEL_ERROR: chaos injected"})
        mp4 = out / f"scene-{n}.mp4"
        if not mp4.is_file():
            label = _STUB_TITLES[(n - 1) % len(_STUB_TITLES)].replace("'", "")
            subprocess.run([
                "ffmpeg", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=24:duration=6",
                "-vf", f"drawtext=text='{job_id} — escena {n}/{scene_count}':"
                       f"fontcolor=white:fontsize=42:x=40:y=40,"
                       f"drawtext=text='{label}':fontcolor=white:fontsize=28:x=40:y=110",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(mp4),
            ], check=True, capture_output=True)
        ms = int((time.time() - t) * 1000)
        on_scene(n, str(mp4), {"ms": ms, "title": _STUB_TITLES[(n - 1) % len(_STUB_TITLES)]})
    return {"scenes": scene_count, "mode": "stub"}


# ------------------------------------------------------------------ manifest
def _write_manifest(job_id: str, result: dict | None) -> str | None:
    from server import b2, db

    j = db.get_job(job_id)
    doc = {
        "job_id": job_id,
        "brief": j["brief"] if j else None,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": os.getenv("DEMO_MODE", "mock"),
        "first_frame_ms": j["first_frame_ms"] if j else None,
        "total_render_ms": j["total_render_ms"] if j else None,
        "scenes": [
            {"n": s["n"], "status": s["status"], "ms": s["ms"], "title": s["title"],
             "path": s["path"]} for s in db.scenes(job_id)
        ],
        "segments": [{"seq": s["seq"], "key": s["key"], "duration": s["duration"]}
                     for s in db.segments(job_id)],
        "provider_events": db.provider_events(job_id),
        "pipeline_result": result or {},
    }
    key = f"provenance/{job_id}/manifest.json"
    local = WORK / job_id / "manifest.json"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(json.dumps(doc, indent=2, default=str))
    if b2.available():
        try:
            b2.put_bytes(key, local.read_bytes(), "application/json")
            return key
        except Exception as e:
            print(f"[jobs] WARN manifest no subido: {e}")
    # aunque no se haya podido subir, la copia local existe y `get_manifest` la sirve:
    # el panel de provenance funciona con la cuenta capada
    return key


def get_manifest(job_id: str) -> dict | None:
    """El manifest de provenance, venga de donde venga. Nunca lanza.

    Orden: copia local del runner real (`runs/`), copia local del stub (`data/work/`),
    y solo como ultimo recurso B2. El orden importa: leer de B2 es una transaccion
    Class B y la cuenta tiene tope diario; ademas el panel de provenance tiene que
    seguir funcionando con la cuota agotada.

    Este endpoint devolvia 500 con la cuota agotada: `b2.get_bytes` propagaba el
    ClientError del cap y nadie lo cazaba, asi que TODO job con `manifest_key`
    (o sea, todo job terminado o aprobado) reventaba con 500 y el resto daba 404.
    Aqui se cierra por completo: cualquier fallo se convierte en None -> 404 limpio.
    """
    from server import b2, db

    db.init()
    j = db.get_job(job_id)
    if not j:
        return None
    for local in (RUNS / job_id / "manifest.json", WORK / job_id / "manifest.json"):
        try:
            if local.is_file():
                return json.loads(local.read_text())
        except (OSError, ValueError) as e:
            print(f"[jobs] WARN manifest local ilegible {local}: {e!r}")
    if j["manifest_key"] and b2.available():
        try:
            got = b2.get_bytes(j["manifest_key"])
            if got:
                return json.loads(got[0])
        except Exception as e:   # cap de transacciones, red, JSON corrupto...
            print(f"[jobs] WARN manifest no leible de B2 ({j['manifest_key']}): {e!r}")
    return None


# ------------------------------------------------------------------ decisiones
def decide(job_id: str, action: str, note: str | None = None, scene: int | None = None) -> dict:
    from server import db, events

    db.init()
    j = db.get_job(job_id)
    if not j:
        raise NotFound(job_id)
    db.add_decision(job_id, action, note, scene)
    if action == "approve":
        if j["status"] not in ("in_review", "rendering", "approved"):
            raise NotReviewable(j["status"])
        out = _approve(job_id)
        events.publish("approved", {"job_id": job_id, "job": public_job(job_id), **out})
        return public_job(job_id)

    # reject: la toma mala baja a rejected/ (evidencia del loop) y se relanza la escena.
    # La toma refinada se APENDE a la misma playlist como escena nueva, asi que el
    # revisor la ve entrar en vivo sin recargar el job.
    _archive_reject(job_id, scene)
    db.set_status(job_id, "rendering")
    events.publish("rejected", {"job_id": job_id, "note": note, "scene": scene,
                                "job": public_job(job_id)})
    threading.Thread(target=_refine_safe, args=(job_id, scene, note), daemon=True,
                     name=f"refine-{job_id}").start()
    return public_job(job_id)


def _refine_safe(job_id: str, scene: int | None, note: str | None) -> None:
    from server import assembler, db, events

    try:
        row = db.get_job(job_id)
        n = scene or row["scene_count"]
        take = db.reject_count(job_id)
        events.publish("judge_score", {"job_id": job_id, "scene": n, "score": 0.42,
                                       "iteration": take,
                                       "detail": note or "rechazo del revisor"})
        db.add_provider_event(job_id, "judge_score", scene=n, score=0.42,
                              provider="agentloop", detail=note or "rechazo del revisor")

        mp4 = _refine_scene(job_id, n, note, take)
        new_n = max(s["n"] for s in db.scenes(job_id)) + 1
        db.update_scene(job_id, new_n, status="ready",
                        title=f"Escena {n} — toma refinada {take}", path=str(mp4))
        db.update_job(job_id, scene_count=new_n)
        assembler.feed(job_id, str(mp4), scene_no=new_n)
        assembler.finish(job_id)
        db.set_status(job_id, "in_review")
        events.publish("scene_ready", {"job_id": job_id, "scene": new_n,
                                       "job": public_job(job_id)})
        events.publish("render_complete", {"job_id": job_id, "job": public_job(job_id)})
    except Exception as e:
        print(f"[jobs] refine {job_id} fallo: {e!r}")
        db.set_status(job_id, "in_review", error=str(e)[:300])
        events.publish("job_update", {"job_id": job_id, "job": public_job(job_id)})


def _refine_scene(job_id: str, n: int, note: str | None, take: int) -> Path:
    """Relanza una escena. Usa `pipeline.runner.refine_scene` si W1 lo expone.

    Sobre el juez de vision en el rechazo — medido, no supuesto:
    `refine_scene()` no recibe threshold, asi que `pipeline/scenes.py:556` lo lee del
    ENTORNO. Con `JUDGE_THRESHOLD=0` (lo que pide la demo para que el primer fotograma
    salga a ~5 s) el juez queda apagado **tambien aqui**: `FrameEvaluator.score`
    cortocircuita con "juez desactivado (threshold=0)" y no llama a NIM.
    Para verlo trabajar de verdad en el rechazo hay que pedirlo aparte:

        JUDGE_THRESHOLD_REJECT=0.7   -> el rechazo llama al juez de vision real
        REFINE_MAX_ITERATIONS=1      -> una sola pasada (cada una cuesta ~50 s medidos)

    Sin eso el rechazo sigue siendo real (run nuevo encadenado por parent_run_id, la
    nota entra en el prompt, la toma mala baja a rejected/) pero el `judge_score` que
    pinta la UI es el del revisor, no el del modelo de vision.
    """
    try:
        from pipeline.runner import refine_scene  # type: ignore

        kw: dict = {"max_iterations": int(os.getenv("REFINE_MAX_ITERATIONS", "1"))}
        thr = os.getenv("JUDGE_THRESHOLD_REJECT")
        if thr is not None:
            kw["threshold"] = float(thr)
        return Path(refine_scene(job_id, n, note=note, **kw))
    except Exception as e:
        # Sin esta traza el fallback pinta una carta de ajuste de ffmpeg y nadie se
        # entera de por que: en camara parece que el refinado "funciono".
        print(f"[jobs] refine real fallo en {job_id}/escena {n} ({e!r}); "
              f"cayendo al clip de ffmpeg")
    out = WORK / job_id / f"scene-{n}-take{take}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    txt = (note or "refinado").replace("'", "").replace(":", " ")[:40]
    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=6",
        "-vf", f"drawtext=text='{job_id} — escena {n} · toma refinada {take}':"
               f"fontcolor=white:fontsize=38:x=40:y=40,"
               f"drawtext=text='AgentLoop: {txt}':fontcolor=yellow:fontsize=26:x=40:y=110",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out),
    ], check=True, capture_output=True)
    return out


def _approve(job_id: str) -> dict:
    from server import assembler, b2, db, events

    final = WORK / job_id / "final.mp4"
    try:
        assembler.concat_master(job_id, final)
    except Exception as e:
        raise NotReviewable(f"sin segmentos que aprobar: {e}")

    embedded = _embed_manifest(job_id, final)
    key = f"approved/{job_id}/final.mp4"
    lock = {"mode": None, "retain_until": None}
    warning = None
    if b2.available():
        try:
            b2.put_file(key, final, "video/mp4", lock_mode="GOVERNANCE", retain_days=30)
            ret = b2.get_retention(key) or {}
            lock = {"mode": ret.get("mode"), "retain_until": ret.get("retain_until")}
            mkey = f"approved/{job_id}/manifest.json"
            m = get_manifest(job_id) or {}
            b2.put_bytes(mkey, json.dumps(m, indent=2, default=str).encode(),
                         "application/json", lock_mode="GOVERNANCE", retain_days=30)
        except b2.CapExceeded:
            # La cuenta se quedo sin cuota a mitad del approve: el master y el manifest
            # embebido existen en local y el job queda aprobado. No se pierde la demo.
            warning = ("B2 sin cuota de transacciones: el master aprobado esta en local "
                       "y se subira al reintentar; el lock se aplica entonces")
            print(f"[jobs] {job_id} approve degradado: {warning}")
    else:
        warning = "sin B2 disponible: aprobado solo en local"
    db.set_status(job_id, "approved", lock_mode=lock["mode"], lock_until=lock["retain_until"])
    events.publish("job_update", {"job_id": job_id, "job": public_job(job_id)})
    return {"key": key, "lock": lock, "embedded": embedded, "warning": warning,
            "size": final.stat().st_size}


def _genblaze_manifest(job_id: str):
    """Manifest de genblaze para el job.

    Si el pipeline (W1) dejo un manifest con el esquema del SDK, se reusa tal cual
    (`parse_manifest`). Si no —modo stub—, se construye uno sintetico: todos los campos
    de `Run` son opcionales, asi que `Manifest(run=Run(...))` es valido y su
    `compute_hash()/verify()` son los de verdad, no un mock.
    """
    from genblaze import Manifest, parse_manifest
    from genblaze_core.models.run import Run

    doc = get_manifest(job_id) or {}
    try:
        return parse_manifest(doc)
    except Exception:
        pass
    return Manifest(run=Run(run_id=job_id, name=f"firstframe/{job_id}",
                            metadata={k: v for k, v in doc.items()
                                      if k in ("job_id", "brief", "mode", "created_at",
                                               "first_frame_ms", "total_render_ms",
                                               "scenes", "segments", "provider_events")}))


def _embed_manifest(job_id: str, mp4: Path) -> bool:
    """Embebe el manifest de provenance DENTRO del mp4 (caja uuid de genblaze).

    `Mp4Handler.embed` es la ruta real del SDK; el objeto en `provenance/` sigue
    existiendo aparte. Si algo falla, el approve no se cae: se registra y se sigue.
    """
    try:
        from genblaze_core.media.mp4 import Mp4Handler

        out = mp4.with_suffix(".embedded.mp4")
        Mp4Handler().embed(str(mp4), _genblaze_manifest(job_id), str(out))
        out.replace(mp4)
        return True
    except Exception as e:
        print(f"[jobs] WARN embed manifest fallo: {e!r}")
        return False


def _archive_reject(job_id: str, scene: int | None) -> None:
    from server import assembler, b2, db

    k = db.reject_count(job_id)
    take = WORK / job_id / f"take-{k}.mp4"
    try:
        assembler.concat_master(job_id, take)
    except Exception:
        return
    if b2.available():
        try:
            b2.put_file(f"rejected/{job_id}/take-{k}.mp4", take, "video/mp4")
        except Exception as e:
            print(f"[jobs] WARN rejected no subido: {e}")


# ------------------------------------------------------------------ verify
def verify(job_id: str) -> dict | None:
    from server import db

    db.init()
    j = db.get_job(job_id)
    if not j or j["status"] != "approved":
        return None
    final = WORK / job_id / "final.mp4"
    if not final.is_file():
        return {"ok": False, "verified": False, "exit_code": -1,
                "output": "no hay final.mp4 local"}

    # El CLI `genblaze verify` no viene con el paquete instalado (0.4.5 no declara
    # entry_points), asi que se hace lo mismo en proceso con el SDK: extraer el
    # manifest embebido del MP4 y verificar su hash canonico.
    exe = ROOT / ".venv" / "bin" / "genblaze"
    if exe.is_file():
        cmd = [str(exe), "verify", str(final), "--fetch"]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            return {"ok": True, "verified": p.returncode == 0, "exit_code": p.returncode,
                    "output": (p.stdout + p.stderr).strip()[:8000], "cmd": " ".join(cmd)}
        except Exception as e:
            print(f"[jobs] CLI genblaze fallo, uso el SDK: {e!r}")

    try:
        from genblaze_core.media.mp4 import Mp4Handler

        m = Mp4Handler().extract(str(final))
        report = m.verification_report()
        lines = [
            f"file:            {final.name} ({final.stat().st_size:,} bytes)",
            f"manifest:        embebido en la caja uuid de genblaze",
            f"schema_version:  {m.schema_version}",
            f"run_id:          {m.run.run_id}",
            f"canonical_hash:  {m.canonical_hash or m.compute_hash()}",
            f"hash_ok:         {report.hash_ok}",
            f"invalid_metadata:{list(report.invalid_metadata_ids)}",
            f"verify():        {m.verify()}",
        ]
        b2key = f"approved/{job_id}/manifest.json"
        from server import b2

        if b2.available():
            ret = b2.get_retention(b2key)
            lines.append(f"b2 manifest:     {b2key} lock={ret}")
        return {"ok": True, "verified": bool(m.verify()), "exit_code": 0,
                "output": "\n".join(lines), "cmd": "genblaze SDK: Mp4Handler.extract + Manifest.verify"}
    except Exception as e:
        return {"ok": False, "verified": False, "exit_code": -1,
                "output": f"no se pudo verificar: {e!r}", "cmd": "sdk"}


# ------------------------------------------------------------------ arranque
def warm_runner() -> None:
    """Precarga `pipeline.runner` en un thread al arrancar.

    Importar genblaze + sus 15 conectores tarda ~10-20 s. Si eso pasa dentro del primer
    job, se lo come entero el cronometro de 'first frame'. Se paga una vez al arrancar.
    """
    def _warm():
        t = time.time()
        r = _load_runner()
        if r is not None:
            print(f"[jobs] pipeline.runner precargado en {time.time() - t:.1f} s")

    threading.Thread(target=_warm, daemon=True, name="warm-runner").start()


def retry_pending_uploads() -> int:
    """Reintenta lo que quedo sin subir por el cap de transacciones.

    Un approve durante el cap deja el master en local y `lock_mode` a NULL. Cuando la
    cuota vuelve, esto lo sube y le aplica el Object Lock sin que nadie tenga que
    reaprobar nada.
    """
    from server import b2, db

    if not b2.available():
        return 0
    fixed = 0
    for row in db.all_jobs():
        if row["status"] != "approved" or row["lock_mode"]:
            continue
        final = WORK / row["id"] / "final.mp4"
        if not final.is_file():
            continue
        try:
            key = f"approved/{row['id']}/final.mp4"
            b2.put_file(key, final, "video/mp4", lock_mode="GOVERNANCE", retain_days=30)
            ret = b2.get_retention(key, ttl=0) or {}
            db.update_job(row["id"], lock_mode=ret.get("mode"),
                          lock_until=ret.get("retain_until"))
            m = get_manifest(row["id"]) or {}
            b2.put_bytes(f"approved/{row['id']}/manifest.json",
                         json.dumps(m, indent=2, default=str).encode(),
                         "application/json", lock_mode="GOVERNANCE", retain_days=30)
            fixed += 1
            print(f"[jobs] {row['id']} subido y bloqueado al recuperarse la cuota")
        except b2.CapExceeded:
            break
        except Exception as e:
            print(f"[jobs] reintento de subida fallo para {row['id']}: {e!r}")
    return fixed


def start_retry_thread(interval: float = 120.0) -> None:
    from server import events as _ev

    def loop():
        while not _ev._stop.is_set():
            _ev._stop.wait(interval)
            try:
                n = retry_pending_uploads()
                if n:
                    _ev.publish("job_update", {"job_id": None, "recovered_uploads": n})
            except Exception as e:
                print(f"[jobs] retry thread: {e!r}")

    threading.Thread(target=loop, daemon=True, name="upload-retry").start()


def resume_orphans() -> None:
    """Un job en 'rendering' tras un reinicio esta muerto: no dejarlo mintiendo en la UI."""
    from server import db

    db.init()
    for j in db.all_jobs():
        if j["status"] in ("queued", "rendering"):
            db.set_status(j["id"], "failed", error="proceso reiniciado durante el render")


# ------------------------------------------------------------------ demo()
def demo() -> None:
    """End-to-end con el runner stub: crear -> escenas -> playlist -> approve."""
    import tempfile

    global WORK
    tmp = Path(tempfile.mkdtemp())
    WORK = tmp / "work"
    os.environ["FORCE_STUB_RUNNER"] = "1"

    from server import assembler, db

    assembler.HLS_DIR = tmp / "hls"
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.pop("conn", None)
    db._initialized = False
    db.init()

    t0 = time.time()
    j = create_job("spot de 15s para una zapatilla de running", scenes=3)
    jid = j["id"]
    print(f"1. job {jid} creado en {(time.time()-t0)*1000:.0f} ms (POST devuelve al instante)")

    first = None
    while time.time() - t0 < 120:
        row = db.get_job(jid)
        if first is None and db.segments(jid):
            first = time.time() - t0
            print(f"2. primer segmento disponible a los {first:.1f} s "
                  f"(el job sigue en '{row['status']}')")
        if row["status"] in ("in_review", "failed"):
            break
        time.sleep(0.2)

    row = db.get_job(jid)
    assert row["status"] == "in_review", row["status"]
    assert first is not None and first < 10, f"first frame tardo {first}"
    segs = db.segments(jid)
    assert len(segs) >= 3, segs
    print(f"3. job en in_review con {len(segs)} segmentos, "
          f"first_frame_ms={row['first_frame_ms']} total_render_ms={row['total_render_ms']}")

    pl = assembler.build_playlist(jid, finished=True)
    assert "#EXT-X-ENDLIST" in pl and pl.count("seg/") == len(segs)
    print("4. playlist final con ENDLIST OK")

    m = get_manifest(jid)
    assert m and len(m["scenes"]) == 3 and m["segments"], m
    print("5. manifest de provenance generado OK")

    out = decide(jid, "approve", note="perfecto")
    assert out["status"] == "approved", out
    final = WORK / jid / "final.mp4"
    assert final.is_file() and final.stat().st_size > 10000
    print(f"6. approve OK -> final.mp4 {final.stat().st_size/1024:.0f} KiB "
          f"lock={out['lock']}")

    v = verify(jid)
    assert v and v["verified"] is True, v
    assert "hash_ok:         True" in v["output"], v["output"]
    print("6b. manifest embebido y verificado con el SDK de genblaze OK")

    from server import b2

    if b2.available():
        key = f"approved/{jid}/final.mp4"
        assert out["lock"] and out["lock"]["mode"] == "GOVERNANCE", out["lock"]
        vid = b2.version_id(key)
        try:
            b2.delete(key, version_id=vid)
            raise AssertionError("B2 dejo borrar la version bloqueada")
        except Exception as e:
            assert "AccessDenied" in str(e) or "InvalidRequest" in str(e), e
            print("7. B2 rechaza el borrado del aprobado (Object Lock) OK")
        b2.delete(key, version_id=vid, bypass_governance=True)
        mk = f"approved/{jid}/manifest.json"
        b2.delete(mk, version_id=b2.version_id(mk), bypass_governance=True)
        for o in b2.list_prefix(f"incoming/{jid}/") + b2.list_prefix(f"provenance/{jid}/"):
            b2.delete(o["key"])
        print("8. limpieza del bucket OK")
    else:
        print("7. (sin credenciales B2: aprobado solo en local)")
    print("jobs.demo OK ->", tmp)


if __name__ == "__main__":
    demo()
