# Dossier técnico — Genblaze SDK (Backblaze) + Backblaze B2

> Investigación para el Backblaze Generative Media Hackathon (deadline 2026-08-03 23:00 GMT+2).
> Fecha de investigación: 2026-08-02.

## 0. Identificación del repo y estado de la release

| Dato | Valor |
|---|---|
| **Repo oficial** | `https://github.com/backblaze-labs/genblaze` (542 stars, org `backblaze-labs`, NO `b2genblaze`) |
| HEAD analizado | `af84f8bc394752d0bfdf2550afcce455a834803e` — 2026-08-01 `docs(release): clarify wave tag vs. PyPI version confusion (#255)` |
| Licencia | MIT |
| Python | >= 3.11 |
| **Versión PyPI del paraguas `genblaze`** | **0.4.5** (histórico: 0.2.3, 0.3.0, 0.3.1, 0.3.2, 0.4.0, 0.4.1, 0.4.3, 0.4.4, 0.4.5) |
| Sub-paquetes que arrastra | `genblaze-core==0.3.8`, `genblaze-s3==0.3.6` (verificado con `pip install genblaze` en venv limpio) |
| Último tag de GitHub | `v0.7.0` (2026-07-28) — **es un "wave tag", NO una versión de PyPI** |

`github.com/b2genblaze` es la org de *review* del hackathon (recibe los forks/submissions); el SDK vive en `backblaze-labs`.

**Instalación correcta** (verificada):

```bash
pip install "genblaze[all]"                  # todo
pip install "genblaze[gmicloud,openai]"      # scoped
pip install genblaze-core genblaze-s3        # paquetes sueltos
```

Nombres: `pip install genblaze-<name>` → `import genblaze_<name>` (guion vs underscore).

Estructura del monorepo:

```
libs/spec/          # JSON Schemas del manifest + tipos TS (@genblaze/spec en npm)
libs/core/          # genblaze-core  → Pipeline, Step, Run, Manifest, sinks, tracers, agents
libs/connectors/    # 15 adaptadores (openai, google, gmicloud, nvidia, runway, luma, s3, ...)
libs/meta/          # el paraguas `genblaze` (solo re-exporta + extras)
cli/                # genblaze-cli (extract / verify / replay / index)
examples/           # 30 ejemplos
docs/features/      # 26 docs de features — la mejor fuente de verdad
docs/reference/     # model-matrix.md (autogenerado), pricing-recipes.md
docs/exec-plans/feedback.md  # ORO: feedback P0/P1 de sample-apps reales
```

---

## 1. Primitivas expuestas

Todo se exporta perezosamente desde `genblaze_core/__init__.py` (`_LAZY_IMPORTS`, ~130 símbolos).
El paraguas `genblaze` re-exporta lo mismo.

### 1.1 Orquestación (núcleo)

`libs/core/genblaze_core/pipeline/pipeline.py` (2980 líneas — la clase central)

```python
Pipeline(
    name=None, tenant_id=None, *, project_id=None,
    chain=False,              # output del step N → input del step N+1
    structured_log=False,
    max_concurrency=None,
    moderation=None,          # ModerationHook
    tracer=None,              # Tracer
    preflight=True,           # valida modelos antes de gastar dinero
)
```

Métodos fluidos: `.step(...)`, `.config(cfg)`, `.tracer(t)`, `.cache(StepCache)`, `.preflight(bool)`,
`.from_result(result)` (linaje `parent_run_id`), `.metadata(**kw)`, `.to_template()`.

Runners:

| Método | Qué hace |
|---|---|
| `run(...)` / `arun(...)` | Síncrono / async |
| `stream(heartbeats=True, **kw)` / `astream(...)` | Generador de `StreamEvent` en vivo |
| `batch_run(...)` / `abatch_run(...)` | Barrido de parámetros/prompts |
| `invoke(input, config)` / `ainvoke(...)` | Interfaz `Runnable` (estilo LangChain) |
| `resume_step(...)` / `aresume_step(...)` | Reanudar un step concreto |
| `Pipeline.ingest(assets, source=..., sink=...)` | **classmethod**: manifest de ingest sin generación |
| `estimated_cost()` | Suma `Decimal` del coste estimado antes de ejecutar |

Firma exacta de `run()` (verificada por introspección sobre 0.3.8):

```python
run(*, sink=None, fail_fast=True, raise_on_failure=None, timeout=None,
    max_retries=None, on_progress=None, progress=None, pipeline_timeout=None,
    on_step_complete=None, on_retry=None) -> PipelineResult
```

Firma exacta de `step()` — `pipeline.py:643`:

```python
step(provider: BaseProvider, *, model: str, prompt=None,
     modality=Modality.IMAGE, step_type=StepType.GENERATE,
     fallback_models: list[str] | None = None,   # ← failover entre modelos
     input_from: list[int] | int | None = None,  # ← fan-in / DAG
     external_inputs: list[Asset] | None = None, # ← inyectar assets del caller
     expected_duration_sec=None, metadata=None,
     prompt_visibility=PromptVisibility.PUBLIC,
     params=None, **extra_params) -> Pipeline
```

Tres mecanismos de input distintos: `chain=True` (secuencial implícito), `input_from=[0,1]` (DAG
explícito, permite fan-in) y `external_inputs=[Asset(...)]` (assets que trae el caller).

### 1.2 Modelo de datos / provenance

- `Run` — `run_id`, `tenant_id`, `project_id`, `parent_run_id`, `metadata`, `steps[]`, `status`.
- `Step` — provider, model, prompt, params, seed, `cost_usd`, `retries`, `assets[]`, `inputs[]`, `metadata`, `error_code`.
- `Asset` — `url`, `media_type`, `sha256`, `size_bytes`, `width/height`, `+ VideoMetadata / AudioMetadata / Track / WordTiming`.
- `Manifest` — `Manifest.from_run(run)`, `.canonical_hash`, `.verify()`, `.to_canonical_json()`, `.manifest_uri`, `parse_manifest()`. Schema actual: **`schema_version: "1.5"`**.
- `EmbedPolicy` — `prompt_visibility`, `embed_mode: "full"|"pointer"|"none"`, `include_params`, `include_seed` (`models/policy.py`).
- `PromptTemplate`, `Voice`, `ChatMessage/ChatResponse/ToolCall` + contenidos multimodales (`ImageURLContent`, `VideoURLContent`, `AudioURLContent`).

