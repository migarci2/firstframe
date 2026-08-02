#!/usr/bin/env python3
"""Espina end-to-end SIN proveedor generativo: pipeline -> B2 -> manifest -> Object Lock.

Demuestra el camino completo que puntua en los criterios 3 y 4, usando imagenes
locales via PassthroughProvider. Cuando haya key de generacion, solo cambia el step 0.

    set -a && . ./.env && set +a && .venv/bin/python scripts/probe_spine.py
"""
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genblaze import Modality, ObjectLockConfig, ObjectStorageSink, Pipeline  # noqa: E402
from genblaze_s3 import S3StorageBackend  # noqa: E402

from pipeline.providers import PassthroughProvider  # noqa: E402

PREFIX = "probe/spine"


def _make_png(dirpath: Path, color: str) -> Path:
    out = dirpath / f"{color}.png"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"color=c={color}:s=512x512:d=1", "-frames:v", "1", str(out)],
        check=True,
    )
    return out


def _s3():
    region = os.environ["B2_REGION"]
    return boto3.client(
        "s3", endpoint_url=f"https://s3.{region}.backblazeb2.com",
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APP_KEY"],
        region_name=region, config=Config(signature_version="s3v4"))


def main() -> int:
    bucket = os.environ["B2_BUCKET"]
    s3 = _s3()

    with tempfile.TemporaryDirectory() as tmp:
        pngs = [_make_png(Path(tmp), c) for c in ("red", "blue")]
        print(f"1. {len(pngs)} imagenes locales generadas con ffmpeg")

        backend = S3StorageBackend.for_backblaze(
            bucket, region=os.environ["B2_REGION"],
            key_id=os.environ["B2_KEY_ID"], app_key=os.environ["B2_APP_KEY"])

        # manifest_lock: ningun ejemplo oficial lo usa. Deja el manifest WORM en B2.
        lock = ObjectLockConfig(
            retain_until=datetime.now(timezone.utc) + timedelta(days=1),
            mode="GOVERNANCE",
        )
        # ObjectStorageSink es de un solo uso: se cierra al terminar el run.
        sink = ObjectStorageSink(backend, prefix=PREFIX, manifest_lock=lock)

        # preflight=False: el preflight usa validate_model(), invertido (issue #248).
        pipe = Pipeline(name="spine-probe", preflight=False)
        pipe.step(PassthroughProvider(pngs), model="local-passthrough",
                  modality=Modality.IMAGE)
        result = pipe.run(sink=sink, raise_on_failure=True)
        print(f"2. pipeline OK  run_id={getattr(result, 'run_id', '?')}")

    listing = s3.list_objects_v2(Bucket=bucket, Prefix=PREFIX).get("Contents", [])
    print(f"3. {len(listing)} objetos en b2://{bucket}/{PREFIX}/")
    for o in listing[:8]:
        print(f"     {o['Size']:>9,} B  {o['Key']}")
    if not listing:
        print("FALLO: el sink no subio nada")
        return 1

    manifests = [o["Key"] for o in listing if o["Key"].endswith(".json")]
    if not manifests:
        print("AVISO: no encontre manifest .json; reviso el lock sobre el primer objeto")
    target = manifests[0] if manifests else listing[0]["Key"]

    retention = s3.get_object_retention(Bucket=bucket, Key=target).get("Retention", {})
    print(f"4. Object Lock sobre {target}: mode={retention.get('Mode')} "
          f"hasta={retention.get('RetainUntilDate')}")
    locked = retention.get("Mode") == "GOVERNANCE"

    # El momento de la demo: B2 se niega a borrar lo bloqueado.
    try:
        version = s3.head_object(Bucket=bucket, Key=target).get("VersionId")
        s3.delete_object(Bucket=bucket, Key=target, VersionId=version)
        print("5. FALLO: B2 permitio borrar un objeto con retencion activa")
        return 1
    except ClientError as e:
        code = e.response["Error"].get("Code")
        print(f"5. borrado rechazado por B2 -> {code}  (esto es el momento del video)")

    print()
    print("GO: pipeline -> B2 -> manifest -> Object Lock -> borrado rechazado."
          if locked else
          "GO PARCIAL: subida OK pero la retencion no quedo aplicada; revisar manifest_lock.")
    return 0 if locked else 1


if __name__ == "__main__":
    sys.exit(main())
