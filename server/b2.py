"""Cliente B2 (S3 compat) con boto3. Region eu-central-003, SigV4.

Dos detalles que cuestan horas si no se saben:

1. **presign path-style.** Los presigns virtual-host style
   (`https://bucket.s3.<region>.backblazeb2.com/key`) fallan en navegador contra buckets
   privados de B2 (SDK issue #246, sin documentar). Hay que firmar en **path-style**:
   `https://s3.<region>.backblazeb2.com/bucket/key`. Se consigue con
   `Config(s3={"addressing_style": "path"})` -> `presign_path_style()`.

2. **Object Lock.** El bucket se creo con lock activado (no se puede activar despues),
   asi que el limite de nombre+file-info es 2048 bytes: los manifests van SIEMPRE como
   objeto, jamas como metadata.

3. **Tope de transacciones de la cuenta free.** Nos lo comimos una vez en integracion:
   B2 empieza a devolver `AccessDenied: Transaction cap exceeded` en `ListObjectsV2` y
   `HeadObject` y la app se cae entera. Desde entonces, todo pasa por `_call()`, que
   (a) **cuenta** cada llamada por operacion -> `stats()` -> `/api/health`,
   (b) detecta el cap, lo marca y **deja de llamar** durante `CAP_RETRY_S` en vez de
       estrellarse contra la pared en bucle,
   (c) permite cachear con TTL lo que se consulta repetido (head, retention, listados).
   Los que llaman tratan `CapExceeded` como "no lo se" y siguen con el disco local.

Autocomprobacion (necesita credenciales):
    set -a && . ./.env && set +a && .venv/bin/python -m server.b2
"""
from __future__ import annotations

import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_lock = threading.Lock()
_clients: dict[str, object] = {}

# ------------------------------------------------------------------ contadores y cap
CAP_RETRY_S = float(os.getenv("B2_CAP_RETRY_S", "300"))

_stats = Counter()
_stats_lock = threading.Lock()
_cap = {"hit": False, "at": None, "detail": None, "retry_at": 0.0, "blocked": 0}
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


class CapExceeded(RuntimeError):
    """B2 dice que la cuenta agoto el tope diario de transacciones."""


def _is_cap(e: ClientError) -> bool:
    err = e.response.get("Error", {})
    blob = f"{err.get('Code', '')} {err.get('Message', '')}".lower()
    return "cap exceeded" in blob or "transaction cap" in blob or "cap_exceeded" in blob


def capped() -> bool:
    """True mientras estemos en la ventana de enfriamiento tras un cap."""
    return bool(_cap["hit"]) and time.time() < _cap["retry_at"]


def _call(op: str, fn, *args, **kwargs):
    """Toda llamada a B2 pasa por aqui: se cuenta, y el cap se detecta y se absorbe."""
    if capped():
        with _stats_lock:
            _cap["blocked"] += 1
        raise CapExceeded(f"{op}: en enfriamiento por cap de transacciones")
    with _stats_lock:
        _stats[op] += 1
        _stats["total"] += 1
    try:
        return fn(*args, **kwargs)
    except ClientError as e:
        if _is_cap(e):
            _cap.update(hit=True, at=int(time.time() * 1000),
                        detail=e.response["Error"].get("Message", "cap exceeded"),
                        retry_at=time.time() + CAP_RETRY_S)
            with _stats_lock:
                _stats["cap_errors"] += 1
            print(f"[b2] TRANSACTION CAP en {op}: se corta el trafico {CAP_RETRY_S:.0f}s")
            raise CapExceeded(str(e)) from e
        raise


def stats() -> dict:
    """Consumo de transacciones desde el arranque, para /api/health."""
    with _stats_lock:
        by_op = {k: v for k, v in _stats.items() if k not in ("total", "cap_errors")}
        total = _stats["total"]
        cap_errors = _stats["cap_errors"]
    return {
        "total": total,
        "by_op": dict(sorted(by_op.items(), key=lambda kv: -kv[1])),
        "cache_hits": _stats.get("cache_hit", 0),
        "capped": capped(),
        "cap_hit_ever": bool(_cap["hit"]),
        "cap_at": _cap["at"],
        "cap_detail": _cap["detail"],
        "calls_skipped_by_cap": _cap["blocked"],
        "cap_retry_in_s": max(0, int(_cap["retry_at"] - time.time())) if _cap["hit"] else 0,
    }