Enums: `Modality`, `StepType` (`GENERATE, UPSCALE, TRANSCODE, MIX, EDIT, CUSTOM, INGEST, IMPORT`), `RunStatus`, `StepStatus`, `PromptVisibility`, `ProviderErrorCode`.

### 1.3 Agentes / evaluación (`libs/core/genblaze_core/agents/`)

`loop.py` — `AgentLoop(pipeline_factory, evaluator, *, max_iterations=3, tracer=None, stop_on_pipeline_failure=True)`.
Ciclo: construir Pipeline vía factory(ctx) → ejecutar → evaluar → si falla, iterar con feedback.
Cada iteración se enlaza con `Pipeline.from_result(prev)` ⇒ **cadena de `parent_run_id` en los manifests**.
API: `.run()`, `.arun()`, `.stream()`, `.astream()`. Devuelve `AgentResult(iterations, final, passed, total_cost_usd)`.
Eventos: `agent.iteration.started`, `agent.iteration.evaluated`, `agent.completed`.

`evaluator.py` — `EvaluationResult(passed, score, feedback, metadata)` y tres evaluadores:
`Evaluator` (ABC, con `aevaluate` por defecto en thread), `CallableEvaluator(fn)`, `ThresholdEvaluator(score_fn, threshold, higher_is_better=True, feedback_fn=None)`.

### 1.4 Retries / failover

`libs/core/genblaze_core/providers/retry.py` — `RetryPolicy` (frozen dataclass) pasada a `Provider(retry_policy=...)`:

```
max_attempts=6, initial_backoff_sec=1.0, max_backoff_sec=30.0, backoff_multiplier=2.0,
jitter="full"|"equal"|"none", respect_retry_after=True (cap MAX_RETRY_AFTER_SEC=120s),
retryable_codes={TIMEOUT, RATE_LIMIT, SERVER_ERROR},
idempotency_key_strategy="step_id"|"uuid_per_attempt"|"none"
```

Presets: `RetryPolicy.conservative()` (2 intentos — para vídeo caro), `.aggressive()` (7), `.disabled()`.
No reintenta por defecto: `CONTENT_POLICY`, `AUTH_FAILURE`, `INVALID_INPUT`, `MODEL_ERROR` (deterministas).

**Failover de modelos**: `step(fallback_models=[...])` → `_try_fallback_models()` en `pipeline.py:1248`.
Solo dispara con `ProviderErrorCode.MODEL_ERROR`. Escribe `metadata["fallback_from"]` y `["fallback_model"]`.
*Verificado en ejecución*: el fallback se intenta y queda registrado en `step.metadata`.
⚠️ No hay failover automático **entre providers distintos** — `fallback_models` son slugs del mismo provider;
para cambiar de proveedor hay que orquestarlo a mano (try/except o AgentLoop).

### 1.5 Storage / sinks

- `BaseSink`, `ObjectStorageSink`, `ParquetSink`, `WebhookSink`.
- `StorageBackend` (ABC), `AssetTransfer`, `KeyStrategy`, `KeyBuilder`, `ObjectLockConfig`, `StorageConfig`, `URLPolicy`, `ObjectMetadata`, `FileEntry`, `ListPage`, `DeleteResult`, `TransferProgress`, `StorageErrorCode`, `classify_botocore_error`.

### 1.6 Media embedding (`libs/core/genblaze_core/media/`)

`Mp4Handler`, `PngHandler`, `JpegHandler`, `WebpHandler`, `Mp3` / `Wav` / `Aac` / `Flac`, `SidecarHandler`,
`SmartEmbedder` (auto-detecta el handler por MIME), `sniff_mime()`, `guess_mime()`.
Uso: `handler.embed(path, manifest)` / `handler.extract(path)` → `manifest.verify()`.

### 1.7 Observabilidad

`Tracer` (ABC), `NoOpTracer`, `LoggingTracer`, `OTelTracer`, `CompositeTracer`, `StructuredLogger`, `StreamEvent`, `ProgressEvent`, `Spinner`.
Conector aparte: `genblaze-langsmith` → `LangSmithTracer`.

### 1.8 Utilidades locales sin coste

- `MockProvider`, `MockVideoProvider`, `MockAudioProvider` — en `genblaze_core.mocks` (sin pytest).
- `FFmpegCompositor` — muxea vídeo+audio a MP4 (`SyncProvider`, `accepts_chain_input=True`).
- `FFmpegTransform` — `resize`, `crop`, `overlay_text`, `audio_normalize`, `convert_format` (vía `step.params["operation"]`).
- `StepCache(cache_dir)` — caché de steps en disco (dedupe por contenido).
- `PipelineTemplate` / `StepTemplate` — serializar un pipeline a JSON y reinstanciarlo.
- `ModerationHook` / `ModerationResult` — screening pre-prompt y post-output.
- `ProviderComplianceTests` — harness de conformidad para providers propios (requiere pytest).

### 1.9 CLI (`genblaze-cli` 0.3.6, comando `genblaze`)

```bash
genblaze extract video.mp4 [-o m.json]   # extraer manifest embebido
genblaze verify video.mp4 [--fetch]      # verificar hash (+ re-hash de bytes reales)
genblaze verify manifest.json            # verificar manifest suelto
genblaze replay manifest.json            # dry-run de replay
genblaze index manifest.json -o data/    # → tablas Parquet particionadas
```

Exit code != 0 si falla la verificación. Cero dependencias de providers.

---

## 2. Proveedores y modelos

15 conectores. Cada uno lee su API key de una env var (o del constructor).

