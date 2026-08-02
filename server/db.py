"""Estado en sqlite3 de stdlib. Sin ORM.

Un solo fichero (`data/firstframe.db`), WAL, conexiones por thread.
La idempotencia de los webhooks de B2 sale de `INSERT OR IGNORE` sobre `events(event_id)`:
B2 entrega at-least-once, asi que el mismo eventId puede llegar 3 veces y solo la primera
devuelve rowcount=1.

Autocomprobacion:  .venv/bin/python -m server.db
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("FIRSTFRAME_DB", ROOT / "data" / "firstframe.db"))

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    title           TEXT,
    brief           TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',
    scene_count     INTEGER NOT NULL DEFAULT 6,
    created_at      INTEGER NOT NULL,
    started_at      INTEGER,
    first_frame_ms  INTEGER,
    total_render_ms INTEGER,
    manifest_key    TEXT,
    lock_mode       TEXT,
    lock_until      TEXT,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS scenes (
    job_id   TEXT NOT NULL,
    n        INTEGER NOT NULL,
    status   TEXT NOT NULL DEFAULT 'pending',
    title    TEXT,
    path     TEXT,
    ms       INTEGER,
    started_at INTEGER,
    PRIMARY KEY (job_id, n)
);

-- event_id es la clave de idempotencia: B2 entrega at-least-once.
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    source     TEXT NOT NULL,          -- webhook | poll | internal
    kind       TEXT,
    job_id     TEXT,
    key        TEXT,
    payload    TEXT,
    at         INTEGER NOT NULL,
    processed  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, at);

CREATE TABLE IF NOT EXISTS decisions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL,
    action   TEXT NOT NULL,
    note     TEXT,
    scene    INTEGER,
    at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_job ON decisions(job_id, at);

CREATE TABLE IF NOT EXISTS provider_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id   TEXT NOT NULL,
    scene    INTEGER,
    kind     TEXT NOT NULL,
    provider TEXT,
    model    TEXT,
    fallback_model TEXT,
    score    REAL,
    detail   TEXT,
    at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS pev_job ON provider_events(job_id, at);

CREATE TABLE IF NOT EXISTS segments (
    job_id   TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    name     TEXT NOT NULL,
    duration REAL NOT NULL,
    scene    INTEGER,
    key      TEXT,
    at       INTEGER NOT NULL,
    PRIMARY KEY (job_id, seq)
);

CREATE TABLE IF NOT EXISTS chaos (
    provider TEXT PRIMARY KEY,
    dead     INTEGER NOT NULL DEFAULT 0,
    at       INTEGER NOT NULL
);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), timeout=15, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=15000")
        _local.conn = c
    return c


def init() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        conn().executescript(SCHEMA)
        conn().commit()
        _initialized = True


def now_ms() -> int:
    return int(time.time() * 1000)


def _rows(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def _row(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def _exec(sql, args=()):
    cur = conn().execute(sql, args)
    conn().commit()
    return cur


# ------------------------------------------------------------------ jobs
def create_job(job_id: str, brief: str, title: str, scene_count: int) -> dict:
    _exec(
        "INSERT INTO jobs(id,title,brief,status,scene_count,created_at) VALUES(?,?,?,?,?,?)",
        (job_id, title, brief, "queued", scene_count, now_ms()),
    )
    for n in range(1, scene_count + 1):
        _exec("INSERT OR IGNORE INTO scenes(job_id,n,status) VALUES(?,?,'pending')", (job_id, n))
    return get_job(job_id)


def get_job(job_id: str) -> dict | None:
    return _row("SELECT * FROM jobs WHERE id=?", (job_id,))


def all_jobs() -> list[dict]:
    return _rows("SELECT * FROM jobs ORDER BY created_at DESC")


def update_job(job_id: str, **fields) -> dict | None:
    if not fields:
        return get_job(job_id)
    cols = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
    return get_job(job_id)


def set_status(job_id: str, status: str, **fields) -> dict | None:
    return update_job(job_id, status=status, **fields)


# ------------------------------------------------------------------ scenes
def scenes(job_id: str) -> list[dict]:
    return _rows("SELECT * FROM scenes WHERE job_id=? ORDER BY n", (job_id,))


def update_scene(job_id: str, n: int, **fields) -> None:
    _exec("INSERT OR IGNORE INTO scenes(job_id,n,status) VALUES(?,?,'pending')", (job_id, n))
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        _exec(f"UPDATE scenes SET {cols} WHERE job_id=? AND n=?", (*fields.values(), job_id, n))


# ------------------------------------------------------------------ events
def record_event(event_id: str, source: str, kind: str | None = None,
                 job_id: str | None = None, key: str | None = None,
                 payload: dict | None = None) -> bool:
    """True si es NUEVO (hay que procesarlo), False si ya lo habiamos visto.

    Esta es LA garantia de idempotencia frente al at-least-once de B2.
    """
    cur = _exec(
        "INSERT OR IGNORE INTO events(event_id,source,kind,job_id,key,payload,at) "
        "VALUES(?,?,?,?,?,?,?)",
        (event_id, source, kind, job_id, key, json.dumps(payload or {}), now_ms()),
    )
    return cur.rowcount == 1


def mark_processed(event_id: str) -> None:
    _exec("UPDATE events SET processed=1 WHERE event_id=?", (event_id,))


def recent_events(job_id: str | None = None, limit: int = 100) -> list[dict]:
    if job_id:
        return _rows("SELECT * FROM events WHERE job_id=? ORDER BY at DESC LIMIT ?",
                     (job_id, limit))
    return _rows("SELECT * FROM events ORDER BY at DESC LIMIT ?", (limit,))


def seen_key(key: str) -> bool:
    return _row("SELECT 1 AS x FROM events WHERE key=? LIMIT 1", (key,)) is not None


# ------------------------------------------------------------------ decisions
def add_decision(job_id: str, action: str, note: str | None, scene: int | None) -> None:
    _exec("INSERT INTO decisions(job_id,action,note,scene,at) VALUES(?,?,?,?,?)",
          (job_id, action, note, scene, now_ms()))


def decisions(job_id: str) -> list[dict]:
    return _rows("SELECT action,note,scene,at FROM decisions WHERE job_id=? ORDER BY at",
                 (job_id,))


def reject_count(job_id: str) -> int:
    r = _row("SELECT COUNT(*) AS c FROM decisions WHERE job_id=? AND action='reject'", (job_id,))
    return r["c"] if r else 0


# ------------------------------------------------------------------ provider events
def add_provider_event(job_id: str, kind: str, **f) -> dict:
    at = now_ms()
    _exec(
        "INSERT INTO provider_events(job_id,scene,kind,provider,model,fallback_model,"
        "score,detail,at) VALUES(?,?,?,?,?,?,?,?,?)",
        (job_id, f.get("scene"), kind, f.get("provider"), f.get("model"),
         f.get("fallback_model"), f.get("score"), f.get("detail"), at),
    )
    return {"job_id": job_id, "kind": kind, "at": at, **f}


def provider_events(job_id: str) -> list[dict]:
    return _rows("SELECT scene,kind,provider,model,fallback_model,score,detail,at "
                 "FROM provider_events WHERE job_id=? ORDER BY at", (job_id,))


# ------------------------------------------------------------------ segments
def add_segment(job_id: str, seq: int, name: str, duration: float,
                scene: int | None, key: str | None) -> bool:
    cur = _exec("INSERT OR IGNORE INTO segments(job_id,seq,name,duration,scene,key,at) "
                "VALUES(?,?,?,?,?,?,?)", (job_id, seq, name, duration, scene, key, now_ms()))
    return cur.rowcount == 1


def segments(job_id: str) -> list[dict]:
    return _rows("SELECT * FROM segments WHERE job_id=? ORDER BY seq", (job_id,))


def next_seq(job_id: str) -> int:
    r = _row("SELECT MAX(seq) AS m FROM segments WHERE job_id=?", (job_id,))
    return (r["m"] or 0) + 1


# ------------------------------------------------------------------ chaos
def set_chaos(provider: str, dead: bool | None = None) -> bool:
    cur = is_dead(provider)
    new = (not cur) if dead is None else bool(dead)
    _exec("INSERT INTO chaos(provider,dead,at) VALUES(?,?,?) "
          "ON CONFLICT(provider) DO UPDATE SET dead=excluded.dead, at=excluded.at",
          (provider, int(new), now_ms()))
    return new


def is_dead(provider: str) -> bool:
    r = _row("SELECT dead FROM chaos WHERE provider=?", (provider,))
    return bool(r and r["dead"])


def dead_providers() -> list[str]:
    return [r["provider"] for r in _rows("SELECT provider FROM chaos WHERE dead=1")]


# ------------------------------------------------------------------ demo()
def demo() -> None:
    """Autocomprobacion: la idempotencia por event_id de verdad funciona."""
    import tempfile

    global DB_PATH, _initialized
    tmp = Path(tempfile.mkdtemp()) / "t.db"
    DB_PATH = tmp
    _local.__dict__.pop("conn", None)
    _initialized = False
    init()

    j = create_job("j_test", "brief de prueba", "T", 3)
    assert j["status"] == "queued" and j["scene_count"] == 3
    assert len(scenes("j_test")) == 3

    update_scene("j_test", 2, status="ready", ms=1234)
    assert scenes("j_test")[1]["ms"] == 1234

    # idempotencia: el mismo eventId 3 veces -> solo la primera es nueva
    assert record_event("ev1", "webhook", key="incoming/j_test/seg/00001.ts") is True
    assert record_event("ev1", "webhook", key="incoming/j_test/seg/00001.ts") is False
    assert record_event("ev1", "poll", key="incoming/j_test/seg/00001.ts") is False
    assert record_event("ev2", "poll") is True
    assert len(recent_events()) == 2

    assert set_chaos("gmicloud") is True
    assert is_dead("gmicloud") is True
    assert set_chaos("gmicloud") is False
    assert set_chaos("gmicloud", True) is True
    assert dead_providers() == ["gmicloud"]

    assert add_segment("j_test", 1, "00001.ts", 4.0, 1, "k") is True
    assert add_segment("j_test", 1, "00001.ts", 4.0, 1, "k") is False   # idempotente
    assert next_seq("j_test") == 2

    add_decision("j_test", "reject", "logo ilegible", 2)
    assert reject_count("j_test") == 1
    set_status("j_test", "in_review", total_render_ms=9000)
    assert get_job("j_test")["status"] == "in_review"
    print("db.demo OK ->", tmp)


if __name__ == "__main__":
    demo()