def reset_stats() -> None:
    with _stats_lock:
        _stats.clear()
    _cap.update(hit=False, at=None, detail=None, retry_at=0.0, blocked=0)
    with _cache_lock:
        _cache.clear()


def _cached(key: str, ttl: float, producer):
    """Memo con TTL: releer lo mismo en cada request es lo que agota la cuota."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            with _stats_lock:
                _stats["cache_hit"] += 1
            return hit[1]
    val = producer()
    with _cache_lock:
        _cache[key] = (now + ttl, val)
    return val


def invalidate(prefix: str = "") -> None:
    """Tras escribir algo hay que olvidar lo cacheado de esa clave."""
    with _cache_lock:
        for k in [k for k in _cache if prefix in k]:
            _cache.pop(k, None)


def region() -> str:
    return os.getenv("B2_REGION", "eu-central-003")


def bucket() -> str:
    return os.getenv("B2_BUCKET", "genblaze-review-migarci2")


def endpoint() -> str:
    return f"https://s3.{region()}.backblazeb2.com"


def _make(addressing: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint(),
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APP_KEY"],
        region_name=region(),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing},
            retries={"max_attempts": 3, "mode": "standard"},
            max_pool_connections=32,
        ),
    )


def client():
    """Cliente normal (auto addressing) para operaciones server-side."""
    with _lock:
        if "auto" not in _clients:
            _clients["auto"] = _make("auto")
        return _clients["auto"]


def path_client():
    """Cliente forzado a path-style: el unico valido para presigns de navegador."""
    with _lock:
        if "path" not in _clients:
            _clients["path"] = _make("path")
        return _clients["path"]


def has_credentials() -> bool:
    return bool(os.getenv("B2_KEY_ID") and os.getenv("B2_APP_KEY"))


def available() -> bool:
    """Hay credenciales **y** no estamos en enfriamiento por cap.

    Los que llaman usan esto para saltarse el trafico opcional sin pensar: con la cuota
    agotada la app sigue en pie tirando de disco local.
    """
    if not has_credentials():
        return False
    if capped():
        with _stats_lock:
            _cap["blocked"] += 1
        return False
    return True


# ------------------------------------------------------------------ subidas
def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream",
              lock_mode: str | None = None, retain_days: int = 30) -> dict:
    kw = {"Bucket": bucket(), "Key": key, "Body": data, "ContentType": content_type}
    if lock_mode:
        kw["ObjectLockMode"] = lock_mode
        kw["ObjectLockRetainUntilDate"] = datetime.now(timezone.utc) + timedelta(days=retain_days)
    out = _call("put_object", client().put_object, **kw)
    invalidate(key)
    return out


def put_file(key: str, path: str | Path, content_type: str | None = None,
             lock_mode: str | None = None, retain_days: int = 30) -> dict:
    p = Path(path)
    ct = content_type or _guess_ct(p.name)
    return put_bytes(key, p.read_bytes(), ct, lock_mode=lock_mode, retain_days=retain_days)


def _guess_ct(name: str) -> str:
    n = name.lower()
    if n.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if n.endswith(".ts"):
        return "video/mp2t"
    if n.endswith((".mp4", ".m4s")):
        return "video/mp4"
    if n.endswith(".json"):
        return "application/json"
    if n.endswith(".png"):
        return "image/png"
    return "application/octet-stream"


# ------------------------------------------------------------------ lecturas
def get_bytes(key: str, byte_range: str | None = None) -> tuple[bytes, dict] | None:
    """Devuelve (body, headers-utiles) o None si el objeto no existe (404) o hay cap."""
    kw = {"Bucket": bucket(), "Key": key}
    if byte_range:
        kw["Range"] = byte_range
    try:
        r = _call("get_object", client().get_object, **kw)
    except CapExceeded:
        return None
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound", "InvalidRange", "416"):
            return None
        raise
    meta = {
        "content_length": r.get("ContentLength"),
        "content_range": r.get("ContentRange"),
        "content_type": r.get("ContentType"),
        "etag": r.get("ETag"),
    }
    return r["Body"].read(), meta


def head(key: str, ttl: float = 60.0) -> dict | None:
    """HEAD cacheado: `head_object` es Class B y se llamaba en cada request."""
    def _do():
        try:
            return _call("head_object", client().head_object, Bucket=bucket(), Key=key)
        except (ClientError, CapExceeded):
            return None

    return _cached(f"head:{key}", ttl, _do)


def list_prefix(prefix: str, max_keys: int = 1000, ttl: float = 15.0) -> list[dict]:
    """LIST cacheado (Class C, el mas caro en transacciones). ttl=0 fuerza fresco."""
    def _do():
        out, token = [], None
        while True:
            kw = {"Bucket": bucket(), "Prefix": prefix, "MaxKeys": min(max_keys, 1000)}
            if token:
                kw["ContinuationToken"] = token
            try:
                r = _call("list_objects_v2", client().list_objects_v2, **kw)
            except CapExceeded:
                return out
            for o in r.get("Contents", []):
                out.append({"key": o["Key"], "size": o["Size"],
                            "at": int(o["LastModified"].timestamp() * 1000),
                            "etag": o.get("ETag", "").strip('"')})
            token = r.get("NextContinuationToken")
            if not r.get("IsTruncated") or len(out) >= max_keys:
                return out[:max_keys]

    if ttl <= 0:
        return _do()
    return _cached(f"list:{prefix}:{max_keys}", ttl, _do)


def delete(key: str, bypass_governance: bool = False, version_id: str | None = None) -> None:
    """OJO: `delete_object` SIN VersionId solo crea un hide marker y Object Lock no lo
    impide (la version sigue viva). Para probar que el lock protege de verdad hay que
    borrar la VERSION concreta: eso es lo que B2 rechaza con AccessDenied."""
    kw = {"Bucket": bucket(), "Key": key}
    if version_id:
        kw["VersionId"] = version_id
    if bypass_governance:
        kw["BypassGovernanceRetention"] = True
    _call("delete_object", client().delete_object, **kw)
    invalidate(key)


def version_id(key: str) -> str | None:
    h = head(key)
    return h.get("VersionId") if h else None


# ------------------------------------------------------------------ object lock
def get_retention(key: str, ttl: float = 300.0) -> dict | None:
    """{'mode': 'GOVERNANCE', 'retain_until': iso} o None si no esta bloqueado.

    Cacheado 5 min: la retencion de un objeto no cambia, y la UI la pide en cada
    refresco del panel de provenance.
    """
    def _do():
        try:
            r = _call("get_object_retention", client().get_object_retention,
                      Bucket=bucket(), Key=key)
        except (ClientError, CapExceeded):
            return None
        ret = r.get("Retention") or {}
        until = ret.get("RetainUntilDate")
        return {
            "mode": ret.get("Mode"),
            "retain_until": until.isoformat().replace("+00:00", "Z") if until else None,
        }

    return _cached(f"ret:{key}", ttl, _do)


def apply_lock(key: str, mode: str = "GOVERNANCE", days: int = 30) -> dict:
    """Aplica retencion a un objeto que ya existe."""
    until = datetime.now(timezone.utc) + timedelta(days=days)
    _call("put_object_retention", client().put_object_retention,
          Bucket=bucket(), Key=key,
          Retention={"Mode": mode, "RetainUntilDate": until})
    invalidate(key)
    return {"mode": mode, "retain_until": until.isoformat().replace("+00:00", "Z")}


# ------------------------------------------------------------------ presign
def presign_path_style(key: str, expires: int = 3600, download_as: str | None = None) -> str:
    """URL firmada AWS4 **path-style**.

    Virtual-host style falla en navegador contra buckets privados de B2 (issue #246).
    Aqui se fuerza `addressing_style=path` -> https://s3.<region>.backblazeb2.com/<bucket>/<key>
    """
    params = {"Bucket": bucket(), "Key": key}
    if download_as:
        params["ResponseContentDisposition"] = f'attachment; filename="{download_as}"'
    url = path_client().generate_presigned_url("get_object", Params=params, ExpiresIn=expires)
    assert f"/{bucket()}/" in url, f"presign no es path-style: {url}"
    return url


# ------------------------------------------------------------------ multipart (info)
def list_multipart(prefix: str = "incoming/") -> list[dict]:
    try:
        r = _call("list_multipart_uploads", client().list_multipart_uploads,
                  Bucket=bucket(), Prefix=prefix)
    except (ClientError, CapExceeded):
        return []
    return [{"key": u["Key"], "upload_id": u["UploadId"],
             "at": int(u["Initiated"].timestamp() * 1000)} for u in r.get("Uploads", [])]


# ------------------------------------------------------------------ demo()
def demo() -> None:
    """Autocomprobacion contra la cuenta real. Usa el prefijo probe/b2mod/."""
    assert has_credentials(), "faltan credenciales: set -a && . ./.env && set +a"
    reset_stats()
    p = f"probe/b2mod/{int(datetime.now().timestamp())}"

    try:
        put_bytes(f"{p}/hello.txt", b"hola firstframe", "text/plain")
    except CapExceeded:
        pass
    got = None if capped() else get_bytes(f"{p}/hello.txt")
    if capped():
        print("AVISO: la cuenta esta con el tope de transacciones agotado.")
        print("       Se saltan las pruebas contra B2 y se comprueba la DEGRADACION,")
        print("       que es justo lo que importa en este estado.")
        _demo_offline()
        return

    assert got and got[0] == b"hola firstframe", got
    print("1. put/get OK")

    partial = get_bytes(f"{p}/hello.txt", "bytes=0-3")
    assert partial and partial[0] == b"hola", partial
    print("2. range OK ->", partial[1]["content_range"])

    assert get_bytes(f"{p}/no-existe.txt") is None
    print("3. 404 -> None OK")

    ls = list_prefix(f"{p}/")
    assert len(ls) == 1 and ls[0]["key"].endswith("hello.txt")
    print("4. list OK")

    url = presign_path_style(f"{p}/hello.txt", 300)
    assert url.startswith(f"{endpoint()}/{bucket()}/"), url
    assert "X-Amz-Signature" in url and "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    print("5. presign path-style OK ->", url[:88], "...")
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=15) as r:
            body = r.read()
        assert body == b"hola firstframe", body
        print("6. presign descarga OK (200 desde HTTP plano, sin credenciales)")
    except Exception as e:  # pragma: no cover
        print("6. presign descarga FALLO:", e)
        raise

    lock = apply_lock(f"{p}/hello.txt", "GOVERNANCE", days=1)
    ret = get_retention(f"{p}/hello.txt")
    assert ret and ret["mode"] == "GOVERNANCE", ret
    print("7. object lock OK ->", lock)

    vid = version_id(f"{p}/hello.txt")
    try:
        delete(f"{p}/hello.txt", version_id=vid)
        print("8. FALLO: B2 dejo borrar la version bloqueada")
        raise SystemExit(1)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        assert code in ("AccessDenied", "InvalidRequest"), code
        print(f"8. borrado rechazado por B2 ({code}) OK  <- este es el momento del video")

    delete(f"{p}/hello.txt", bypass_governance=True, version_id=vid)
    print("9. limpieza con bypassGovernance OK")

    _demo_offline()


def _demo_offline() -> None:
    """Lo que se puede comprobar sin gastar cuota: cache y degradacion por cap."""
    # cache: la segunda lectura de lo mismo no gasta transaccion
    invalidate("")
    before = stats()["total"]
    for _ in range(5):
        list_prefix("approved/", max_keys=5)
        get_retention("no/existe.txt")
    spent = stats()["total"] - before
    assert spent <= 2, f"la cache no esta funcionando: {spent} llamadas para 10 lecturas"
    print(f"10. 10 lecturas repetidas -> {spent} transacciones (el resto, cache) OK")

    # degradacion: con el cap puesto, nada revienta y nada llama a B2
    _cap.update(hit=True, at=1, detail="simulado", retry_at=time.time() + 60)
    assert capped() is True and available() is False
    invalidate("")
    assert head("approved/loquesea.mp4") is None
    assert list_prefix("approved/") == []
    assert get_bytes("approved/loquesea.mp4") is None
    assert get_retention("approved/loquesea.mp4") is None
    assert stats()["capped"] is True and stats()["calls_skipped_by_cap"] >= 4
    print("11. con cap simulado: 0 llamadas, 0 excepciones, /api/health lo marca OK")
    reset_stats()
    assert capped() is False
    print("12. tras reset_stats el cliente vuelve a operar OK")
    print("b2.demo OK — transacciones del demo:", stats()["total"])


if __name__ == "__main__":
    demo()