| Provider | Paquete pip | Provider IDs | Env var |
|---|---|---|---|
| **Backblaze B2 / S3** | `genblaze-s3` | (storage backend) | `B2_KEY_ID`, `B2_APP_KEY`, opc. `B2_BUCKET`, `B2_REGION` |
| GMICloud | `genblaze-gmicloud` | `gmicloud`, `gmicloud-image`, `gmicloud-audio` | `GMI_API_KEY` (+ `GMI_BASE_URL`) |
| NVIDIA NIM | `genblaze-nvidia` | `nvidia-video/-image/-audio/-chat` | `NVIDIA_API_KEY` (`nvapi-...`) |
| OpenAI | `genblaze-openai` | `openai-sora`, `openai-dalle`, `openai-tts` | `OPENAI_API_KEY` |
| Google | `genblaze-google` | `google-veo`, `google-imagen`, `google-gemini-image` | `GEMINI_API_KEY` |
| Runway | `genblaze-runway` | `runway` | `RUNWAYML_API_SECRET` |
| Luma | `genblaze-luma` | `luma` | `LUMAAI_API_KEY` |
| Decart | `genblaze-decart` | `decart`, `decart-image` | `DECART_API_KEY` |
| Replicate | `genblaze-replicate` | `replicate` | `REPLICATE_API_TOKEN` |
| ElevenLabs | `genblaze-elevenlabs` | `elevenlabs-tts`, `elevenlabs-sfx` | `ELEVENLABS_API_KEY` |
| Stability Audio | `genblaze-stability-audio` | `stability-audio` | `STABILITY_API_KEY` |
| LMNT | `genblaze-lmnt` | `lmnt` | `LMNT_API_KEY` |
| Hume | `genblaze-hume` | `hume-tts` | `HUME_API_KEY` |
| AssemblyAI | `genblaze-assemblyai` | `assemblyai` | `ASSEMBLYAI_API_KEY` |
| LangSmith | `genblaze-langsmith` | (Tracer, no provider) | `LANGSMITH_API_KEY` |

### 2.1 Modelos por provider (`docs/reference/model-matrix.md`, autogenerado por `tools/gen_model_matrix.py`)

**GMICloud vídeo (23)** — `seedance-2-0-260128` ($0.052/s), `seedance-1-0-pro-250528`, `seedance-1-0-pro-fast`,
`kling-{text2video,image2video}-v{1.5,1.6}-pro` ($0.098), `kling-*-v2.1-master` ($0.28), `luma-ray-2` ($0.20),
`pixverse-v5.6-{t2v,i2v,transition}` ($0.03), `wan2.6-{t2v,i2v,r2v}`, `wan2.7-{t2v,i2v}`, `sora-2-pro`, `veo3`.
⚠️ Marcados `suspected_dead`: `kling-text2video-v2.1-master`, `minimax-hailuo-2.3-fast`, `veo3-fast`, `vidu-q1`.

**GMICloud imagen (14)** — `seedream-5.0-lite`, `flux-kontext-pro`, `gemini-2.5-flash-image`,
`reve-{create,edit,edit-fast,remix,remix-fast}-*`, `seededit-3-0-i2i-250628`, `bria-{eraser,genfill,fibo-*}` ($0.007–0.05).

**GMICloud audio (5)** — `ElevenLabs-TTS-v3`, `MiniMax-Music-2.5`, `MiniMax-TTS-Speech-2.6-Turbo`,
`MiniMax-Voice-Clone-Speech-2.6-HD`, `Inworld-TTS-1.5-Mini`. ⚠️ **TODOS marcados `suspected_dead` Y rotos por el issue #251.**

**NVIDIA NIM** — vídeo: `nvidia/cosmos-{1.0-7b,2.0}-diffusion-{text2world,video2world}`;
imagen: `black-forest-labs/flux.1-{dev,schnell}`, `stabilityai/stable-diffusion-{xl,3-5-medium,3-5-large,3-5-large-turbo}`;
audio: `nvidia/fugatto`, `nvidia/riva-tts`, `nvidia/maxine-voice-font`; chat: descubrimiento nativo (Nemotron, Llama, Mistral, Qwen, Phi).

**OpenAI** — `sora-2`, `sora-2-pro`; `dall-e-2` ($0.02), `dall-e-3` ($0.04), `gpt-image-{1,1-mini,1.5,2}`; TTS `tts-1`, `tts-1-hd`, `gpt-4o-mini-tts`.
**Google** — `veo-2.0-generate-001` ($1.40), `veo-3.0-fast-generate-001` ($1.00), `veo-3.0-generate-001` ($2.00); `imagen-3.0-generate-002` ($0.04), `imagen-3.0-fast-generate-001` ($0.02).
**Runway** — `gen3a_turbo`, `gen4_turbo` (README menciona además `gen4.5`, `veo3`, `veo3.1`, `veo3.1_fast`).
**Luma** — `ray-2`, `ray-flash-2`. **Decart** — 8 `lucy-*` vídeo ($0.04), 2 `lucy-pro-{t2i,i2i}` ($0.02).
**ElevenLabs** — TTS `eleven_v3`, `eleven_multilingual_v2`, `eleven_turbo_v2_5`, `eleven_flash_v2_5`; SFX `eleven_text_to_sound_v2` ($0.10/s).
**Stability** — `stable-audio-2.5` ($0.01/s). **Replicate** — catálogo abierto (`owner/model`). **AssemblyAI** — `universal-3-pro`, `universal-2`.

### 2.2 Coste: qué es barato/gratis

- **NVIDIA NIM = la opción más barata y sin tarjeta.** `libs/connectors/nvidia/README.md:25`:
  *"The free tier is rate-limited (~40 requests/minute per model) with no per-token billing.
  Some models (Cosmos video) are still enterprise-gated as of 2026-04 and will return `AUTH_FAILURE` for free-tier keys."*
  Una sola key (`nvapi-...` de build.nvidia.com) da **vídeo + imagen + audio + chat**. Ideal para multi-modalidad barata.
- **Coste cero absoluto**: `MockProvider` / `MockVideoProvider` / `MockAudioProvider` + `FFmpegCompositor` + `FFmpegTransform` (locales).
- **Barato de verdad si hay que pagar**: GMICloud imagen (`bria-*` $0.007, `seedream-5.0-lite`), Decart imagen ($0.02),
  `dall-e-2` ($0.02), `imagen-3.0-fast` ($0.02), Stability Audio ($0.01/s), GMICloud `pixverse-v5.6` ($0.03/asset — el vídeo más barato).
