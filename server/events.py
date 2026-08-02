"""Bus de eventos: B2 Event Notifications (webhook firmado) + Poller + SSE.

Tres fuentes, un solo bus interno:

1. `POST /webhooks/b2` — B2 Event Notifications. Firma HMAC-SHA256 del **cuerpo crudo**
   en `X-Bz-Event-Notification-Signature: v1=<64 hex>`. B2 exige 200 en <3 s, asi que
   aqui solo se verifica, se hace `INSERT OR IGNORE` por `eventId` y se encola en un
   `queue.Queue`; el trabajo real lo hace un worker thread. Entrega **at-least-once**:
   todo handler es idempotente por `eventId`.
2. `Poller` — thread cada 2 s con `list_objects_v2` sobre los prefijos que importan.
   Emite exactamente los MISMOS eventos internos. Si la cuenta tiene las Event
   Notifications gated (403 al crear reglas), la demo es indistinguible.
3. Eventos internos del propio backend (assembler, jobs).

Conmutable con `EVENTS_MODE=webhook|poll|both` (default `both`). Con `both`, el que
llegue segundo se descarta solo, porque la clave de idempotencia es el objeto, no la fuente.

Autocomprobacion de la firma HMAC:
    .venv/bin/python -m server.events
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import queue
import threading
import time

SIG_HEADER = "X-Bz-Event-Notification-Signature"

_loop: asyncio.AbstractEventLoop | None = None
_subscribers: set[asyncio.Queue] = set()
_sub_lock = threading.Lock()

_work: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=10000)
_worker: threading.Thread | None = None
_poller: threading.Thread | None = None
_stop = threading.Event()

_recent: list[dict] = []          # buffer para debug / health


def secret() -> bytes:
    return os.getenv("B2_WEBHOOK_SECRET", "firstframe-dev-secret").encode()


def mode() -> str:
    return os.getenv("EVENTS_MODE", "both").lower()


# ------------------------------------------------------------------ firma HMAC
def sign(raw: bytes, key: bytes | None = None) -> str:
    """Genera la cabecera tal y como la manda B2: `v1=<hexdigest>`."""
    return "v1=" + hmac.new(key or secret(), raw, hashlib.sha256).hexdigest()


def verify(raw: bytes, header: str | None, key: bytes | None = None) -> bool:
    """Comparacion en tiempo constante. Sin firma valida no se procesa nada."""
    if not header:
        return False
    header = header.strip()
    if not header.startswith("v1="):
        return False
    got = header[3:]
    if len(got) != 64:
        return False
    expected = hmac.new(key or secret(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, got)


# ------------------------------------------------------------------ pub/sub SSE
def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=1000)
    with _sub_lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _sub_lock:
        _subscribers.discard(q)


def publish(kind: str, payload: dict) -> None:
    """Thread-safe: lo llaman el assembler, el worker del webhook y el poller."""
    payload = {"at": int(time.time() * 1000), **payload}
    _recent.append({"kind": kind, **payload})
    del _recent[:-200]
    with _sub_lock:
        subs = list(_subscribers)
    if not subs:
        return
    for q in subs:
        if _loop is not None and _loop.is_running():
            _loop.call_soon_threadsafe(_offer, q, kind, payload)
        else:
            _offer(q, kind, payload)


def _offer(q: asyncio.Queue, kind: str, payload: dict) -> None:
    try:
        q.put_nowait((kind, payload))
    except asyncio.QueueFull:
        pass


def recent(limit: int = 50) -> list[dict]:
    return _recent[-limit:]


# ------------------------------------------------------------------ webhook
def handle_webhook(raw: bytes, signature: str | None) -> tuple[bool, int]:
    """Ack rapido: verifica, deduplica y encola. Devuelve (firma_ok, encolados)."""
    if not verify(raw, signature):
        return False, 0
    try:
        body = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return True, 0
    evs = body.get("events") or ([body] if body.get("eventId") else [])
    queued = 0
    from server import db

    for ev in evs:
        eid = ev.get("eventId") or _synth_id("webhook", ev)
        key = ev.get("objectName") or ev.get("key")
        if db.record_event(eid, "webhook", kind=ev.get("eventType"),
                           job_id=_job_from_key(key), key=key, payload=ev):
            try:
                _work.put_nowait((eid, ev))
                queued += 1
            except queue.Full:
                pass
    return True, queued


def _synth_id(source: str, ev: dict) -> str:
    base = f"{source}:{ev.get('objectName')}:{ev.get('eventType')}:{ev.get('eventVersion')}"
    return hashlib.sha256(base.encode()).hexdigest()[:32]


def _job_from_key(key: str | None) -> str | None:
    if not key:
        return None
    parts = key.split("/")
    if len(parts) >= 2 and parts[0] in ("incoming", "approved", "rejected",
                                        "provenance", "runs", "refs"):
        return parts[1]
    return None


# ------------------------------------------------------------------ worker
def _worker_loop() -> None:
    from server import db

    while not _stop.is_set():
        try:
            eid, ev = _work.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            _dispatch(ev)
            db.mark_processed(eid)
        except Exception as e:  # nunca matar el worker
            print(f"[events] worker error: {e!r}")
        finally:
            _work.task_done()


def _dispatch(ev: dict) -> None:
    """Traduce un evento de B2 al bus interno. Idempotente por construccion."""
    key = ev.get("objectName") or ev.get("key") or ""
    etype = ev.get("eventType") or ""
    job_id = _job_from_key(key)
    at = int(time.time() * 1000)

    if key.startswith("incoming/") and key.endswith((".ts", ".m4s")):
        publish("segment_landed", {"job_id": job_id, "at": at, "key": key,
                                   "source": ev.get("_source", "b2")})
        from server import db, jobs

        j = db.get_job(job_id) if job_id else None
        if j and j["status"] == "queued":
            jobs.mark_rendering(job_id)
    elif key.startswith("approved/") and key.endswith(".mp4"):
        from server import jobs

        publish("approved", {"job_id": job_id, "at": at, "key": key,
                             "job": jobs.public_job(job_id) if job_id else None})
    elif key.startswith("provenance/"):
        publish("job_update", {"job_id": job_id, "at": at, "manifest_key": key})
    elif key.startswith("rejected/") or "HideMarker" in etype:
        publish("job_update", {"job_id": job_id, "at": at, "key": key,
                               "audit": "lifecycle-hide"})
    else:
        publish("job_update", {"job_id": job_id, "at": at, "key": key})


# ------------------------------------------------------------------ poller
class Poller(threading.Thread):
    """Fallback obligatorio: mismos eventos internos, sin depender de webhooks.

    Un `list_objects_v2` cada 2 s sobre los prefijos que importan. La deduplicacion
    la hace la misma tabla `events` que usa el webhook, asi que con EVENTS_MODE=both
    lo que ya vio el webhook aqui es un no-op.
    """

    PREFIXES = ("incoming/", "approved/", "provenance/", "rejected/")

    def __init__(self, interval: float = 2.0):
        super().__init__(daemon=True, name="b2-poller")
        self.interval = interval
        self.ticks = 0

    def run(self) -> None:
        from server import b2, db

        if not b2.available():
            print("[events] poller: sin credenciales B2, no arranca")
            return
        while not _stop.is_set():
            try:
                self.ticks += 1
                for prefix in self.PREFIXES:
                    for o in b2.list_prefix(prefix, max_keys=500):
                        ev = {"objectName": o["key"], "eventType": "b2:ObjectCreated:Upload",
                              "eventVersion": o["etag"], "_source": "poll"}
                        eid = _synth_id("poll", ev)
                        if db.record_event(eid, "poll", kind=ev["eventType"],
                                           job_id=_job_from_key(o["key"]),
                                           key=o["key"], payload=ev):
                            _work.put_nowait((eid, ev))
            except Exception as e:
                print(f"[events] poller error: {e!r}")
            _stop.wait(self.interval)


# ------------------------------------------------------------------ ciclo de vida
def start(app=None) -> None:
    global _loop, _worker, _poller
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        _loop = None
    _stop.clear()
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_worker_loop, daemon=True, name="events-worker")
        _worker.start()
    if mode() in ("poll", "both") and (_poller is None or not _poller.is_alive()):
        _poller = Poller()
        _poller.start()
    print(f"[events] mode={mode()} worker=on poller={'on' if _poller else 'off'}")


def stop() -> None:
    _stop.set()


# ------------------------------------------------------------------ demo()
def demo() -> None:
    """Autocomprobacion de la verificacion HMAC y de la idempotencia por eventId."""
    import tempfile
    from pathlib import Path

    key = b"secreto-de-prueba"
    body = json.dumps({"events": [{
        "eventId": "abc123", "eventType": "b2:ObjectCreated:Upload",
        "objectName": "incoming/j_x/seg/00001.ts", "eventVersion": "v1"}]}).encode()

    good = sign(body, key)
    assert good.startswith("v1=") and len(good) == 67, good
    assert verify(body, good, key) is True
    print("1. firma valida aceptada OK ->", good[:20], "...")

    assert verify(body, None, key) is False
    assert verify(body, "", key) is False
    assert verify(body, good[3:], key) is False                  # sin prefijo v1=
    assert verify(body, "v1=" + "0" * 64, key) is False          # hex del tamano bueno
    assert verify(body, good[:-1] + ("0" if good[-1] != "0" else "1"), key) is False
    assert verify(body + b" ", good, key) is False               # cuerpo alterado
    assert verify(body, sign(body, b"otro-secreto"), key) is False
    print("2. rechaza: sin header, sin v1=, hex falso, 1 bit cambiado, cuerpo alterado, "
          "secreto distinto OK")

    # el header real de B2 llega con el mismo formato que produce sign()
    assert verify(body, f"  {good}  ", key) is True
    print("3. tolera espacios alrededor del header OK")

    os.environ["B2_WEBHOOK_SECRET"] = key.decode()
    from server import db

    tmp = Path(tempfile.mkdtemp())
    db.DB_PATH = tmp / "t.db"
    db._local.__dict__.pop("conn", None)
    db._initialized = False
    db.init()

    ok, n = handle_webhook(body, good)
    assert ok and n == 1, (ok, n)
    ok, n = handle_webhook(body, good)       # B2 reintenta: at-least-once
    assert ok and n == 0, (ok, n)
    ok, n = handle_webhook(body, good)
    assert ok and n == 0, (ok, n)
    print("4. mismo eventId 3 veces -> encolado 1 sola vez (idempotente) OK")

    ok, n = handle_webhook(body, "v1=" + "f" * 64)
    assert ok is False and n == 0
    print("5. firma mala -> 401, nada encolado OK")

    t0 = time.time()
    for i in range(200):
        b = json.dumps({"events": [{"eventId": f"e{i}", "eventType": "x",
                                    "objectName": f"incoming/j/seg/{i}.ts"}]}).encode()
        handle_webhook(b, sign(b, key))
    ms = (time.time() - t0) * 1000
    assert ms < 3000, ms
    print(f"6. 200 webhooks verificados+encolados en {ms:.0f} ms "
          f"({ms/200:.2f} ms cada uno; el limite de B2 es 3000 ms) OK")

    assert _job_from_key("incoming/j_abc/seg/00001.ts") == "j_abc"
    assert _job_from_key("approved/j_abc/final.mp4") == "j_abc"
    assert _job_from_key("otracosa/x") is None
    print("7. extraccion de job_id desde la clave OK")
    print("events.demo OK")


if __name__ == "__main__":
    demo()
