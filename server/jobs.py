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
    objects = []
    if b2.available():
        try:
            for pref in (f"approved/{job_id}/", f"provenance/{job_id}/", f"rejected/{job_id}/"):
                objects += b2.list_prefix(pref, max_keys=20)
        except Exception:
            pass
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

    t0 = time.time()
    db.set_status(job_id, "rendering", started_at=db.now_ms())
    events.publish("job_update", {"job_id": job_id, "job": public_job(job_id)})
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
    print(f"[jobs] {job_id} runner={runner.__name__ if runner else 'stub'}")
    if runner is not None:
        result = runner(job_id, brief, scenes=scene_count, on_scene=on_scene, on_event=on_event)
    else:
        result = _stub_runner(job_id, brief, scene_count, on_scene, on_event)

    assembler.finish(job_id)
    total = int((time.time() - t0) * 1000)
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
    return key if b2.available() else None


def get_manifest(job_id: str) -> dict | None:
    from server import b2, db

    db.init()
    j = db.get_job(job_id)
    if not j:
        return None
    local = WORK / job_id / "manifest.json"
    if local.is_file():
        return json.loads(local.read_text())
    if j["manifest_key"] and b2.available():
        got = b2.get_bytes(j["manifest_key"])
        if got:
            return json.loads(got[0])
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

    # reject: la toma mala baja a rejected/ (evidencia del loop) y se relanza la escena
    _archive_reject(job_id, scene)
    db.set_status(job_id, "rejected")
    events.publish("rejected", {"job_id": job_id, "note": note, "scene": scene,
                                "job": public_job(job_id)})
    return public_job(job_id)


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
    if b2.available():
        b2.put_file(key, final, "video/mp4", lock_mode="GOVERNANCE", retain_days=30)
        ret = b2.get_retention(key) or {}
        lock = {"mode": ret.get("mode"), "retain_until": ret.get("retain_until")}
        mkey = f"approved/{job_id}/manifest.json"
        m = get_manifest(job_id) or {}
        b2.put_bytes(mkey, json.dumps(m, indent=2, default=str).encode(),
                     "application/json", lock_mode="GOVERNANCE", retain_days=30)
    db.set_status(job_id, "approved", lock_mode=lock["mode"], lock_until=lock["retain_until"])
    events.publish("job_update", {"job_id": job_id, "job": public_job(job_id)})
    return {"key": key, "lock": lock, "embedded": embedded,
            "size": final.stat().st_size}


def _embed_manifest(job_id: str, mp4: Path) -> bool:
    """Embebe el manifest en el MP4 con las utilidades de genblaze si estan disponibles.

    Si el helper no existe en esta version del SDK, se sigue adelante: el manifest ya
    esta como objeto en `provenance/` y el approve no se cae por esto.
    """
    doc = get_manifest(job_id) or {}
    try:
        from genblaze_provenance import SmartEmbedder  # type: ignore

        SmartEmbedder().embed(str(mp4), doc)
        return True
    except Exception:
        pass
    try:
        from genblaze_provenance.handlers import Mp4Handler  # type: ignore

        Mp4Handler().embed(str(mp4), json.dumps(doc, default=str))
        return True
    except Exception:
        pass
    # fallback honesto: metadata de comentario con el manifest (ffmpeg)
    try:
        tmp = mp4.with_suffix(".embed.mp4")
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(mp4),
                        "-c", "copy", "-movflags", "use_metadata_tags",
                        "-metadata", f"firstframe_manifest={json.dumps(doc, default=str)[:8000]}",
                        str(tmp)], check=True, capture_output=True)
        tmp.replace(mp4)
        return True
    except Exception as e:
        print(f"[jobs] WARN embed manifest fallo: {e}")
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
    exe = ROOT / ".venv" / "bin" / "genblaze"
    cmd = [str(exe) if exe.is_file() else "genblaze", "verify", str(final), "--fetch"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        out = (p.stdout + p.stderr).strip()
        return {"ok": True, "verified": p.returncode == 0, "exit_code": p.returncode,
                "output": out[:8000], "cmd": " ".join(cmd)}
    except Exception as e:
        return {"ok": False, "verified": False, "exit_code": -1, "output": str(e),
                "cmd": " ".join(cmd)}


# ------------------------------------------------------------------ arranque
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
