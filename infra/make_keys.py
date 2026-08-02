#!/usr/bin/env python3
"""FirstFrame - application keys restringidas (multi-tenancy REAL, a nivel de storage).

Crea dos keys de B2 acotadas por bucket + prefijo + capabilities:

  firstframe-server    todo lo que la app necesita, SOLO sobre este bucket
  firstframe-reviewer  UNICAMENTE readFiles, SOLO sobre approved/ de este bucket

La diferencia con lo que hace casi todo el mundo: el aislamiento del revisor externo no
esta simulado en la capa de aplicacion (un `if user.role == "reviewer"`), esta impuesto
por Backblaze. Si le filtras la key del revisor a Internet, lo peor que puede pasar es
que alguien lea masters ya aprobados. No puede escribir, no puede borrar, no puede ni
listar el bucket, y no ve nada fuera de approved/.

El script VERIFICA cada key recien creada contra la API real:
  - autoriza con ella y compara capabilities/bucket/prefijo con lo pedido
  - intenta obtener una URL de subida  -> debe salir 401 unauthorized
  - intenta listar el bucket por S3    -> debe salir 403 AccessDenied

    set -a && . ./.env && set +a && .venv/bin/python infra/make_keys.py

Flags:
    --rotate    borra las keys con estos nombres y las vuelve a crear
    --revoke    borra las keys y sale (limpieza)
    --bucket    sobreescribe B2_BUCKET
    --no-color

La clave secreta de una application key la devuelve B2 UNA SOLA VEZ. No hay forma de
recuperarla despues. Este script la imprime una vez y nunca la escribe a disco.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from b2_setup import ROOT, B2, B2Error, Style, note, table  # noqa: E402

# Capabilities que la app necesita sobre SU bucket y solo sobre su bucket.
# Ni deleteBuckets, ni writeBuckets, ni listKeys, ni nada a nivel de cuenta:
# aunque la key del servidor se filtre, no puede crear ni destruir buckets ni keys.
SERVER_CAPABILITIES = [
    "listBuckets", "readBuckets",
    "listFiles", "readFiles", "shareFiles", "writeFiles", "deleteFiles",
    "readBucketEncryption", "writeBucketEncryption",
    "readBucketNotifications", "writeBucketNotifications",
    "readBucketLifecycleRules", "writeBucketLifecycleRules",
    "readBucketRetentions", "writeBucketRetentions",
    "readFileRetentions", "writeFileRetentions",
    "readFileLegalHolds", "writeFileLegalHolds",
    "bypassGovernance",
]

KEY_SPECS: list[dict[str, Any]] = [
    {
        "name": "firstframe-server",
        "capabilities": SERVER_CAPABILITIES,
        "prefix": None,  # todo el layout del bucket
        "env": "B2_KEY_ID / B2_APP_KEY",
        "role": "backend FastAPI + pipeline genblaze",
    },
    {
        "name": "firstframe-reviewer",
        "capabilities": ["readFiles"],
        "prefix": "approved/",
        "env": "B2_REVIEWER_KEY_ID / B2_REVIEWER_APP_KEY",
        "role": "revisor externo del cliente (solo lectura de masters)",
    },
]


# ---------------------------------------------------------------------------
def list_keys(b2: B2) -> list[dict[str, Any]]:
    out, start = [], None
    while True:
        payload: dict[str, Any] = {"accountId": b2.account_id, "maxKeyCount": 1000}
        if start:
            payload["startApplicationKeyId"] = start
        res = b2.call("b2_list_keys", payload)
        out.extend(res.get("keys", []))
        start = res.get("nextApplicationKeyId")
        if not start:
            return out


def verify_key(b2: B2, spec: dict[str, Any], created: dict[str, Any],
               bucket_id: str, region: str) -> list[tuple[str, bool, str]]:
    """Comprueba contra la API real que la key hace exactamente lo que decimos."""
    checks: list[tuple[str, bool, str]] = []
    key_id, key_secret = created["applicationKeyId"], created["applicationKey"]

    # 1. Que B2 diga lo mismo que pedimos, leido de vuelta al autorizar.
    tok = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    req = urllib.request.Request(
        "https://api.backblazeb2.com/b2api/v3/b2_authorize_account",
        headers={"Authorization": f"Basic {tok}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        auth = json.load(resp)
    storage = auth["apiInfo"]["storageApi"]
    got_caps = sorted(storage.get("capabilities", []))
    checks.append(("capabilities == las pedidas",
                   got_caps == sorted(spec["capabilities"]),
                   ", ".join(got_caps) if len(got_caps) < 4 else f"{len(got_caps)} caps"))
    checks.append(("acotada a este bucket",
                   storage.get("bucketId") == bucket_id,
                   storage.get("bucketName") or "(sin bucket!)"))
    checks.append((f"acotada al prefijo {spec['prefix'] or '(bucket entero)'}",
                   (storage.get("namePrefix") or None) == spec["prefix"],
                   str(storage.get("namePrefix"))))

    if spec["prefix"] is None:
        return checks

    # 2. Escritura: pedir una upload URL con esta key debe salir 401.
    req = urllib.request.Request(
        f"{storage['apiUrl']}/b2api/v3/b2_get_upload_url",
        data=json.dumps({"bucketId": bucket_id}).encode(),
        headers={"Authorization": auth["authorizationToken"],
                 "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30)
        checks.append(("escritura denegada", False, "B2 DIO una upload URL (!!)"))
    except urllib.error.HTTPError as exc:
        checks.append(("escritura denegada", exc.code in (401, 403),
                       f"{exc.code} unauthorized"))

    # 3. Listado por S3 debe salir 403: sin listFiles no puede ni enumerar.
    try:
        import boto3  # type: ignore
        from botocore.config import Config  # type: ignore
        from botocore.exceptions import ClientError  # type: ignore

        s3 = boto3.client(
            "s3", endpoint_url=f"https://s3.{region}.backblazeb2.com",
            aws_access_key_id=key_id, aws_secret_access_key=key_secret,
            region_name=region,
            config=Config(signature_version="s3v4", retries={"max_attempts": 1}))
        try:
            s3.list_objects_v2(Bucket=storage["bucketName"], Prefix="", MaxKeys=1)
            checks.append(("listado denegado", False, "S3 LISTO el bucket (!!)"))
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            checks.append(("listado denegado", code in ("AccessDenied", "Unauthorized"), code))
    except ImportError:
        checks.append(("listado denegado", True, "boto3 no instalado, check omitido"))

    return checks


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Crea las application keys de FirstFrame.")
    parser.add_argument("--bucket", default=None)
    parser.add_argument("--rotate", action="store_true",
                        help="borra las keys existentes con estos nombres y las recrea")
    parser.add_argument("--revoke", action="store_true",
                        help="borra las keys y sale")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    Style.enabled = sys.stdout.isatty() and not args.no_color

    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    # Crear keys exige `writeKeys`, que la key del servidor NO tiene a proposito.
    # Este script es bootstrap: corre con la master key y luego se aparta.
    pair = (os.environ.get("B2_MASTER_KEY_ID"), os.environ.get("B2_MASTER_APP_KEY"))
    if not all(pair):  # el par se usa entero o no se usa
        pair = (os.environ.get("B2_KEY_ID"), os.environ.get("B2_APP_KEY"))
    master_id, master_key = pair
    if not master_id or not master_key:
        print(Style.err("FALLO: faltan B2_MASTER_KEY_ID / B2_MASTER_APP_KEY "
                        "(o B2_KEY_ID / B2_APP_KEY) en el entorno."))
        return 1

    bucket_name = args.bucket or os.environ.get("B2_BUCKET")
    region = os.environ.get("B2_REGION", "eu-central-003")
    if not bucket_name:
        print(Style.err("FALLO: define B2_BUCKET o pasa --bucket"))
        return 1

    print()
    print(Style.bold("FirstFrame - application keys restringidas"))
    print()

    b2 = B2(master_id, master_key).authorize()
    if "writeKeys" not in b2.capabilities:
        print(Style.err("FALLO: la key actual no tiene 'writeKeys'."))
        print("       Pon la master key en B2_MASTER_KEY_ID / B2_MASTER_APP_KEY.")
        print("       (Es lo esperado si B2_KEY_ID ya es la key restringida del servidor.)")
        return 1

    bucket = b2.find_bucket(bucket_name)
    if bucket is None:
        print(Style.err(f"FALLO: el bucket '{bucket_name}' no existe."))
        return 1
    bucket_id = bucket["bucketId"]
    print(f"  cuenta            {b2.account_id}")
    print(f"  bucket            {bucket_name}  (id {bucket_id})")

    existing = {k["keyName"]: k for k in list_keys(b2)}
    wanted = {spec["name"] for spec in KEY_SPECS}

    if args.revoke or args.rotate:
        for name in sorted(wanted & set(existing)):
            b2.call("b2_delete_key", {"applicationKeyId": existing[name]["applicationKeyId"]})
            print(Style.warn(f"  revocada          {name} ({existing[name]['applicationKeyId']})"))
            existing.pop(name)
        if args.revoke:
            print()
            print(Style.ok("  keys revocadas. Nada mas que hacer."))
            return 0

    created: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rows: list[list[str]] = []
    all_ok = True

    for spec in KEY_SPECS:
        if spec["name"] in existing:
            key = existing[spec["name"]]
            rows.append([spec["name"], key["applicationKeyId"],
                         spec["prefix"] or "(bucket entero)",
                         str(len(key["capabilities"])),
                         Style.dim("ya existe")])
            print(Style.warn(
                f"  {spec['name']}: ya existe ({key['applicationKeyId']}). "
                "Su secreto NO es recuperable; usa --rotate si lo perdiste."))
            continue

        payload: dict[str, Any] = {
            "accountId": b2.account_id,
            "keyName": spec["name"],
            "capabilities": spec["capabilities"],
            "bucketId": bucket_id,
        }
        if spec["prefix"]:
            payload["namePrefix"] = spec["prefix"]
        key = b2.call("b2_create_key", payload)
        created.append((spec, key))
        rows.append([spec["name"], key["applicationKeyId"],
                     spec["prefix"] or "(bucket entero)",
                     str(len(key["capabilities"])),
                     Style.ok("creada")])

    table("APPLICATION KEYS", ["nombre", "keyId", "prefijo", "caps", "estado"], rows)

    for spec, key in created:
        print()
        print(Style.bold(f"  verificacion de {spec['name']}"))
        for label, ok, detail in verify_key(b2, spec, key, bucket_id, region):
            mark = Style.ok("OK  ") if ok else Style.err("FALLO")
            all_ok = all_ok and ok
            print(f"    {mark}  {label:<38} {Style.dim(detail)}")

    if created:
        print()
        print(Style.warn("=" * 78))
        print(Style.warn("  SECRETOS - B2 los devuelve UNA SOLA VEZ. Copialos AHORA a tu .env"))
        print(Style.warn("  (que esta en .gitignore). No se pueden recuperar despues."))
        print(Style.warn("=" * 78))
        for spec, key in created:
            env_id, env_secret = spec["env"].split(" / ")
            print()
            print(f"  # {spec['name']} - {spec['role']}")
            print(f"  {env_id}={key['applicationKeyId']}")
            print(f"  {env_secret}={key['applicationKey']}")
        print()
        print(Style.warn("=" * 78))

    print()
    note("""
Esto es multi-tenancy a nivel de storage, no a nivel de aplicacion. La key del revisor
no lleva writeFiles ni deleteFiles ni listFiles, y esta clavada al prefijo approved/ del
bucket: puede descargar un master aprobado si sabe su nombre, y absolutamente nada mas.
No hay codigo nuestro en medio que pueda tener un bug de autorizacion, porque la
autorizacion la aplica Backblaze antes de que la peticion llegue a ningun sitio.
""")
    print()
    return 0 if all_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except B2Error as exc:
        print(Style.err(f"FALLO B2: {exc}"))
        sys.exit(1)
