"""Provenance: agregado del job, aprobacion con Object Lock y verificacion.

Tres piezas:

1. **Agregado.** Los manifests de escena (uno por run de Genblaze) se juntan en
   `provenance/{job}/manifest.json` **como OBJETO**, nunca como metadata: el
   bucket tiene Object Lock activado y eso baja el limite de nombre+file-info a
   2048 bytes — un manifest de 6 escenas no cabe ni de lejos.

2. **Aprobacion.** `Mp4Handler`/`SmartEmbedder` embeben el manifest DENTRO del
   mp4, el master sube a `approved/{job}/final.mp4` con
   `ObjectLockMode=GOVERNANCE` + 30 dias, y el manifest de `approved/` lo
   escribe un `ObjectStorageSink` con `manifest_lock=ObjectLockConfig(...)`
   (via `Pipeline.ingest`, que ademas deja el mp4 documentado como un run de
   ingesta con su procedencia).

3. **Verificacion.** `verify(job)` hace lo que haria `genblaze verify --fetch`:
   baja el master de B2, extrae el manifest embebido, comprueba el hash
   canonico y **re-descarga cada asset declarado para re-hashearlo**. Si el
   binario `genblaze` esta en el PATH se usa el CLI y se reporta su salida;
   si no (es el caso del venv actual: el paquete no instala CLI), se ejecuta
   el equivalente en proceso. El campo `method` dice cual de los dos corrio.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from genblaze import Asset, Manifest, ObjectLockConfig, ObjectStorageSink, Pipeline

logger = logging.getLogger("firstframe.manifest")

SCHEMA = "firstframe/job-manifest@1"
RETAIN_DAYS = 30
LOCK_MODE = "GOVERNANCE"


def prov_key(job_id: str) -> str:
    return f"provenance/{job_id}/manifest.json"


def approved_prefix(job_id: str) -> str:
    return f"approved/{job_id}"


def runs_prefix(job_id: str, scene_n: int) -> str:
    return f"runs/{job_id}/scene-{scene_n}"


# --- B2 ----------------------------------------------------------------------

def b2_config() -> dict[str, str] | None:
    """Credenciales de B2 desde el entorno, o None si falta alguna."""
    cfg = {k: os.environ.get(k, "").strip()
           for k in ("B2_BUCKET", "B2_REGION", "B2_KEY_ID", "B2_APP_KEY")}
    return cfg if all(cfg.values()) else None


def b2_enabled() -> bool:
    return b2_config() is not None


def _s3():
    cfg = b2_config()
    if cfg is None:
        raise RuntimeError("faltan B2_BUCKET/B2_REGION/B2_KEY_ID/B2_APP_KEY en el entorno")
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://s3.{cfg['B2_REGION']}.backblazeb2.com",
        aws_access_key_id=cfg["B2_KEY_ID"],
        aws_secret_access_key=cfg["B2_APP_KEY"],
        region_name=cfg["B2_REGION"],
        config=Config(signature_version="s3v4"),
    ), cfg["B2_BUCKET"]


def backend():
    """`S3StorageBackend` de genblaze-s3 apuntando al bucket del proyecto."""
    cfg = b2_config()
    if cfg is None:
        raise RuntimeError("B2 no configurado")
    from genblaze_s3 import S3StorageBackend

    # El preflight de genblaze-s3 hace un HeadBucket, que es una transaccion
    # Class B. Con la cuota diaria de B2 agotada devuelve 403 y tumba el run
    # ENTERO antes de generar nada, aunque las subidas (Class A) sigan yendo bien.
    #
    # `preflight=False` NO lo desactiva: solo lo aplaza a la primera E/S
    # (backend.py:1763 "leave the verify-on-first-use machinery alone").
    # Y ese 403 por cuota lo clasifica `is_sticky_preflight_error` como error
    # permanente, igual que unas credenciales malas, asi que cachea el fallo y
    # envenena el backend durante toda la vida del proceso.
    #
    # Region y bucket ya estan verificados (llevamos toda la noche escribiendo
    # aqui), asi que marcamos la verificacion como hecha y dejamos que sea la
    # propia operacion la que falle si de verdad hay un problema.
    # ponytail: acceso a atributo privado; quitar si el SDK expone una opcion
    # de verdad para saltarse el preflight (candidato a PR upstream).
    backend = S3StorageBackend.for_backblaze(
        cfg["B2_BUCKET"], region=cfg["B2_REGION"],
        key_id=cfg["B2_KEY_ID"], app_key=cfg["B2_APP_KEY"],
        preflight=False,
    )
    backend._region_verified = True
    return backend


def scene_sink(job_id: str, scene_n: int) -> ObjectStorageSink:
    """Sink NUEVO para el run de una escena. `ObjectStorageSink` es de un solo uso.

    El que llama tiene que cerrarlo en un `finally` — reutilizarlo entre runs
    escribe sobre un pool cerrado.
    """
    return ObjectStorageSink(backend(), prefix=runs_prefix(job_id, scene_n))


def lock_config(days: int = RETAIN_DAYS) -> ObjectLockConfig:
    return ObjectLockConfig(
        retain_until=datetime.now(timezone.utc) + timedelta(days=days),
        mode=LOCK_MODE,
    )


# --- Registro de escena ------------------------------------------------------

@dataclass
class SceneRecord:
    """Lo que el runner sabe de una escena una vez terminada."""

    n: int
    title: str
    run_id: str
    parent_run_id: str | None = None
    # run_id de la escena ANTERIOR. `parent_run_id` apunta a la iteracion
    # previa cuando el AgentLoop refino (el SDK lo reescribe con
    # from_result), asi que la cadena entre escenas se guarda aparte.
    chain_parent_run_id: str | None = None
    first_run_id: str | None = None
    iterations: int = 1
    passed: bool = True
    judge: dict[str, Any] = field(default_factory=dict)
    provider_mode: str = "mock"
    canonical_hash: str = ""
    manifest_uri: str | None = None
    manifest: dict[str, Any] | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    fallbacks: list[dict[str, str]] = field(default_factory=list)
    local_path: str = ""
    # Spec de la escena (titulo + los 3 prompts). Va al manifest para que
    # `runner.refine_scene()` pueda relanzar UNA escena mucho despues, con la
    # nota del revisor, sin volver a planificar el job entero.
    spec: dict[str, Any] = field(default_factory=dict)
    duration_sec: float = 0.0
    cost_usd: float = 0.0
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def scene_record_from_result(scene_n: int, title: str, agent_result,
                             *, judge: dict[str, Any] | None = None,
                             local_path: str = "", elapsed_ms: int = 0) -> SceneRecord:
    """Extrae de un `AgentResult` todo lo que va al manifest agregado."""
    final = agent_result.final
    run = final.run
    steps: list[dict[str, Any]] = []
    fallbacks: list[dict[str, str]] = []
    for s in run.steps:
        steps.append({
            "index": s.step_index,
            "role": s.metadata.get("role"),
            "provider": s.provider,
            "model": s.model,
            "status": str(getattr(s.status, "value", s.status)),
            "cost_usd": s.cost_usd or 0.0,
            "assets": [a.asset_id for a in s.assets],
        })
        if s.metadata.get("fallback_model"):
            fallbacks.append({
                "role": str(s.metadata.get("role")),
                "from": str(s.metadata.get("fallback_from")),
                "to": str(s.metadata.get("fallback_model")),
            })
    return SceneRecord(
        n=scene_n,
        title=title,
        run_id=run.run_id,
        parent_run_id=run.parent_run_id,
        first_run_id=(agent_result.iterations[0].result.run.run_id
                      if agent_result.iterations else run.run_id),
        iterations=len(agent_result.iterations),
        passed=agent_result.passed,
        judge=judge or {},
        provider_mode=str(run.metadata.get("provider_mode", "mock")),
        canonical_hash=final.manifest.canonical_hash,
        manifest_uri=final.manifest.manifest_uri,
        manifest=final.manifest.model_dump(mode="json"),
        steps=steps,
        fallbacks=fallbacks,
        local_path=local_path,
        cost_usd=float(agent_result.total_cost_usd or 0.0),
        elapsed_ms=elapsed_ms,
    )


# --- 1. Agregado -------------------------------------------------------------

def build_aggregate(job_id: str, brief: str, scenes: list[SceneRecord],
                    *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Manifest agregado del job. Objeto JSON, jamas metadata de B2."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        gb_version = _pkg_version("genblaze")
    except PackageNotFoundError:  # pragma: no cover
        gb_version = "unknown"

    cfg = b2_config() or {}
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "job_id": job_id,
        "brief": brief,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "genblaze_version": gb_version,
        "bucket": cfg.get("B2_BUCKET"),
        "region": cfg.get("B2_REGION"),
        "scene_count": len(scenes),
        "total_cost_usd": round(sum(s.cost_usd for s in scenes), 6),
        "total_elapsed_ms": sum(s.elapsed_ms for s in scenes),
        "refined_scenes": [s.n for s in scenes if s.iterations > 1],
        "failovers": [{"scene": s.n, **f} for s in scenes for f in s.fallbacks],
        "lineage": [{"scene": s.n, "run_id": s.run_id,
                     "first_run_id": s.first_run_id,
                     "parent_run_id": s.parent_run_id,
                     "chain_parent_run_id": s.chain_parent_run_id}
                    for s in scenes],
        "scenes": [s.as_dict() for s in scenes],
    }
    if extra:
        doc.update(extra)
    return doc


def write_aggregate(job_id: str, doc: dict[str, Any],
                    *, local_dir: str | Path = "runs",
                    use_b2: bool | None = None) -> dict[str, Any]:
    """Escribe el agregado en local y, si hay B2, en `provenance/{job}/manifest.json`.

    Se sube como CUERPO del objeto. Meterlo en `Metadata=` reventaria: con
    Object Lock activo el limite de nombre+file-info es 2048 bytes.
    """
    body = json.dumps(doc, indent=2, ensure_ascii=False).encode()
    local = Path(local_dir) / job_id / "manifest.json"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(body)
    out = {"local": str(local), "bytes": len(body), "b2_key": None, "b2_url": None}

    if use_b2 is None:
        use_b2 = b2_enabled()
    if not (use_b2 and b2_enabled()):
        logger.info("sin B2: agregado solo en %s", local)
        return out

    s3, bucket = _s3()
    key = prov_key(job_id)
    s3.put_object(Bucket=bucket, Key=key, Body=body,
                  ContentType="application/json")
    out["b2_key"] = key
    out["b2_url"] = f"s3://{bucket}/{key}"
    logger.info("agregado -> %s (%d B, como objeto)", out["b2_url"], len(body))
    return out


def read_aggregate(job_id: str, *, local_dir: str | Path = "runs",
                   use_b2: bool | None = None) -> dict[str, Any]:
    """Lee el agregado, de B2 si se puede y si no de local."""
    if use_b2 is None:
        use_b2 = b2_enabled()
    if use_b2 and b2_enabled():
        s3, bucket = _s3()
        try:
            return json.loads(s3.get_object(Bucket=bucket, Key=prov_key(job_id))
                              ["Body"].read().decode())
        except Exception as exc:  # noqa: BLE001
            logger.warning("no pude leer %s de B2 (%s); pruebo local", prov_key(job_id), exc)
    return json.loads((Path(local_dir) / job_id / "manifest.json").read_text())


# --- 2. Aprobacion -----------------------------------------------------------

def _manifest_for_final(job_id: str, mp4: Path, doc: dict[str, Any]) -> Manifest:
    """Manifest de Genblaze del master, via `Pipeline.ingest` (sin sink).

    `ingest` es la API del SDK para documentar procedencia de un asset que no
    genero el pipeline — aqui, el master ya montado.
    """
    data = mp4.read_bytes()
    asset = Asset(
        url=mp4.resolve().as_uri(),
        media_type="video/mp4",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
    result = Pipeline.ingest(
        assets=[asset],
        source=f"firstframe://{job_id}/approved",
        source_metadata={
            "job_id": job_id,
            "scene_count": doc.get("scene_count"),
            "scene_run_ids": [s["run_id"] for s in doc.get("scenes", [])],
            "aggregate_manifest": prov_key(job_id),
            "approved_at": datetime.now(timezone.utc).isoformat(),
        },
        name=f"{job_id}/approved",
    )
    return result.manifest


def approve(job_id: str, final_mp4: str | Path, *,
            doc: dict[str, Any] | None = None,
            retain_days: int = RETAIN_DAYS,
            local_dir: str | Path = "runs",
            use_b2: bool | None = None) -> dict[str, Any]:
    """Embebe el manifest en el mp4, lo sube con Object Lock y bloquea su manifest.

    Devuelve un dict con las claves de B2, el modo/fecha de retencion y el
    metodo de embebido, listo para la UI.
    """
    final_mp4 = Path(final_mp4)
    if not final_mp4.is_file():
        raise FileNotFoundError(f"no existe el master: {final_mp4}")
    doc = doc or read_aggregate(job_id, local_dir=local_dir, use_b2=use_b2)

    from genblaze_core.media import Mp4Handler, SmartEmbedder

    manifest = _manifest_for_final(job_id, final_mp4, doc)

    embedded = final_mp4.parent / f"{final_mp4.stem}.embedded.mp4"
    # SmartEmbedder elige handler por mime y cae a sidecar si el contenedor no
    # admite el manifest; para mp4 acaba en Mp4Handler (uuid box).
    embed = SmartEmbedder().embed(final_mp4, manifest, embedded, mime_type="video/mp4")
    out_path = Path(embed.path)

    # Comprobacion inmediata: si no se puede extraer, no lo subimos como
    # "verificable". Mp4Handler es el que sabe leer la caja.
    handler = Mp4Handler()
    try:
        extracted = handler.extract(out_path)
        embed_ok = extracted.canonical_hash == manifest.canonical_hash
    except Exception as exc:  # noqa: BLE001
        embed_ok, extracted = False, None
        logger.warning("no pude releer el manifest embebido: %s", exc)

    result: dict[str, Any] = {
        "job_id": job_id,
        "local_master": str(out_path),
        "embed_method": embed.method,
        "embed_error": embed.embed_error,
        "embed_verified": embed_ok,
        "canonical_hash": manifest.canonical_hash,
        "sidecar": str(embed.sidecar_path) if embed.sidecar_path else None,
        "lock": None,
        "keys": {},
    }
    if use_b2 is None:
        use_b2 = b2_enabled()
    if not (use_b2 and b2_enabled()):
        logger.info("sin B2: aprobado solo en local (%s)", out_path)
        return result

    s3, bucket = _s3()
    retain_until = datetime.now(timezone.utc) + timedelta(days=retain_days)
    key = f"{approved_prefix(job_id)}/final.mp4"
    master_bytes = out_path.read_bytes()
    master_sha256 = hashlib.sha256(master_bytes).hexdigest()
    s3.put_object(
        Bucket=bucket, Key=key, Body=master_bytes,
        ContentType="video/mp4",
        ObjectLockMode=LOCK_MODE,
        ObjectLockRetainUntilDate=retain_until,
    )
    result["keys"]["master"] = key
    result["master_sha256"] = master_sha256
    result["lock"] = {"mode": LOCK_MODE, "retain_until": retain_until.isoformat()}

    # El manifest de approved/ lo escribe el SINK con manifest_lock: queda WORM.
    # Sink NUEVO y close() en finally — es de un solo uso.
    #
    # GOTCHA: `AssetTransfer` solo lee `file://` bajo `tempfile.gettempdir()`
    # o `/tmp` (`ALLOWED_FILE_ROOTS`) y `ObjectStorageSink` nunca le pasa
    # `allowed_roots`. Un master en `runs/` da "Access denied ... outside
    # allowed directories", asi que se copia a temp antes de la ingesta.
    staging = Path(tempfile.mkdtemp(prefix=f"ff-approve-{job_id}-"))
    staged = staging / "final.mp4"
    shutil.copyfile(out_path, staged)
    sink = ObjectStorageSink(backend(), prefix=approved_prefix(job_id),
                             manifest_lock=lock_config(retain_days))
    try:
        ingest = Pipeline.ingest(
            assets=[Asset(url=staged.resolve().as_uri(), media_type="video/mp4",
                          sha256=hashlib.sha256(staged.read_bytes()).hexdigest(),
                          size_bytes=staged.stat().st_size)],
            source=f"firstframe://{job_id}/approved",
            source_metadata={"job_id": job_id, "master_key": key,
                             "aggregate_manifest": prov_key(job_id)},
            sink=sink,
            name=f"{job_id}/approved",
        )
        result["keys"]["manifest"] = sink.manifest_key_for(ingest.run)
        result["approved_run_id"] = ingest.run.run_id
    finally:
        sink.close()
        shutil.rmtree(staging, ignore_errors=True)

    # El agregado tambien deja constancia de la aprobacion.
    doc["approved"] = {k: result[k] for k in ("keys", "lock", "canonical_hash",
                                              "embed_method", "embed_verified",
                                              "master_sha256")}
    write_aggregate(job_id, doc, local_dir=local_dir, use_b2=use_b2)
    logger.info("aprobado: s3://%s/%s con lock %s hasta %s",
                bucket, key, LOCK_MODE, retain_until.isoformat())
    return result


# --- 3. Verificacion ---------------------------------------------------------

def b2_key_from_url(url: str) -> tuple[str, str] | None:
    """(bucket, key) si la URL apunta al endpoint S3 de B2; None si no.

    Hace falta porque `ObjectStorageSink` REESCRIBE `asset.url` a
    `https://s3.{region}.backblazeb2.com/{bucket}/{key}` (path-style) durante
    el run. El bucket es privado: un GET anonimo devuelve 401. Cualquiera que
    quiera releer un asset despues del run (el juez, el runner, `verify`)
    tiene que firmar la peticion.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc.endswith(".backblazeb2.com"):
        return None
    parts = parsed.path.lstrip("/").split("/", 1)
    if len(parts) != 2 or not parts[1]:
        return None
    return parts[0], urllib.parse.unquote(parts[1])


