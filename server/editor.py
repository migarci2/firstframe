"""EDL: el montaje que decide la persona, separado de lo que genera el pipeline.

`scenes` es lo que el AgentLoop produce. La EDL es cómo se montan: en qué orden,
con qué recorte y cuáles entran. Son dos cosas distintas a propósito — si una
escena se relanza y vuelve con otra duración, el corte que hiciste sigue en pie.

Una entrada es {n, in, out, enabled}:
    n        número de escena
    in       segundo de entrada dentro de la escena (0 = desde el principio)
    out      segundo de salida, o null = "hasta el final, dure lo que dure"
    enabled  false = la escena sigue existiendo pero se queda fuera del montaje

Se guarda como un JSON por job, no como filas: es una LISTA ORDENADA y el orden
es justo el dato que importa. Una tabla con posiciones sería la misma lista con
más sitios donde desincronizarse.

Endpoints (todos bajo /api/jobs/{job_id}):
    GET  /edl        -> {job_id, edl, duration, source_seconds}
    PUT  /edl        -> guarda y devuelve lo mismo, ya normalizado
    POST /edl/reset  -> vuelve al orden y duraciones originales
    GET  /cut        -> el plan de montaje resuelto (para ffmpeg o para el player)

Enganche en server/app.py:
    from server import editor
    app.include_router(editor.router)

Autocomprobación:  .venv/bin/python -m server.editor
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server import db

router = APIRouter()

NOMINAL_SEC = 6.0     # lo que asumimos que durará una escena que aún no existe
MIN_CLIP = 0.4        # por debajo de esto un corte deja de ser un corte

_SCHEMA = """
CREATE TABLE IF NOT EXISTS edls (
    job_id     TEXT PRIMARY KEY,
    edl        TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""
_ready = False


def _ensure() -> None:
    """La tabla se crea aquí y no en db.SCHEMA para no tocar db.py."""
    global _ready
    if _ready:
        return
    db.conn().executescript(_SCHEMA)
    db.conn().commit()
    _ready = True


def _err(status: int, msg: str):
    return JSONResponse({"error": msg}, status_code=status)


# ---------------------------------------------------------------- normalizar

def _src_seconds(scene: dict | None) -> float:
    """Duración de origen. Una escena sin renderizar todavía ocupa la nominal:
    así se puede reordenar antes de que exista."""
    if not scene:
        return NOMINAL_SEC
    ms = scene.get("ms")
    if ms:
        return max(0.0, float(ms) / 1000.0)
    return NOMINAL_SEC


def _has_media(scene: dict | None) -> bool:
    return bool(scene and scene.get("ms") and scene.get("path"))


def normalize(edl: list, scenes: list[dict]) -> list[dict]:
    """Reconcilia una EDL con las escenas vivas. Misma lógica que el cliente
    (web/editor/ffeditor.js): conserva orden y cortes de lo que ya estaba, añade
    lo nuevo al final, tira lo que ya no existe y reajusta los cortes a la
    duración real. Es lo que permite editar MIENTRAS el pipeline trabaja."""
    by_n = {int(s["n"]): s for s in scenes}
    seen: set[int] = set()
    out: list[dict] = []

    for raw in edl or []:
        if not isinstance(raw, dict):
            continue
        try:
            n = int(raw.get("n"))
        except (TypeError, ValueError):
            continue
        if n not in by_n or n in seen:
            continue
        seen.add(n)

        dur = _src_seconds(by_n[n])
        try:
            tin = float(raw.get("in") or 0.0)
        except (TypeError, ValueError):
            tin = 0.0
        tin = min(max(0.0, tin), max(0.0, dur - MIN_CLIP))

        tout = raw.get("out")
        if tout is None:
            pass                       # "hasta el final": se resuelve al montar
        else:
            try:
                tout = min(max(float(tout), tin + MIN_CLIP), dur)
            except (TypeError, ValueError):
                tout = None

        out.append({
            "n": n,
            "in": round(tin, 3),
            "out": None if tout is None else round(tout, 3),
            "enabled": raw.get("enabled", True) is not False,
        })

    for s in scenes:                   # lo que ha aparecido después, al final
        n = int(s["n"])
        if n not in seen:
            out.append({"n": n, "in": 0.0, "out": None, "enabled": True})
    return out


def _out_of(entry: dict, scene: dict | None) -> float:
    return _src_seconds(scene) if entry.get("out") is None else float(entry["out"])


def duration(edl: list[dict], scenes: list[dict]) -> float:
    by_n = {int(s["n"]): s for s in scenes}
    total = 0.0
    for e in edl:
        if not e.get("enabled", True):
            continue
        total += max(0.0, _out_of(e, by_n.get(e["n"])) - float(e.get("in") or 0.0))
    return round(total, 3)


# ---------------------------------------------------------------- almacén

def get_edl(job_id: str) -> list[dict]:
    """La EDL guardada, ya reconciliada con las escenas de ahora mismo.
    Si nunca se guardó ninguna, devuelve el montaje natural."""
    _ensure()
    scenes = db.scenes(job_id)
    row = db._row("SELECT edl FROM edls WHERE job_id=?", (job_id,))
    stored: list = []
    if row:
        try:
            stored = json.loads(row["edl"])
        except (ValueError, TypeError):
            stored = []
    if not stored:
        return [{"n": int(s["n"]), "in": 0.0, "out": None, "enabled": True} for s in scenes]
    return normalize(stored, scenes)


def is_edited(job_id: str) -> bool:
    """¿El revisor tocó el montaje, o sigue siendo el orden natural?

    `server/assembler.py:concat_master()` lo consulta antes de aprobar: si nadie
    editó, concatena los segmentos tal cual (rápido, `-c copy`); si hay corte, lo
    materializa. Comparar contra el montaje natural en vez de mirar si existe fila
    en `edls` evita que un guardado que no cambió nada dispare un re-encode.
    """
    _ensure()
    scenes = db.scenes(job_id)
    natural = [{"n": int(s["n"]), "in": 0.0, "out": None, "enabled": True} for s in scenes]
    current = get_edl(job_id)
    if len(current) != len(natural):
        return True
    for a, b in zip(current, natural):
        if (a["n"] != b["n"] or a.get("enabled", True) is not True
                or float(a.get("in") or 0.0) != 0.0 or a.get("out") is not None):
            return True
    return False


def put_edl(job_id: str, edl: list) -> list[dict]:
    _ensure()
    clean = normalize(edl, db.scenes(job_id))
    db._exec(
        "INSERT INTO edls(job_id,edl,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(job_id) DO UPDATE SET edl=excluded.edl, updated_at=excluded.updated_at",
        (job_id, json.dumps(clean), db.now_ms()),
    )
    return clean


def clear_edl(job_id: str) -> list[dict]:
    _ensure()
    db._exec("DELETE FROM edls WHERE job_id=?", (job_id,))
    return get_edl(job_id)


# ---------------------------------------------------------------- el montaje

def cut_plan(job_id: str) -> dict:
    """La EDL resuelta a algo que ffmpeg (o el player) puede ejecutar: cada
    entrada con su fichero, su recorte y dónde cae en el montaje final.

    `ready` dice si el montaje se puede renderizar entero ahora mismo; si alguna
    escena activa aún no tiene media, se lista en `missing`."""
    scenes = {int(s["n"]): s for s in db.scenes(job_id)}
    edl = get_edl(job_id)
    clips, missing, t = [], [], 0.0

    for e in edl:
        if not e.get("enabled", True):
            continue
        s = scenes.get(e["n"])
        tin = float(e.get("in") or 0.0)
        tout = _out_of(e, s)
        length = max(0.0, tout - tin)
        if not _has_media(s):
            missing.append(e["n"])
        clips.append({
            "n": e["n"],
            "title": (s or {}).get("title"),
            "path": (s or {}).get("path"),
            "in": round(tin, 3),
            "out": round(tout, 3),
            "length": round(length, 3),
            "at": round(t, 3),          # dónde empieza en el montaje final
            "ready": _has_media(s),
        })
        t += length

    return {
        "job_id": job_id,
        "clips": clips,
        "duration": round(t, 3),
        "missing": missing,
        "ready": not missing and bool(clips),
    }


def ffmpeg_plan(job_id: str) -> list[list[str]]:
    """Los cortes que habría que ejecutar para materializar el montaje, uno por
    clip. El concat lo hace quien llame (server/assembler.py ya sabe).

    Se deja explícito y no se ejecuta desde aquí: renderizar es cosa del
    pipeline, este módulo solo decide QUÉ hay que renderizar."""
    plan = cut_plan(job_id)
    cmds = []
    for i, c in enumerate(plan["clips"]):
        if not c["ready"]:
            continue
        cmds.append([
            "ffmpeg", "-loglevel", "error", "-y",
            "-ss", f"{c['in']:.3f}", "-to", f"{c['out']:.3f}",
            "-i", c["path"],
            "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
            f"cut_{i:03d}_scene{c['n']}.mp4",
        ])
    return cmds


# ---------------------------------------------------------------- endpoints

def _payload(job_id: str, edl: list[dict]) -> dict:
    scenes = db.scenes(job_id)
    return {
        "job_id": job_id,
        "edl": edl,
        "duration": duration(edl, scenes),
        "source_seconds": round(sum(_src_seconds(s) for s in scenes), 3),
        "scene_count": len(scenes),
    }


@router.get("/api/jobs/{job_id}/edl")
def read_edl(job_id: str):
    if not db.get_job(job_id):
        return _err(404, "job not found")
    return _payload(job_id, get_edl(job_id))


@router.put("/api/jobs/{job_id}/edl")
async def write_edl(job_id: str, req: Request):
    if not db.get_job(job_id):
        return _err(404, "job not found")
    try:
        body = await req.json()
    except Exception:                     # noqa: BLE001
        return _err(400, "invalid json")
    edl = body.get("edl") if isinstance(body, dict) else body
    if not isinstance(edl, list):
        return _err(400, "edl must be a list")
    if len(edl) > 500:
        return _err(400, "edl too long")
    return _payload(job_id, put_edl(job_id, edl))


@router.post("/api/jobs/{job_id}/edl/reset")
def reset_edl(job_id: str):
    if not db.get_job(job_id):
        return _err(404, "job not found")
    return _payload(job_id, clear_edl(job_id))


@router.get("/api/jobs/{job_id}/cut")
def read_cut(job_id: str):
    if not db.get_job(job_id):
        return _err(404, "job not found")
    return cut_plan(job_id)


# ---------------------------------------------------------------- autocomprobación

def _selftest() -> None:
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t.db"
    db._local = __import__("threading").local()
    db._initialized = False
    db.init()

    db.create_job("j_ed", "brief", "T", 4)
    for n, ms in ((1, 5200), (2, 7800), (3, 3100)):
        db.update_scene("j_ed", n, status="ready", ms=ms, path=f"/tmp/s{n}.mp4")
    scenes = db.scenes("j_ed")
    assert len(scenes) == 4
    print("1. job con 4 escenas, 3 renderizadas (5.2 + 7.8 + 3.1) y la 4 pendiente")

    e = get_edl("j_ed")
    assert [x["n"] for x in e] == [1, 2, 3, 4]
    assert all(x["out"] is None for x in e)
    d0 = duration(e, scenes)
    assert abs(d0 - (5.2 + 7.8 + 3.1 + 6.0)) < 0.01, d0
    print(f"2. EDL natural sin guardar -> orden 1 2 3 4, {d0:.1f} s (la 4 vale la nominal)")

    # reordenar + recortar + saltar
    put_edl("j_ed", [
        {"n": 3, "in": 0, "out": None, "enabled": True},
        {"n": 1, "in": 1.2, "out": 4.0, "enabled": True},
        {"n": 2, "in": 0, "out": None, "enabled": False},
        {"n": 4, "in": 0, "out": None, "enabled": True},
    ])
    e = get_edl("j_ed")
    assert [x["n"] for x in e] == [3, 1, 2, 4], e
    assert e[2]["enabled"] is False
    d1 = duration(e, scenes)
    assert abs(d1 - (3.1 + 2.8 + 6.0)) < 0.01, d1
    print(f"3. reordenado 3 1 2 4, escena 1 recortada a 2.8 s, escena 2 fuera -> {d1:.1f} s")

    # el pipeline relanza la escena 1 y vuelve mas larga: el corte AGUANTA
    db.update_scene("j_ed", 1, ms=9000)
    e = get_edl("j_ed")
    one = [x for x in e if x["n"] == 1][0]
    assert one["in"] == 1.2 and one["out"] == 4.0, one
    # ...pero la 3, que nunca se toco, crece sola
    db.update_scene("j_ed", 3, ms=4500)
    e = get_edl("j_ed")
    assert [x for x in e if x["n"] == 3][0]["out"] is None
    assert abs(duration(e, db.scenes("j_ed")) - (4.5 + 2.8 + 6.0)) < 0.01
    print("4. escena 1 relanzada a 9 s: el corte 1.2->4.0 aguanta")
    print("   escena 3 relanzada a 4.5 s: como no se toco, crece sola -> 13.3 s")

    # el pipeline anade una escena
    db.update_scene("j_ed", 5, status="ready", ms=2000, path="/tmp/s5.mp4")
    e = get_edl("j_ed")
    assert [x["n"] for x in e] == [3, 1, 2, 4, 5], e
    print("5. aparece la escena 5 -> se anade al final sin tocar el montaje")

    # un cliente que manda basura no puede romper nada
    bad = put_edl("j_ed", [{"n": 1, "in": -50, "out": 999}, {"n": 99}, "nope", {"n": 1}])
    assert [x["n"] for x in bad] == [1, 2, 3, 4, 5], bad
    assert bad[0]["in"] == 0.0 and bad[0]["out"] == 9.0, bad[0]
    print("6. EDL con basura (escena inexistente, duplicada, in negativo, out fuera de rango)")
    print("   -> saneada: in 0, out 9.0, y el resto de escenas reincorporadas")

    plan = cut_plan("j_ed")
    assert plan["missing"] == [4], plan["missing"]
    assert plan["ready"] is False
    ats = [c["at"] for c in plan["clips"]]
    assert ats == sorted(ats)
    print(f"7. cut_plan -> {len(plan['clips'])} clips, {plan['duration']:.1f} s, "
          f"falta media en la escena {plan['missing']}")

    clear_edl("j_ed")
    assert [x["n"] for x in get_edl("j_ed")] == [1, 2, 3, 4, 5]
    print("8. reset -> vuelve al orden natural")
    print("\nOK — server/editor.py")


if __name__ == "__main__":
    _selftest()