- **Caro, evitar en demo**: Google Veo 3 ($1.00–2.00 por generación), Kling v2.1-master ($0.28), Sora.
- **El SDK no trae precios hardcodeados desde 0.3.0** — `cost_usd` sale `None` salvo que registres pricing tú
  (`reg.register_pricing("dall-e-3", per_unit(0.040))`, recetas en `docs/reference/pricing-recipes.md`).

### 2.3 Créditos del hackathon

**No hay ninguna referencia a créditos de hackathon en el repo.** Grep exhaustivo de `hackathon`, `credit`,
`free tier`, `trial`, `sandbox` sobre README, CHANGELOG, docs/ y todos los READMEs de conectores: los únicos hits
son el rate-limit gratuito de NVIDIA, el endpoint `/dream-machine/v1/credits` de Luma, y "sandbox" referido a
allowlists de `file://`. Si hay créditos de GMI Cloud, vienen de la página del evento, no del SDK.

⚠️ Aviso del propio repo (`docs/exec-plans/active/storage-ergonomics-and-gmi-catalog-tranche.md:197`): sondear la cola
de GMICloud **puede facturar generaciones reales** ("the risk of a permissive upstream queue accepting a minimal
probe payload and billing for a real generation job").

---

## 3. Conexión con Backblaze B2

### 3.1 ¿Nativo o "solo S3"?

**Ambos**: hay una única clase `S3StorageBackend` (`libs/connectors/s3/genblaze_s3/backend.py:210`) que sirve para
AWS S3, B2, R2 y MinIO, **pero con un helper de primera clase para B2** y hardening específico de B2 dentro.

```python
# genérico (backend.py:231)
S3StorageBackend(bucket, *, endpoint_url=None, region=None, public_url_base=None,
                 aws_access_key_id=None, aws_secret_access_key=None,
                 access_key_id=None, secret_access_key=None)

# B2 (backend.py:1663) — firma verificada por introspección sobre genblaze-s3 0.3.6
S3StorageBackend.for_backblaze(bucket=None, *, region=None, key_id=None, app_key=None,
                               public_url_base=None, auto_lifecycle=False, preflight=True)
```

`for_backblaze` construye `endpoint_url = https://s3.{region}.backblazeb2.com`, region por defecto `us-west-004`,
y con `preflight=True` (default) lanza un `HeadBucket` inmediato → **las credenciales malas fallan al construir, no a mitad del run**.

Hardening B2 dentro del backend: auto-detección/redirección de región (prueba las 4 regiones publicadas ante un 403),
detección de host `_is_b2`, y desactivación del checksum CRC32-trailer de boto3 (`request_checksum_calculation="when_required"`)
porque rompía los uploads a B2.

Env vars (leídas **solo** dentro de `for_backblaze`, `backend.py:1741-1749`): `B2_BUCKET`, `B2_REGION`, `B2_KEY_ID`, `B2_APP_KEY`.
El constructor genérico usa la cadena de credenciales normal de boto3.

Dependencias: `genblaze-s3` → `boto3>=1.28,<2`, `botocore>=1.31,<2`. Extra async: `genblaze-s3[async]` → `aioboto3` (`AsyncS3StorageBackend`).

### 3.2 ¿Escribe en B2 nativamente? — SÍ

`ObjectStorageSink` se pasa como `sink=` a `Pipeline.run()` y hace **todo el ciclo automáticamente**
(`libs/core/genblaze_core/storage/sink.py:230`, `write_run()` en `:438`):

1. Descarga los bytes del asset desde el CDN del provider (`AssetTransfer`, streaming + SHA-256 al vuelo).
2. Sube al backend con la `KeyStrategy` configurada.
3. Recalcula el hash canónico del manifest tras la transferencia.
4. **Sube el manifest JSON** junto a los assets.
5. **Reescribe `asset.url`** a la URL durable (sin credenciales, no expira).

```python
ObjectStorageSink(backend, *, prefix="genblaze",
                  key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
                  parquet_sink=None, max_upload_workers=4,
                  manifest_lock: ObjectLockConfig | None = None,
                  pipelined_transfer=False, eager_transfer=False,
                  asset_url_policy=URLPolicy.AUTO, ...)
```

Para subidas sueltas fuera de un run: `sink.put_asset(asset)` / `sink.put_assets([...])` (`sink.py:731`).

### 3.3 Layouts de claves

```
CONTENT_ADDRESSABLE (default, dedup)        HIERARCHICAL (agrupado por run)
{prefix}/assets/{sha[:2]}/{sha[2:4]}/{sha}.ext   {prefix}/runs/{tenant}/{date}/{run_id}/manifest.json
{prefix}/manifests/{run_id}.json                 {prefix}/runs/.../assets/{asset_id}.ext
```

Índice inverso asset→manifest: `{prefix}/_index/{tenant_id}/{asset_id}.json`.

### 3.4 Presigned URLs

En `S3StorageBackend`:
- `presigned_get(key, *, expires_in=3600) -> PresignedURL` (`backend.py:1182`) — el `__repr__` **redacta** `X-Amz-Signature`.
- `presigned_put(key, *, expires_in=3600, content_type=None) -> PresignedURL` (`:1220`).
- `presigned_get_url()` / `presigned_put_url()` → `str` crudo.
- `get_url(key, *, expires_in=3600, policy=URLPolicy.AUTO)` — pública si hay `public_url_base`, si no presigned.
- `get_durable_url(key)` — no expira, sin credenciales; **es lo que se persiste en el manifest** (decisión deliberada de 0.3.0 para no filtrar firmas en datos persistidos).
- `presigned_post` **no está implementado** (deferido a propósito).

### 3.5 Object Lock / inmutabilidad

Implementado por completo. `libs/core/genblaze_core/storage/base.py:29`:

```python
@dataclass(frozen=True)
class ObjectLockConfig:
    retain_until: datetime            # debe ser tz-aware
    mode: ObjectLockMode = "GOVERNANCE"   # o "COMPLIANCE"
```

Dos entradas: por sink (`ObjectStorageSink(..., manifest_lock=ObjectLockConfig(...))` — aplica a **todos los manifests**)
y por objeto (`backend.put(key, data, object_lock=...)`, `backend.py:371`).
Con `mode="COMPLIANCE"` loguea un warning ruidoso (nadie, ni root, puede borrar hasta que expire).

⚠️ **El bucket de B2 debe crearse con Object Lock habilitado — no se puede activar después.**

### 3.6 Metadata en los objetos

**Verificado en ejecución**: el camino del pipeline **NO adjunta S3 user-metadata ni tags**. Un `head_object` tras
un run real devuelve `Metadata: {}`. Lo que sí se pone:

- `ContentType` (sniffeado de la respuesta HTTP o `mimetypes`).
- `CacheControl`: `public, max-age=31536000, immutable` para CAS/manifests; `private, max-age=3600` para HIERARCHICAL.
- `ChecksumAlgorithm="SHA256"` por defecto en cada `put`.

**La provenance vive en el manifest JSON**, no en la metadata del objeto. Tus metadatos de negocio van por dos vías,
ambas persistidas en el manifest (verificado):
- `Pipeline.metadata(**kw)` → `manifest.run.metadata`
- `Pipeline.step(..., metadata={...})` → `manifest.run.steps[i].metadata`

Si quieres metadata S3 nativa tienes que llamar a `backend.put(key, data, metadata={...})` a mano.

### 3.7 Otros detalles operativos

- Multipart automático a partir de **16 MB**, chunks de 16 MB, 4 workers en paralelo.
- Timeouts: `connect_timeout=30s`, `read_timeout=300s` (el default de 60s de boto3 se queda corto para vídeo), pool de 20 conexiones.
- `backend.copy()` usa `CopyObject` server-side (límite 5 GB).
- `auto_lifecycle=True` en `for_backblaze` aplica reglas de lifecycle: aborta multipart huérfanos a los 7 días y expira versiones no-actuales de manifests a los 30 días (los buckets de B2 siempre tienen versionado → control de coste).
- `delete_many` / `delete_prefix` aceptan `dry_run`.

---

## 4. Los 30 ejemplos del repo

Ruta: `examples/`. Ninguno tiene imports rotos (verificado contra `libs/`), pero ver el gotcha #4 de la sección 5.

### 4.1 Sin API key — ejecutables ya (el "camino feliz")

| Fichero | LOC | Qué demuestra | Estado |
|---|---|---|---|
| `quickstart_local.py` | 62 | `RunBuilder`/`StepBuilder` → `Manifest.from_run()` → `.verify()`. Cero red. | ✅ **ejecutado OK** |
| `agent_loop_local.py` | 89 | `AgentLoop` + `CallableEvaluator`: generate→evaluate→refine 3 iteraciones, streaming de eventos, **linaje `parent_run_id` encadenado**. | ✅ **ejecutado OK** |
| `streaming_local.py` | 43 | `.stream()` y `.astream()` sobre eventos de pipeline. | ✅ |
| `error_handling.py` | 49 | `fail_fast=False`, `error_summary()`, manifest parcial con los steps que sí salieron. | ✅ |
| `custom_model_registry.py` | 209 | `register_pricing`, `register_family`, `ModelSpec`, filtrado por allowlist de params. Sin red (solo lee `DalleProvider.models_default()`). | ✅ |

### 4.2 Storage / B2

| Fichero | Necesita | Demuestra |
|---|---|---|
| `quickstart.py` | `GMI_API_KEY` + B2 | El quickstart del README: GMICloud vídeo → B2 → provenance |
| `b2_storage_pipeline.py` | `REPLICATE_API_TOKEN` + B2 | `for_backblaze(...)` + `KeyStrategy.CONTENT_ADDRESSABLE` + `auto_lifecycle=True` |
| `s3_storage_pipeline.py` | Replicate + AWS | `KeyStrategy.HIERARCHICAL` + presigned URLs |
| `ingest_podcast_episode.py` | B2 | `Pipeline.ingest()` desde RSS, dedup CAS, lookup inverso asset→manifest |
| `ingest_ugc_upload.py` | B2 | `Pipeline.ingest()` de subidas de usuario con atribución del uploader |

### 4.3 Multi-step / multi-provider (los que puntúan)

| Fichero | Steps | Providers |
|---|---|---|
| **`fan_in_av_composite.py`** | **3** | **Sora (vídeo) + ElevenLabs SFX → `FFmpegCompositor` vía `input_from=[0,1]`** ← el mejor ejemplo de orquestación real |
| `chain_image_to_video.py` | 2 | DALL-E → Sora con `chain=True` (imagen→vídeo) |
| `batch_with_templates.py` | N | `PromptTemplate` + `batch_run()` sobre dicts (⚠️ roto, ver gotcha #4) |

### 4.4 Un solo provider (plantillas de copiar/pegar)

`dalle_image_pipeline.py`, `sora_video_pipeline.py`, `tts_audio_pipeline.py` (OpenAI) ·
`imagen_pipeline.py`, `veo_video_pipeline.py` (Google) · `gmicloud_{video,image,audio}_pipeline.py` ·
`replicate_flux_pipeline.py` · `runway_video_pipeline.py` · `luma_video_pipeline.py` · `decart_video_pipeline.py` ·
`elevenlabs_{tts,sfx}_pipeline.py` · `lmnt_tts_pipeline.py` · `stability_audio_pipeline.py` ·
`transcribe.py` (AssemblyAI — es un smoke test en vivo que **factura**).

Nota: `ingest_podcast_episode.py` menciona en comentarios un `WhisperProvider` encadenado que **no existe todavía** (Wave 6) — es aspiracional.

---

## 5. Gotchas — lo que puede hundir la demo

Fuentes: issues de GitHub, `docs/exec-plans/feedback.md` (feedback P0/P1 de sample-apps reales), y **verificación propia en un venv limpio con la versión publicada en PyPI (`genblaze` 0.4.5 / `genblaze-core` 0.3.8 / `genblaze-s3` 0.3.6)**.

### 5.1 Verificados por mí ejecutando código (100 % reproducibles hoy)

**G1. `PromptTemplate("literal")` explota — y el ejemplo oficial usa esa forma.**
```
>>> PromptTemplate("a {subject} in {style}")
TypeError: BaseModel.__init__() takes 1 positional argument but 2 were given
```
`examples/batch_with_templates.py:19` usa exactamente la forma posicional ⇒ **ese ejemplo está roto tal cual se publica**.
*Fix*: usar siempre `PromptTemplate(template="...")`. (feedback P1-02)

**G2. `from genblaze_core.testing import MockProvider` falla en instalación limpia.**
```
ModuleNotFoundError: No module named 'pytest'
```
`testing.py` hace `import pytest` a nivel de módulo y pytest no es dependencia de runtime.
*Fix*: importar desde el paraguas → `from genblaze_core import MockProvider, MockVideoProvider, MockAudioProvider`
(viven en `genblaze_core/mocks.py`, sin pytest). **Verificado que esta vía sí funciona.** (feedback P1-01, parcialmente resuelto)

**G3. `Pipeline.run(cache=...)` lanza `TypeError`.**
```
TypeError: Pipeline.run() got an unexpected keyword argument 'cache'
```
La caché es fluida: `Pipeline(...).cache(StepCache("dir")).run(...)`. (feedback P1-03)

### 5.2 Issues abiertos que rompen demos

**G4. #251 — Todo el audio de GMI Cloud está inutilizable.** Los allowlists de params de TTS/música omiten el
parámetro `text`/`lyrics` que exige la API ⇒ **todas** las llamadas devuelven 400. Además los 5 modelos de
`gmicloud-audio` están marcados `suspected_dead` en el model matrix.
*Mitigación*: usar `genblaze_openai.OpenAITTSProvider` (`tts-1`, `gpt-4o-mini-tts`) o ElevenLabs directo.

**G5. #248 — `validate_model()` está invertido.** Reporta `ok_authoritative` para modelos que dan 404 al hacer submit
real, y `unknown_permissive` para modelos que sí funcionan. El preflight da **falsa confianza** y el fallo aparece
a mitad del run, con el job ya encolado y facturado.
*Mitigación*: no fiarse del preflight. Hacer **una generación real de cada modelo del camino de demo durante el ensayo**.
(Al equipo del issue le costó casi un día.)

**G6. #253 — Las ediciones de imagen de OpenAI rechazan cualquier fuente `https://`.** El fichero temporal se guarda
con extensión `.img` → OpenAI lo ve como `application/octet-stream` y lo rechaza. Rompe cualquier flujo
"asset guardado en B2 (presigned) → editar con gpt-image".
*Mitigación*: descargar a fichero local con extensión correcta antes de llamar al provider.

**G7. #233 — El quickstart de `genblaze-google` usa slugs `imagen-3.0-*` deslistados.** Copiar/pegar el README falla directo.

**G8. #246 — Buckets privados de B2 en navegador necesitan presigns AWS4 path-style**, sin documentar. Momento clásico de
"con curl funciona, en el navegador no" en mitad de la demo.

Otros abiertos, menor impacto: #247 (`ObjectStorageSink` no admite `output_dir` del provider), #245 (`embedded_manifest_hash`
inconsistente), #239 (un fallback exitoso **borra del provenance el intento primario fallido**), #240 (pierde el usage de GPT Image),
#238 (`SmartEmbedder` en modo pointer puede devolver una ruta inexistente), #254 (no hay tabla de compatibilidad de versiones).

### 5.3 Packaging / versiones

**G9. Nunca pinchar `genblaze==<wave tag>`.** Los tags de GitHub (`v0.5.0`, `v0.6.0`, `v0.7.0`) **no existen en PyPI**;
PyPI va 0.4.1→0.4.3→0.4.4→**0.4.5**. Un pin tipo `genblaze==0.4.0` resuelve **en silencio** a código viejo de otra wave
(sin error). El tag `v0.7.0` mapea a `genblaze==0.4.5`. (issue #250, cerrado hoy mismo; README:48-62 tiene el aviso).
*Fix*: `pip install "genblaze[all]"` sin pin, o pin exacto `genblaze==0.4.5`, y `pip freeze` a un lockfile en cuanto funcione.

**G10. No hacer `pip install -e .` desde la raíz del repo.** El `pyproject.toml` raíz **no tiene bloque `[project]` a propósito**
(hubo un stub `name="genblaze" version="0.1.0"` sin deps que ensombrecía el paraguas real). Para dev del monorepo: `make install-dev`.

### 5.4 Trampas de diseño (feedback.md — sample-apps reales)

**G11. P0-04: no existe `Pipeline.input(asset_or_path)`.** El step 0 **tiene que ser un provider generador**. Para
arrancar un pipeline desde un fichero/URL existente hay que escribir un `SyncProvider` de usar y tirar. Lo sufrieron 8 de 10 sample-apps.
*Mitigación*: escribe un `PassthroughProvider(SyncProvider)` de 10 líneas al principio, no el día de la demo.
*Alternativa*: `Pipeline.ingest(assets=[...], source=..., sink=...)` **sí existe** (`pipeline.py:578`) y cubre el caso de
"documentar assets externos con manifest", aunque no encadena hacia steps generativos.

**G12. P0-05/P0-06: el SDK es "generation-shaped", no "analysis-shaped".** No hay `Step.output` ni `Asset.text`;
transcripciones/resúmenes/JSON se meten en `metadata["text"]` o en data-URIs falsas (comportamiento indefinido en el sink).
`StepType` sí tiene `INGEST`/`IMPORT` pero no `TRANSCRIBE`/`CLASSIFY`/`ANALYZE`.

**G13. `@dataclass` sobre una subclase de `SyncProvider` la rompe en silencio.** Sobrescribe `__init__`, se salta
`BaseProvider.__init__`, y revienta mucho después con `AttributeError: '_poll_cache_max_age'`. Trampa clásica escribiendo un provider rápido.

**G14. P0-03: `GMICloudBase.__init__` se traga el kwarg `models=`.** `GMICloudVideoProvider(models=reg)` lanza `TypeError`
pese a estar documentado ⇒ no puedes registrar pricing propio en GMICloud por esa vía.

**G15. P0-01: `from_result()` se estrechó en silencio** — ya no hidrata los steps completados, solo fija `parent_run_id`.
Un step con `input_from` que cruce runs falla con "index 0 is out of range" (el error culpa al sitio equivocado).

### 5.5 Fragilidades históricas (cerradas, pero comprueba tu versión instalada)

- **#162** — B2 free tier: al superar el cap diario de transacciones Class B, `HeadObject` devuelve 403, el sink lo
  interpreta como "no subido", reintenta y hace un GET no autenticado de la URL durable ya privada ⇒ **falla el ingest
  entero aunque los bytes estén a salvo**. Reportado por un equipo de hackathon. Arreglado en `genblaze-s3` ≥ 0.3.6 (la versión actual).
- **#164** — subidas `file://` desde Windows rotas (backslashes / letra de unidad). Arreglado en main.
- **#57** — `Pipeline` no llamaba a `sink.close()` → fuga del pool de threads. Si tu core es viejo, cierra el sink a mano.
- **#83** — `batch_run(max_concurrency=N)` **ignora la concurrencia** (siempre secuencial) y `abatch_run(max_concurrency=0)`
  **se cuelga para siempre** (`asyncio.Semaphore(0)`). **Nunca pases `max_concurrency=0`.**
- **#136** — `VeoProvider` roto en modo **Vertex AI** (funciona bien con `GEMINI_API_KEY`).
- **#126** — encadenado Sora imagen→vídeo roto (`image=` no existe en el SDK openai 2.x; requiere `input_reference` con bytes).
- **#86** — en cancelación async, `step.failed` llega con distinto `step_id` que `step.started` (rompe UIs de progreso).

### 5.6 Ojo con `ObjectStorageSink`: es de un solo uso

De la docstring de `run()`: los sinks con recursos por-run (como `ObjectStorageSink`) **se cierran automáticamente
al terminar el run** (`close()` en un `finally`), así que **quedan gastados**. Hay que construir uno nuevo por run.
`WebhookSink` se excluye (`_close_with_run = False`) y sí es reutilizable.

---

## 6. Features infrautilizadas que puntúan en "Use of Genblaze"

El criterio exige **orquestación real multi-proveedor / multi-paso**, no una llamada suelta. Lo que casi nadie usa:

### 6.1 `AgentLoop` + `Evaluator` con un juez real (LLM/visión)
La feature más potente y la que menos se usa con providers reales — el único ejemplo (`agent_loop_local.py`) va con mocks.
Un `ThresholdEvaluator` cuyo `score_fn` llame a un modelo de visión (GPT-4o o Nemotron vía NIM) convierte la demo en
**generate → judge → refine automático**, y cada iteración queda encadenada por `parent_run_id` en los manifests.
Es literalmente "orquestación multi-paso con feedback" servida en bandeja.

### 6.2 Fan-in real con `input_from=[0, 1]` + `FFmpegCompositor` / `FFmpegTransform`
Solo `fan_in_av_composite.py` lo demuestra. Un DAG de verdad (dos providers independientes → un step de composición local)
es mucho más vistoso que una cadena lineal, **y el paso de composición es gratis** (ffmpeg local, sin API key).
`FFmpegTransform` añade `resize`/`crop`/`overlay_text`/`audio_normalize`/`convert_format` gratis como pasos extra del pipeline.

### 6.3 Object Lock sobre los manifests (`manifest_lock=ObjectLockConfig(...)`)
**Ningún ejemplo lo usa.** Es la feature que une "provenance" con "Backblaze B2" de forma que ningún otro storage iguala:
provenance **inmutable y demostrable** (`mode="COMPLIANCE"` ⇒ ni root puede borrarlo hasta que expire).
Narrativa de demo perfecta para un hackathon de Backblaze. Recuerda: el bucket debe crearse con Object Lock activado.

### 6.4 Manifest embebido en el propio fichero + verificación por CLI
`Mp4Handler`/`PngHandler`/`SmartEmbedder` incrustan el manifest **dentro** del `.mp4`/`.png`, y luego
`genblaze verify video.mp4 --fetch` lo re-verifica descargando y re-hasheando los bytes reales.
Poder decir "descarga el fichero, pásale el CLI y comprueba tú mismo que no está manipulado" es un cierre de demo demoledor.
Complemento: `EmbedPolicy(embed_mode="pointer", prompt_visibility=PRIVATE)` para redactar prompts sensibles.

### 6.5 Observabilidad + analítica en el mismo run
Casi nadie usa: `ObjectStorageSink(..., parquet_sink=ParquetSink("data/"))` (assets+manifest en B2 **y** tablas
run/step/asset en Parquet a la vez), `CompositeTracer([LoggingTracer(), OTelTracer()])`, `WebhookSink`/`WebhookNotifier`
(para un dashboard en vivo), `.stream()`/`.astream()` para UI en tiempo real, y `estimated_cost()` para mostrar el
coste **antes** de ejecutar. Un panel en vivo con eventos + coste acumulado + tablas Parquet demuestra que has usado
el SDK en serio, no solo `.run()`.

**Bonus baratos**: `fallback_models=[...]` + `RetryPolicy.conservative()` (resiliencia visible),
`PipelineTemplate` (guardar/reinstanciar pipelines desde JSON), `StepCache` (dedupe entre iteraciones — ahorra dinero
durante el desarrollo), `ModerationHook` (screening pre-prompt), `Pipeline.ingest()` (provenance de material de origen),
y `chat()` de OpenAI/Google/GMICloud/NVIDIA envuelto en un `SyncProvider` para que **el guion escrito por el LLM también
quede en el manifest** (receta exacta en `docs/features/llm-calls.md`).

---

## 7. Snippet mínimo verificado: prompt → generación → B2 con metadata

### 7.1 Verificación realizada

Ejecutado en un venv limpio con `genblaze` 0.4.5 (`genblaze-core` 0.3.8, `genblaze-s3` 0.3.6) contra un servidor
S3 local (moto) — **exactamente el mismo code path que B2**, cambiando solo `endpoint_url`. Resultado real:

```
asset url   : http://127.0.0.1:5111/genblaze-demo/genblaze/assets/fd/87/fd8776...c62.png
sha256      : fd8776a4f2ddbf5b494ea9556ac9e0f2c71dfbdd8d1e208281de31e491447c62
size_bytes  : 568
cost_usd    : 0.04
manifest_uri: http://127.0.0.1:5111/genblaze-demo/genblaze/manifests/340afa31-....json
canon hash  : 99ce6f269b60de7d53b304d4e905b0e7f84c0e9095d8bbe41fb2475b92a74e31
verify()    : True
bucket keys : ['genblaze/assets/fd/87/fd8776....png', 'genblaze/manifests/340afa31-....json']
```

Confirmado: subida automática del asset, subida automática del manifest, reescritura de `asset.url`,
SHA-256 calculado al vuelo, `verify() == True`, y `run.metadata` / `step.metadata` presentes en el manifest.

### 7.2 El snippet (versión B2 real)

```python
"""prompt -> generación -> Backblaze B2 con metadata + provenance verificable."""
import os
from genblaze_core import KeyStrategy, Modality, ObjectStorageSink, Pipeline
from genblaze_s3 import S3StorageBackend

# export B2_KEY_ID=...   export B2_APP_KEY=...
# (opcional: B2_BUCKET, B2_REGION)

backend = S3StorageBackend.for_backblaze(
    "mi-bucket",
    region="us-west-004",
    # public_url_base="https://f004.backblazeb2.com/file/mi-bucket",  # si el bucket es público
    auto_lifecycle=True,   # aborta multipart huérfanos + expira versiones viejas de manifests
    preflight=True,        # HeadBucket inmediato: credenciales malas fallan AQUÍ, no a mitad del run
)

# OJO: un ObjectStorageSink se cierra al terminar el run -> construye uno nuevo por run.
sink = ObjectStorageSink(
    backend,
    prefix="hackathon",
    key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,   # {prefix}/assets/{sha[:2]}/{sha[2:4]}/{sha}.ext
)

# --- elige UN provider (NVIDIA NIM es el más barato: free tier ~40 req/min) ---
from genblaze_nvidia import NvidiaImageProvider          # pip install "genblaze[nvidia]"
provider, model = NvidiaImageProvider(), "black-forest-labs/flux.1-schnell"
# alternativas: from genblaze_openai import DalleProvider  -> "dall-e-3"
#               from genblaze_gmicloud import GMICloudImageProvider -> "seedream-5.0-lite"

result = (
    Pipeline("hackathon-demo", tenant_id="team-1", project_id="demo")
    .metadata(campaign="launch", author="me")          # -> manifest.run.metadata
    .step(
        provider,
        model=model,
        prompt="a neon skyline at dusk, cinematic",
        modality=Modality.IMAGE,
        metadata={"scene": "01"},                      # -> manifest.run.steps[0].metadata
        fallback_models=["stabilityai/stable-diffusion-xl"],   # failover ante MODEL_ERROR
    )
    .run(sink=sink, timeout=300, raise_on_failure=True)
)

asset = result.run.steps[0].assets[0]
print("URL durable :", asset.url)                  # ya apunta a tu bucket B2, sin credenciales
print("SHA-256     :", asset.sha256)
print("Manifest    :", result.manifest.manifest_uri)
print("Hash canón. :", result.manifest.canonical_hash)
print("Verificado  :", result.manifest.verify())   # True

# URL temporal firmada (1h por defecto) si el bucket es privado
print("Presigned   :", backend.presigned_get_url(asset.url.split("/", 4)[-1], expires_in=900))
```

### 7.3 Variante sin ninguna API key (para ensayar el cableado)

```python
import hashlib, tempfile
from pathlib import Path
from genblaze_core import Asset, KeyStrategy, Modality, ObjectStorageSink, Pipeline, MockProvider
from genblaze_s3 import S3StorageBackend

data = b"bytes que simulan la salida del modelo"
p = Path(tempfile.gettempdir()) / "demo.png"          # el sink permite file:// en el temp del sistema
p.write_bytes(data)

provider = MockProvider(
    name="demo-provider",
    assets=[Asset(url=p.as_uri(), media_type="image/png",
                  sha256=hashlib.sha256(data).hexdigest())],
    cost_usd=0.04,
)
sink = ObjectStorageSink(S3StorageBackend.for_backblaze("mi-bucket"),
                         key_strategy=KeyStrategy.CONTENT_ADDRESSABLE)
result = (Pipeline("dry-run")
          .step(provider, model="demo-model-1", prompt="a neon skyline", modality=Modality.IMAGE)
          .run(sink=sink, progress=False))
print(result.manifest.verify())   # True
```

Con esto validas credenciales de B2, layout de claves y provenance **sin gastar un céntimo en generación**.

### 7.4 Añadir Object Lock (el diferenciador B2)

```python
from datetime import datetime, timedelta, timezone
from genblaze_core import ObjectLockConfig

sink = ObjectStorageSink(
    backend,
    key_strategy=KeyStrategy.CONTENT_ADDRESSABLE,
    manifest_lock=ObjectLockConfig(
        retain_until=datetime.now(timezone.utc) + timedelta(days=7),
        mode="GOVERNANCE",     # "COMPLIANCE" = ni root puede borrarlo
    ),
)
# Requiere que el bucket se haya CREADO con Object Lock habilitado (no se activa después).
```

---

## 8. Recomendación operativa para 24h

1. **Setup (30 min)**: `pip install "genblaze[all]"` (sin pin) → `pip freeze > requirements.lock` inmediatamente.
   Bucket de B2 **creado con Object Lock habilitado** desde el minuto uno.
2. **Valida el camino de storage con `MockProvider`** (§7.3) antes de tocar ningún provider de pago.
3. **Una key barata que lo cubra todo**: `NVIDIA_API_KEY` de build.nvidia.com (vídeo+imagen+audio+chat, free tier ~40 req/min).
   Segunda key para diversidad de proveedor: GMICloud (imagen barata) o ElevenLabs (audio).
4. **Ensaya una generación real por cada modelo** del camino de demo — el preflight miente (G5).
5. **Arquitectura que puntúa**: chat/LLM (guion) → imagen (provider A) → vídeo (provider B) + audio (provider C)
   → `FFmpegCompositor` con `input_from=[i, j]` → `ObjectStorageSink` a B2 con `manifest_lock` →
   manifest embebido en el MP4 → `genblaze verify --fetch` en vivo. Envuelve todo en un `AgentLoop` con un juez de visión.
6. **Evita**: audio de GMICloud (G4), Veo por Vertex, edición de imagen desde URL https (G6),
   `max_concurrency=0` (#83), `PromptTemplate` posicional (G1), `genblaze_core.testing` (G2).
