#!/usr/bin/env python3
"""End-to-end REAL: prompt -> imagen generada -> Genblaze -> B2 -> manifest verificado.

Esta es la prueba de que la demo NO enseña generacion simulada. A diferencia de
`probe_spine.py` (que sube PNGs de ffmpeg via PassthroughProvider), aqui el step 0
es `PollinationsProvider`: sale a internet, genera pixeles que no existian, y el
pipeline de Genblaze los sube con su manifest.

Ademas ejercita `fallback_models=`, que solo salta ante
`ProviderErrorCode.MODEL_ERROR`: el step 1 pide un modelo inexistente a proposito
y el SDK cae al bueno.

    set -a && . ./.env && set +a && .venv/bin/python scripts/probe_free_provider.py

Modos:
    (sin flags)  sube de verdad a B2 (subir es Class A: gratis y sin cap)
    --local      salta B2 y verifica solo el camino pipeline -> sink -> manifest

Codigos de salida:
    0  todo OK
    1  fallo de verdad (mi codigo o el provider)
    2  todo subido y manifest verificado, pero NO se pudo releer de B2: la
       cuenta tiene el cap de transacciones alcanzado (LIST/GET dan 403). No
       es un fallo de codigo: subir el cap en Backblaze > Caps & Alerts.

Notas que cuestan una hora si se olvidan:
  - `Pipeline(..., preflight=False)` SIEMPRE: el preflight usa `validate_model()`,
    que esta invertido (issue #248).
  - `ObjectStorageSink` es de UN SOLO USO: se cierra al terminar el run.
  - El tier anonimo de Pollinations serializa a 1 request por IP, asi que N
    escenas tardan ~N x 45s (medido). Con 2 steps, ~90s de reloj en frio.
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, BinaryIO

import boto3
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genblaze import Modality, ObjectStorageSink, Pipeline  # noqa: E402
from genblaze_core.exceptions import StorageError  # noqa: E402
from genblaze_core.models.manifest import parse_manifest  # noqa: E402
from genblaze_core.storage.base import StorageBackend  # noqa: E402
from genblaze_s3 import S3StorageBackend  # noqa: E402

from pipeline.free_provider import PollinationsProvider  # noqa: E402

PREFIX = "probe/free-provider"

# Dos prompts con look de escena de video, no de test unitario: si la calidad no
# da el nivel para grabar, se ve aqui.
PROMPTS = [
    "cinematic wide shot of a modern data center at night, endless rows of "
    "servers glowing deep blue, volumetric light beams, shallow depth of field, "
    "anamorphic lens flare, film grain, 35mm",
    "close-up portrait of a focused software engineer lit only by three monitors, "
    "warm rim light, dark office background, bokeh, cinematic color grading, 85mm",
]

SAMPLES_DIR = Path("/tmp/free-provider-samples")


class MemoryStorageBackend(StorageBackend):
    """Backend en memoria para verificar el camino sink -> manifest sin red.

    Para `--local`: permite verificar el camino completo pipeline -> sink ->
    manifest sin gastar una sola transaccion de Backblaze (util con la cuenta
    capada, y para iterar rapido). Cumple el mismo contrato que
    S3StorageBackend, asi que lo que verifica aqui verifica en B2.
    """

    def __init__(self, bucket: str = "memory") -> None:
        self._bucket = bucket
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes | BinaryIO, *, content_type: str | None = None,
            metadata: dict[str, str] | None = None,
            extra_args: dict[str, Any] | None = None) -> str:
        self.objects[key] = data if isinstance(data, bytes) else data.read()
        return key

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def exists(self, key: str) -> bool:
        return key in self.objects

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def get_url(self, key: str, *, expires_in: int = 3600) -> str:
        return f"https://{self._bucket}.invalid/{key}"

    def get_durable_url(self, key: str) -> str:
        return f"https://{self._bucket}.invalid/{key}"


def _s3():
    region = os.environ["B2_REGION"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://s3.{region}.backblazeb2.com",
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APP_KEY"],
        region_name=region,
        config=Config(signature_version="s3v4"),
    )


def _is_cap_error(exc: Exception) -> bool:
    """True si el error es el cap de transacciones de la cuenta de Backblaze."""
    return "transaction cap exceeded" in str(exc).lower()


def _make_b2_backend() -> tuple[StorageBackend | None, str]:
    """(backend, motivo). backend=None cuando B2 no esta disponible.

    Con la cuenta capada hay que esquivar DOS HeadBucket, ambos Class B (que es
    justo lo que esta capado) y ninguno necesario para subir:
      - `preflight=True` (default) HEADea el bucket al construir.
      - `put()` llama a `_ensure_region_verified()`, que HEADea otra vez para
        autodetectar la region por redirect. Pasamos `region=` explicita, asi
        que no hay nada que autodetectar.
    Marcamos la region como verificada a mano. Verificado: con esto `put()`
    sube a B2 correctamente aun con el cap alcanzado (subir es Class A, gratis).
    """
    try:
        backend = S3StorageBackend.for_backblaze(
            os.environ["B2_BUCKET"], region=os.environ["B2_REGION"],
            key_id=os.environ["B2_KEY_ID"], app_key=os.environ["B2_APP_KEY"],
            preflight=False,
        )
        backend._region_verified = True  # noqa: SLF001 — ver docstring
        return backend, "ok"
    except (StorageError, ValueError) as exc:
        return None, str(exc)


def _save_samples(provider: PollinationsProvider, result: Any) -> list[Path]:
    """Copia las imagenes generadas a /tmp para juzgar la calidad a ojo.

    El sink reescribe `asset.url` a la URL durable del backend, pero `sha256`
    sobrevive — y el provider nombra sus ficheros por sha256[:16], asi que el
    fichero local se localiza sin depender de la url.
    """
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    # PipelineResult expone .run/.manifest, NO .steps directamente.
    for i, step in enumerate(result.run.steps):
        for asset in step.assets or []:
            matches = list(provider.output_dir.glob(f"{(asset.sha256 or 'x')[:16]}.*"))
            if matches:
                dst = SAMPLES_DIR / f"scene{i}{matches[0].suffix}"
                shutil.copyfile(matches[0], dst)
                saved.append(dst)
    return saved


def main() -> int:
    force_local = "--local" in sys.argv
    provider = PollinationsProvider()
    bucket = os.environ.get("B2_BUCKET", "?")

    print(f"0. provider={provider.name}  tier={'token' if provider.token else 'anonymous'}")

    backend: StorageBackend | None = None
    b2_reason = "forzado --local"
    if not force_local:
        backend, b2_reason = _make_b2_backend()
    using_b2 = backend is not None
    if using_b2:
        print(f"   backend=B2 b2://{bucket} ({os.environ['B2_REGION']})")
    else:
        capped = "transaction_cap_exceeded" in b2_reason or "403" in b2_reason
        print(f"   backend=memoria — B2 NO disponible: "
              f"{'CUENTA CAPADA (transaction_cap_exceeded)' if capped else b2_reason[:120]}")
        backend = MemoryStorageBackend(bucket)

    # ObjectStorageSink es de un solo uso: uno por run, nunca reutilizar.
    sink = ObjectStorageSink(backend, prefix=PREFIX)

    # preflight=False: el preflight usa validate_model(), invertido (issue #248).
    pipe = Pipeline(name="free-provider-probe", preflight=False)
    # Step 0: modelo bueno.
    pipe.step(provider, model="flux", modality=Modality.IMAGE,
              prompt=PROMPTS[0], params={"seed": 1234})
    # Step 1: modelo inexistente -> MODEL_ERROR -> fallback_models salta a "sana".
    pipe.step(provider, model="modelo-que-no-existe", fallback_models=["sana"],
              modality=Modality.IMAGE, prompt=PROMPTS[1], params={"seed": 4321})

    started = time.monotonic()
    result = pipe.run(sink=sink, raise_on_failure=True)
    elapsed = time.monotonic() - started
    print(f"1. pipeline OK en {elapsed:.1f}s  run_id={result.run.run_id}  "
          f"({len(PROMPTS)} imagenes generadas de verdad, la 2a via fallback_models)")
    ok_steps = result.succeeded_steps()
    if len(ok_steps) != len(PROMPTS):
        print(f"FALLO: {len(ok_steps)}/{len(PROMPTS)} steps OK — "
              f"{result.error_summary()}")
        return 1

    saved = _save_samples(provider, result)
    print("2. muestras para juzgar calidad:")
    for p in saved:
        print(f"     {p}  ({p.stat().st_size:,} B)")
    if not saved:
        print("     FALLO: no encontre las imagenes locales")
        return 1

    # --- Objetos subidos ----------------------------------------------------
    # Las URLs de los assets ya vienen reescritas por el sink a la URL durable
    # del backend: eso es la prueba de que el sink los subio.
    uploaded = [a.url for s in result.run.steps for a in (s.assets or [])]
    read_back_capped = False
    if using_b2:
        try:
            listing = [(o["Key"], o["Size"])
                       for o in _s3().list_objects_v2(
                           Bucket=bucket, Prefix=PREFIX).get("Contents", [])]
        except Exception as exc:  # noqa: BLE001
            if not _is_cap_error(exc):
                raise
            # LIST es Class C y esta capado; la subida (Class A) si funciono.
            read_back_capped = True
            listing = []
        where = f"b2://{bucket}/{PREFIX}/"
    else:
        listing = [(k, len(v)) for k, v in backend.objects.items()]
        where = "memoria"

    if read_back_capped:
        print(f"3. {len(uploaded)} assets subidos a {where} "
              f"(LIST bloqueado por el cap, no puedo releer)")
        for url in uploaded[:10]:
            print(f"     {url}")
    else:
        print(f"3. {len(listing)} objetos en {where}")
        for key, size in listing[:10]:
            print(f"     {size:>9,} B  {key}")
        if not listing:
            print("FALLO: el sink no subio nada")
            return 1
        images = [(k, s) for k, s in listing if not k.endswith(".json")]
        if len(images) < len(PROMPTS):
            print(f"FALLO: esperaba {len(PROMPTS)} imagenes, hay {len(images)}")
            return 1
        if any(s < 1024 for _, s in images):
            print("FALLO: alguna imagen subida pesa menos de 1 KB")
            return 1

    if len(uploaded) < len(PROMPTS):
        print(f"FALLO: esperaba {len(PROMPTS)} assets subidos, hay {len(uploaded)}")
        return 1

    # --- Manifest -----------------------------------------------------------
    # Con el cap activo no se puede releer de B2, asi que se verifica el
    # manifest que el sink serializo (`result.manifest`) — el MISMO objeto que
    # subio, byte por byte.
    manifests = [k for k, _ in listing if k.endswith(".json")]
    source = "releido de B2" if using_b2 else "releido del backend en memoria"
    if manifests and not read_back_capped:
        key = manifests[0]
        body = (_s3().get_object(Bucket=bucket, Key=key)["Body"].read()
                if using_b2 else backend.get(key))
        manifest = parse_manifest(json.loads(body))
    elif read_back_capped:
        manifest = result.manifest
        key = f"manifests/{result.run.run_id}.json"
        source = "en memoria (GET bloqueado por el cap)"
    else:
        print("FALLO: el sink no dejo manifest .json")
        return 1

    ok = manifest.verify()
    missing = manifest.output_asset_ids_missing_sha256()
    print(f"4. manifest {key}  [{source}]")
    print(f"     verify()           -> {ok}")
    print(f"     hash canonico      -> {manifest.verify_hash()}")
    print(f"     outputs sin sha256 -> {missing or 'ninguno'}")
    if not ok or missing:
        print("FALLO: el manifest no verifica (sha256 de los outputs)")
        return 1

    print()
    if using_b2 and not read_back_capped:
        print("GO: prompt -> imagen REAL -> pipeline Genblaze -> B2 -> manifest verificado.")
        rc = 0
    elif using_b2:
        print("GO: prompt -> imagen REAL -> pipeline Genblaze -> B2. Las imagenes y el")
        print("    manifest ESTAN subidos (subir es Class A, gratis y sin cap) y el")
        print("    manifest verifica.")
        print("AVISO: la cuenta de Backblaze tiene el cap de transacciones alcanzado,")
        print("  asi que LIST y GET devuelven 403 y no se puede releer lo subido")
        print("  (tampoco desde la consola web). Subir el cap en Backblaze >")
        print("  Caps & Alerts y volver a lanzar para la verificacion completa.")
        rc = 2
    else:
        print("GO PARCIAL: prompt -> imagen REAL -> pipeline Genblaze -> sink -> "
              "manifest verificado, pero SIN tocar B2 (backend en memoria).")
        print(f"  Motivo: {b2_reason[:150]}")
        print("  Lanza sin --local para subir de verdad a Backblaze.")
        rc = 2
    print(f"    Juzga la calidad en: {SAMPLES_DIR}/")
    return rc


if __name__ == "__main__":
    sys.exit(main())