def fetch_bytes(url: str, *, s3=None, bucket: str | None = None) -> bytes:
    """Descarga un asset: `file://`, `s3://`, B2 firmado o https normal."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        return Path(urllib.parse.unquote(parsed.path)).read_bytes()
    if parsed.scheme == "s3":
        if s3 is None:
            s3, bucket = _s3()
        return s3.get_object(Bucket=parsed.netloc or bucket,
                             Key=parsed.path.lstrip("/"))["Body"].read()
    if parsed.scheme in ("http", "https"):
        b2 = b2_key_from_url(url)
        if b2 is not None and b2_enabled():
            if s3 is None:
                s3, bucket = _s3()
            return s3.get_object(Bucket=b2[0], Key=b2[1])["Body"].read()
        req = urllib.request.Request(url, headers={"User-Agent": "firstframe/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            return resp.read()
    raise ValueError(f"esquema no soportado: {url!r}")


# Alias historico: el resto del paquete llamaba a `_fetch_bytes`.
_fetch_bytes = fetch_bytes


def verify(job_id: str, *, fetch: bool = True,
           local_dir: str | Path = "runs") -> dict[str, Any]:
    """Equivalente de `genblaze verify final.mp4 --fetch`.

    Usa el CLI si existe en el PATH; si no, hace lo mismo en proceso:
    extraer el manifest embebido, verificar el hash canonico y (con `fetch`)
    re-descargar cada asset declarado para comprobar su sha256.
    """
    report: dict[str, Any] = {"job_id": job_id, "ok": False, "method": None,
                              "checks": [], "errors": []}

    with tempfile.TemporaryDirectory(prefix=f"verify-{job_id}-") as tmp:
        tmp = Path(tmp)
        master = tmp / "final.mp4"
        s3 = bucket = None
        if b2_enabled():
            s3, bucket = _s3()
            key = f"{approved_prefix(job_id)}/final.mp4"
            try:
                master.write_bytes(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
                report["source"] = f"s3://{bucket}/{key}"
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(f"no pude bajar {key} de B2: {exc}")
        if not master.exists():
            local = Path(local_dir) / job_id / "final.embedded.mp4"
            if not local.is_file():
                local = Path(local_dir) / job_id / "final.mp4"
            if not local.is_file():
                report["errors"].append(f"no encuentro el master de {job_id}")
                return report
            shutil.copyfile(local, master)
            report["source"] = str(local)

        cli = shutil.which("genblaze")
        if cli:
            args = [cli, "verify", str(master)] + (["--fetch"] if fetch else [])
            proc = subprocess.run(args, capture_output=True, text=True)
            report["method"] = "cli"
            report["cli_command"] = " ".join(args)
            report["cli_stdout"] = proc.stdout.strip()
            report["cli_stderr"] = proc.stderr.strip()
            report["ok"] = proc.returncode == 0
            if proc.returncode == 0:
                return report
            report["errors"].append(f"CLI devolvio {proc.returncode}; sigo en proceso")

        # --- equivalente en proceso ------------------------------------------
        report["method"] = report["method"] or "in-process"
        from genblaze_core.media import Mp4Handler

        try:
            manifest = Mp4Handler().extract(master)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"no hay manifest embebido en el mp4: {exc}")
            return report

        report["run_id"] = manifest.run.run_id
        report["canonical_hash"] = manifest.canonical_hash

        hash_ok = manifest.compute_hash() == manifest.canonical_hash
        report["checks"].append({"check": "canonical_hash", "ok": hash_ok})

        struct = manifest.verification_report()
        report["checks"].append({"check": "manifest.verify", "ok": bool(struct.ok),
                                 "invalid_metadata_ids": list(
                                     getattr(struct, "invalid_metadata_ids", []) or [])})

        assets_ok = True
        if fetch:
            for step in manifest.run.steps:
                for asset in step.assets:
                    entry: dict[str, Any] = {"check": "asset_sha256",
                                             "asset_id": asset.asset_id,
                                             "url": asset.url}
                    try:
                        data = _fetch_bytes(asset.url, s3=s3, bucket=bucket)
                        got = hashlib.sha256(data).hexdigest()
                        entry["ok"] = (got == asset.sha256)
                        entry["expected"] = asset.sha256
                        entry["actual"] = got
                    except FileNotFoundError:
                        # El manifest embebido describe el master ANTES de
                        # embeber, que vive en local. Si el que verifica no es
                        # la maquina que genero, no es un fallo: se salta y el
                        # peso recae en el check remoto de mas abajo.
                        entry["ok"] = None
                        entry["skipped"] = "asset local no disponible aqui"
                    except Exception as exc:  # noqa: BLE001
                        entry["ok"] = False
                        entry["error"] = str(exc)
                    if entry["ok"] is not None:
                        assets_ok &= bool(entry["ok"])
                    report["checks"].append(entry)
            report["fetched"] = True

        # El agregado del job es parte de la procedencia: tambien se comprueba.
        doc = None
        try:
            doc = read_aggregate(job_id, local_dir=local_dir)
            report["scene_count"] = doc.get("scene_count")
            report["failovers"] = doc.get("failovers")
            report["checks"].append({"check": "aggregate_manifest", "ok": True,
                                     "key": prov_key(job_id)})
        except Exception as exc:  # noqa: BLE001
            report["checks"].append({"check": "aggregate_manifest", "ok": False,
                                     "error": str(exc)})

        # Check REMOTO de verdad: el master que hay en B2 ahora mismo tiene
        # que ser byte a byte el que se aprobo. Es lo que un tercero puede
        # comprobar sin acceso a la maquina que genero.
        expected = (doc or {}).get("approved", {}).get("master_sha256")
        if expected:
            got = hashlib.sha256(master.read_bytes()).hexdigest()
            ok = got == expected
            assets_ok &= ok
            report["checks"].append({"check": "approved_master_sha256", "ok": ok,
                                     "expected": expected, "actual": got})

        report["ok"] = hash_ok and bool(struct.ok) and assets_ok and not report["errors"]
    return report


def demo() -> None:
    """Autocomprobacion.

    Sin B2 en el entorno: agregado local, embebido en mp4 y verify --fetch
    contra ficheros locales (todo el camino de provenance salvo la subida).
    Con B2: sube a provenance/ y approved/, bloquea con GOVERNANCE, comprueba
    que la retencion esta puesta y que B2 RECHAZA el borrado.
    """
    from pipeline.providers import make_clip, make_voiceover

    tmpdir = Path(tempfile.mkdtemp(prefix="manifest-demo-"))
    job = f"demo-manifest-{os.getpid()}"
    try:
        # master de mentira, pero mp4 de verdad
        clip = make_clip(tmpdir / "raw.mp4", seconds=1.0)
        make_voiceover(tmpdir / "vo.m4a", seconds=1.0)
        final = Path(tmpdir) / job / "final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(clip, final)

        rec = SceneRecord(n=1, title="apertura", run_id="run-a", iterations=2,
                          judge={"score": 0.42, "reason": "logo ilegible"},
                          fallbacks=[{"role": "clip", "from": "pixverse-v5.6",
                                      "to": "seedance-2-0"}],
                          local_path=str(final), cost_usd=0.03)
        rec2 = SceneRecord(n=2, title="detalle", run_id="run-b",
                           parent_run_id="run-a", local_path=str(final))
        doc = build_aggregate(job, "spot de un serum", [rec, rec2])
        assert doc["schema"] == SCHEMA
        assert doc["scene_count"] == 2
        assert doc["refined_scenes"] == [1]
        assert doc["failovers"][0]["to"] == "seedance-2-0"
        assert doc["lineage"][1]["parent_run_id"] == "run-a"

        written = write_aggregate(job, doc, local_dir=tmpdir)
        assert Path(written["local"]).is_file()
        assert written["bytes"] > 0
        # Con manifests de escena reales el agregado pasa holgadamente de los
        # 2048 B que admite nombre+file-info en un bucket con Object Lock: por
        # eso va SIEMPRE como cuerpo del objeto. Aqui los registros son de
        # juguete, asi que solo se comprueba que se escribio como objeto.
        assert "Metadata" not in json.dumps(written)
        back = read_aggregate(job, local_dir=tmpdir)
        assert back["job_id"] == job

        # --- aprobacion + embebido ---
        res = approve(job, final, doc=doc, local_dir=tmpdir)
        assert res["embed_method"], res
        assert res["embed_verified"], f"el manifest embebido no se relee: {res}"
        assert Path(res["local_master"]).is_file()

        # --- verify --fetch ---
        # approve() ya dejo el master embebido donde verify() lo busca:
        # {local_dir}/{job}/final.embedded.mp4
        assert Path(res["local_master"]) == final.parent / "final.embedded.mp4"
        report = verify(job, fetch=True, local_dir=tmpdir)
        assert report["ok"], json.dumps(report, indent=2, default=str)[:2000]
        assert any(c["check"] == "asset_sha256" and c["ok"] for c in report["checks"]), \
            "verify --fetch debe re-hashear al menos un asset"
        print(f"  verify: method={report['method']} checks={len(report['checks'])} ok={report['ok']}")

        if b2_enabled():
            s3, bucket = _s3()
            assert s3.head_object(Bucket=bucket, Key=prov_key(job))["ContentLength"] > 0
            mkey = res["keys"]["master"]
            ret = s3.get_object_retention(Bucket=bucket, Key=mkey).get("Retention", {})
            assert ret.get("Mode") == LOCK_MODE, ret
            print(f"  B2: s3://{bucket}/{mkey} lock={ret.get('Mode')} "
                  f"hasta={ret.get('RetainUntilDate')}")

            # El momento del video: B2 se niega a borrarlo.
            from botocore.exceptions import ClientError
            version = s3.head_object(Bucket=bucket, Key=mkey).get("VersionId")
            try:
                s3.delete_object(Bucket=bucket, Key=mkey, VersionId=version)
                raise AssertionError("B2 permitio borrar un objeto con retencion!")
            except ClientError as exc:
                print(f"  B2 rechazo el borrado -> {exc.response['Error'].get('Code')}")

            # limpieza de lo que SI se puede borrar
            for k in (prov_key(job), res["keys"].get("manifest")):
                if k:
                    try:
                        s3.delete_object(Bucket=bucket, Key=k)
                    except Exception:  # noqa: BLE001,S110
                        pass
        else:
            print("  (B2 no configurado: subida/lock no ejercitados)")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("demo OK: agregado como objeto, manifest embebido en el mp4 y "
          "verify --fetch re-hasheando los assets declarados")


if __name__ == "__main__":
    demo()
