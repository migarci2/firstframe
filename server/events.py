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
_poll_wake = threading.Event()    # despierta al poller cuando aparece un job


def wake() -> None:
    """Saca al poller de su siesta larga (lo llama `jobs.create_job`).

    Sin esto, un job creado durante el intervalo de reposo (60 s) tardaria hasta un
    minuto en entrar en la rotacion. Asi el intervalo largo no cuesta reactividad.
    """
    _poll_wake.set()


def secret() -> bytes:
    return os.getenv("B2_WEBHOOK_SECRET", "firstframe-dev-secret").encode()


def mode() -> str:
    """`webhook` | `poll` | `both` | `off`.

    `off` = ni webhook ni poller tocan B2. Es el modo de emergencia si la cuenta se
    queda sin cuota de transacciones: la app sigue entera porque todos los eventos que
    mueven la UI (segmentos, escenas, approve) los emite el propio backend.
    """
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
    if mode() == "off":
        return True, 0
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
        from server import db, jobs

        # si el segmento ya esta en la DB es que lo subimos nosotros y el assembler ya
        # publico su evento: el eco de B2 (webhook o poller) no se repite en la UI.
        known = {s["key"] for s in db.segments(job_id)} if job_id else set()
        if key not in known:
            publish("segment_landed", {"job_id": job_id, "at": at, "key": key,
                                       "source": ev.get("_source", "b2")})
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

    **Presupuesto de transacciones.** La primera version listaba 4 prefijos cada 2 s:
    120 `list_objects_v2` por minuto, que es Class C. Con eso nos comimos el tope diario
    de la cuenta free en una tarde de integracion. Ahora:
      - **un solo prefijo por tick**, rotando (no 4);
      - intervalo **adaptativo**: `B2_POLL_ACTIVE_S` (10 s) solo mientras hay un job
        vivo, `B2_POLL_IDLE_S` (60 s) cuando no hay nada que mirar;
      - mientras hay job vivo se mira **su** prefijo `incoming/{job}/`, no `incoming/`
        entero;
      - si B2 marca cap, el poller **se para** hasta que se enfrie.
    De 3600 listados/hora a ~110 en reposo y ~360 con un job corriendo.

    La deduplicacion la sigue haciendo la tabla `events`, asi que con EVENTS_MODE=both
    lo que ya vio el webhook aqui es un no-op.
    """

    PREFIXES = ("approved/", "provenance/", "rejected/")

    def __init__(self, interval: float | None = None):
        super().__init__(daemon=True, name="b2-poller")
        self.active_interval = float(os.getenv("B2_POLL_ACTIVE_S", interval or 10))
        self.idle_interval = float(os.getenv("B2_POLL_IDLE_S", "60"))
        self.ticks = 0
        self.listings = 0
        self._rot = 0

    def _next_prefix(self, active_jobs: list[str]) -> str:
        """Un prefijo por tick. Los jobs vivos entran en la rotacion, acotados a su id."""
        pool = [f"incoming/{j}/" for j in active_jobs] + list(self.PREFIXES)
        self._rot = (self._rot + 1) % len(pool)
        return pool[self._rot]

    def run(self) -> None:
        from server import b2, db

        if not b2.has_credentials():
            print("[events] poller: sin credenciales B2, no arranca")
            return
        print(f"[events] poller: {self.active_interval:.0f}s con job vivo / "
              f"{self.idle_interval:.0f}s en reposo, 1 prefijo por tick")
        while not _stop.is_set():
            interval = self.idle_interval
            try:
                if b2.capped():
                    print("[events] poller en pausa: cap de transacciones de B2")
                    _stop.wait(30)
                    continue
                active = [j["id"] for j in db.all_jobs()
                          if j["status"] in ("queued", "rendering")]
                interval = self.active_interval if active else self.idle_interval
                self.ticks += 1
                prefix = self._next_prefix(active)
                self.listings += 1
                for o in b2.list_prefix(prefix, max_keys=200, ttl=0):
                    ev = {"objectName": o["key"], "eventType": "b2:ObjectCreated:Upload",
                          "eventVersion": o["etag"], "_source": "poll"}
                    eid = _synth_id("poll", ev)
                    if db.record_event(eid, "poll", kind=ev["eventType"],
                                       job_id=_job_from_key(o["key"]),
                                       key=o["key"], payload=ev):
                        _work.put_nowait((eid, ev))
            except Exception as e:
                print(f"[events] poller error: {e!r}")
            self._nap(interval)

    def _nap(self, interval: float) -> None:
        """Duerme `interval`, pero se despierta antes si aparece un job (`wake()`)."""
        end = time.time() + interval
        while not _stop.is_set() and time.time() < end:
            if _poll_wake.wait(min(1.0, end - time.time())):
                _poll_wake.clear()
                return


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
    elif mode() == "off":
        print("[events] EVENTS_MODE=off: sin poller y el webhook devolvera 503")
    print(f"[events] mode={mode()} worker=on "
          f"poller={'on' if _poller and _poller.is_alive() else 'off'}")


def poller_stats() -> dict:
    return {
        "running": bool(_poller and _poller.is_alive()),
        "ticks": _poller.ticks if _poller else 0,
        "listings": _poller.listings if _poller else 0,
        "active_interval_s": _poller.active_interval if _poller else None,
        "idle_interval_s": _poller.idle_interval if _poller else None,
    }


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

    # --- presupuesto de transacciones (esto es lo que tumbo la cuenta) -------------
    p = Poller()
    got = [p._next_prefix(["j_1"]) for _ in range(8)]
    assert all(isinstance(g, str) for g in got)
    assert len(set(got)) == 4, got                     # rota 1 prefijo por tick
    assert "incoming/j_1/" in got, got                 # acotado al job, no a incoming/
    assert not any(g == "incoming/" for g in got), got
    print(f"8. poller: 1 prefijo por tick, rotando {sorted(set(got))} OK")

    hourly_idle = 3600 / p.idle_interval
    hourly_active = 3600 / p.active_interval
    assert hourly_idle <= 120 and hourly_active <= 400, (hourly_idle, hourly_active)
    print(f"9. presupuesto: {hourly_idle:.0f} listados/hora en reposo, "
          f"{hourly_active:.0f}/hora con job vivo (antes: 7200/hora) OK")

    os.environ["EVENTS_MODE"] = "off"
    assert mode() == "off"
    ok, n = handle_webhook(body, good)
    assert ok is True and n == 0, (ok, n)
    print("10. EVENTS_MODE=off: ni poller ni procesado de webhooks (modo sin cuota) OK")
    os.environ["EVENTS_MODE"] = "both"

    t0 = time.time()
    threading.Timer(0.3, wake).start()
    p._nap(30)
    assert time.time() - t0 < 2, "wake() no despierta al poller"
    print("11. wake() saca al poller de la siesta de 60 s al crear un job OK")
    print("events.demo OK")


if __name__ == "__main__":
    demo()
