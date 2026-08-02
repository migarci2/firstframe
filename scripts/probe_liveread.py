#!/usr/bin/env python3
"""Go/no-go de B2 Live Read. Correr ANTES de construir nada encima.

Uso:
    export B2_KEY_ID=... B2_APP_KEY=... B2_BUCKET=... [B2_REGION=us-west-004]
    .venv/bin/python scripts/probe_liveread.py

Verifica lo unico que importa: que se pueda LEER un objeto mientras todavia
se esta subiendo. Sube 2 partes de 5MB con una pausa en medio y, entre parte
y parte, intenta leer un rango de la parte que aun no existe (debe dar 416)
y otro de la que si (debe dar los bytes).
"""
import hashlib
import os
import sys

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# B2 exige partes de >=5MB y todas iguales salvo la ultima.
PART = 5 * 1024 * 1024
LIVE_READ = {"x-backblaze-live-read-enabled": "true"}
KEY = "probe/liveread-check.bin"


def _client():
    region = os.environ.get("B2_REGION", "us-west-004")
    return boto3.client(
        "s3",
        endpoint_url=f"https://s3.{region}.backblazeb2.com",
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APP_KEY"],
        region_name=region,
        config=Config(signature_version="s3v4"),
    )


def _get_range(s3, bucket, first, last):
    """Devuelve (status, body). status 416 = ese rango aun no existe.

    El header live-read del lector lo inyecta el handler before-send en main();
    sin el, B2 trata esto como un GET normal y el objeto incompleto no existe.
    """
    try:
        resp = s3.get_object(Bucket=bucket, Key=KEY, Range=f"bytes={first}-{last}")
        return 200, resp["Body"].read()
    except ClientError as e:
        return int(e.response["ResponseMetadata"]["HTTPStatusCode"]), b""


def main():
    missing = [v for v in ("B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET") if not os.environ.get(v)]
    if missing:
        sys.exit(f"faltan env vars: {', '.join(missing)}")

    bucket = os.environ["B2_BUCKET"]
    s3 = _client()

    # El header custom viaja en la peticion HTTP, no como parametro de boto3:
    # se inyecta en el evento before-send del cliente.
    def _inject(request, **_):
        request.headers.add_header("x-backblaze-live-read-enabled", "true")

    s3.meta.events.register_first("before-send.s3.CreateMultipartUpload", _inject)
    s3.meta.events.register_first("before-send.s3.GetObject", _inject)

    print(f"bucket={bucket} key={KEY}")
    try:
        mpu = s3.create_multipart_upload(Bucket=bucket, Key=KEY)
    except ClientError as e:
        sys.exit(f"NO-GO: CreateMultipartUpload rechazado -> {e}")
    upload_id = mpu["UploadId"]
    print(f"  multipart iniciado: {upload_id[:24]}...")

    part1 = b"A" * PART
    part2 = b"B" * PART
    parts = []

    try:
        r = s3.upload_part(Bucket=bucket, Key=KEY, PartNumber=1,
                           UploadId=upload_id, Body=part1)
        parts.append({"ETag": r["ETag"], "PartNumber": 1})
        print("  parte 1/2 subida (objeto AUN INCOMPLETO)")

        # 1) leer dentro de la parte 1: debe funcionar aunque el objeto no este cerrado.
        status, body = _get_range(s3, bucket, 0, 15)
        live_read_ok = status == 200 and body == b"A" * 16
        print(f"  GET rango 0-15  -> {status} {'OK' if live_read_ok else 'FALLO'}")

        # 2) leer en la parte 2, que todavia no existe: la API define 416.
        status2, _ = _get_range(s3, bucket, PART + 10, PART + 25)
        print(f"  GET rango futuro -> {status2} {'(416 esperado)' if status2 == 416 else ''}")

        r = s3.upload_part(Bucket=bucket, Key=KEY, PartNumber=2,
                           UploadId=upload_id, Body=part2)
        parts.append({"ETag": r["ETag"], "PartNumber": 2})
        s3.complete_multipart_upload(Bucket=bucket, Key=KEY, UploadId=upload_id,
                                     MultipartUpload={"Parts": parts})
        print("  multipart completado")

        head = s3.head_object(Bucket=bucket, Key=KEY)
        assert head["ContentLength"] == 2 * PART, head["ContentLength"]
        digest = hashlib.sha256(part1 + part2).hexdigest()[:12]
        print(f"  objeto final: {head['ContentLength']} bytes, sha256 {digest}...")
    finally:
        try:
            s3.abort_multipart_upload(Bucket=bucket, Key=KEY, UploadId=upload_id)
        except ClientError:
            pass  # ya completado

    print()
    if live_read_ok and status2 == 416:
        print("GO: Live Read funciona (lectura parcial OK + 416 en rango futuro).")
        return 0
    if live_read_ok:
        print("GO PARCIAL: se lee incompleto, pero el rango futuro dio "
              f"{status2} en vez de 416. Revisa el reintento del player.")
        return 0
    print("NO-GO: no se pudo leer el objeto mientras se subia.")
    print("       -> plan B: preview por partes ya cerradas (chunked), mismo efecto en camara.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
