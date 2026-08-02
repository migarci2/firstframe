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

Autocomprobacion (necesita credenciales):
    set -a && . ./.env && set +a && .venv/bin/python -m server.b2
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_lock = threading.Lock()
_clients: dict[str, object] = {}


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


def available() -> bool:
    return bool(os.getenv("B2_KEY_ID") and os.getenv("B2_APP_KEY"))


# ------------------------------------------------------------------ subidas
def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream",
              lock_mode: str | None = None, retain_days: int = 30) -> dict:
    kw = {"Bucket": bucket(), "Key": key, "Body": data, "ContentType": content_type}
    if lock_mode:
        kw["ObjectLockMode"] = lock_mode
        kw["ObjectLockRetainUntilDate"] = datetime.now(timezone.utc) + timedelta(days=retain_days)
    return client().put_object(**kw)


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
    """Devuelve (body, headers-utiles) o None si el objeto no existe (404)."""
    kw = {"Bucket": bucket(), "Key": key}
    if byte_range:
        kw["Range"] = byte_range
    try:
        r = client().get_object(**kw)
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


def head(key: str) -> dict | None:
    try:
        return client().head_object(Bucket=bucket(), Key=key)
    except ClientError:
        return None


def list_prefix(prefix: str, max_keys: int = 1000) -> list[dict]:
    out, token = [], None
    while True:
        kw = {"Bucket": bucket(), "Prefix": prefix, "MaxKeys": min(max_keys, 1000)}
        if token:
            kw["ContinuationToken"] = token
        r = client().list_objects_v2(**kw)
        for o in r.get("Contents", []):
            out.append({"key": o["Key"], "size": o["Size"],
                        "at": int(o["LastModified"].timestamp() * 1000),
                        "etag": o.get("ETag", "").strip('"')})
        token = r.get("NextContinuationToken")
        if not r.get("IsTruncated") or len(out) >= max_keys:
            return out[:max_keys]


def delete(key: str, bypass_governance: bool = False, version_id: str | None = None) -> None:
    """OJO: `delete_object` SIN VersionId solo crea un hide marker y Object Lock no lo
    impide (la version sigue viva). Para probar que el lock protege de verdad hay que
    borrar la VERSION concreta: eso es lo que B2 rechaza con AccessDenied."""
    kw = {"Bucket": bucket(), "Key": key}
    if version_id:
        kw["VersionId"] = version_id
    if bypass_governance:
        kw["BypassGovernanceRetention"] = True
    client().delete_object(**kw)


def version_id(key: str) -> str | None:
    h = head(key)
    return h.get("VersionId") if h else None


# ------------------------------------------------------------------ object lock
def get_retention(key: str) -> dict | None:
    """{'mode': 'GOVERNANCE', 'retain_until': iso} o None si el objeto no esta bloqueado."""
    try:
        r = client().get_object_retention(Bucket=bucket(), Key=key)
    except ClientError:
        return None
    ret = r.get("Retention") or {}
    until = ret.get("RetainUntilDate")
    return {
        "mode": ret.get("Mode"),
        "retain_until": until.isoformat().replace("+00:00", "Z") if until else None,
    }


def apply_lock(key: str, mode: str = "GOVERNANCE", days: int = 30) -> dict:
    """Aplica retencion a un objeto que ya existe."""
    until = datetime.now(timezone.utc) + timedelta(days=days)
    client().put_object_retention(
        Bucket=bucket(), Key=key,
        Retention={"Mode": mode, "RetainUntilDate": until},
    )
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
        r = client().list_multipart_uploads(Bucket=bucket(), Prefix=prefix)
    except ClientError:
        return []
    return [{"key": u["Key"], "upload_id": u["UploadId"],
             "at": int(u["Initiated"].timestamp() * 1000)} for u in r.get("Uploads", [])]


# ------------------------------------------------------------------ demo()
def demo() -> None:
    """Autocomprobacion contra la cuenta real. Usa el prefijo probe/b2mod/."""
    assert available(), "faltan credenciales: set -a && . ./.env && set +a"
    p = f"probe/b2mod/{int(datetime.now().timestamp())}"

    put_bytes(f"{p}/hello.txt", b"hola firstframe", "text/plain")
    got = get_bytes(f"{p}/hello.txt")
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
    print("b2.demo OK")


if __name__ == "__main__":
    demo()
