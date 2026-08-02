# PLAN — FirstFrame: sala de revisión en vivo sobre B2 + Genblaze

Deadline: 2026-08-03 23:00 GMT+2. Submit objetivo: H-2 (21:00). ~20h útiles desde ahora.
Repo: `/home/xdarksyderx/Escritorio/Projects/genblaze-hackathon`. Venv: `.venv/` (activar con `source .venv/bin/activate`). Python 3.13. genblaze 0.4.5.

---

## 0. Hechos verificados contra los servicios reales — LEER ANTES DE TOCAR NADA

Todo esto está probado ejecutando código, no leído en docs. Detalle en `VALIDACION.md`.

**Entorno:** repo `/home/xdarksyderx/Escritorio/Projects/genblaze-hackathon`, venv `.venv/`
(genblaze 0.4.5 + 15 conectores), Python 3.13, credenciales en `.env` (gitignored).

**B2** — cuenta `ffc72b988bce`, región **`eu-central-003`**,
endpoint `https://s3.eu-central-003.backblazeb2.com`.
- Bucket **`genblaze-review-migarci2`** ya creado **con Object Lock activado**. No crear otro.
- Bucket de pruebas sin lock: `genblaze-scratch-migarci2`.
- La key tiene `writeBucketNotifications`, `readBucketNotifications`, `writeFileRetentions`,
  `writeFileLegalHolds`, `writeBucketRetentions`, `writeBucketLifecycleRules`,
  `bypassGovernance`, `writeKeys`. ⇒ Event Notifications, Object Lock, lifecycle y
  application keys restringidas son **todas ejecutables**, no aspiracionales.
- **Live Read: NO disponible** (ver §5). No perder ni un minuto más en ello.

**Proveedores:**
- NVIDIA NIM: **chat y visión SÍ, generación de imagen NO** (`ai.api.nvidia.com/v1/genai/...`
  cuelga sin responder; la free tier no lo incluye).
- **Juez de visión del AgentLoop: `meta/llama-3.2-90b-vision-instruct`, gratis y verificado.**
  Formato obligatorio: array de contenido estilo OpenAI con `{"type":"image_url",...}`.
  El estilo `<img src="data:...">` inline **da respuestas incorrectas**, y
  `nemotron-nano-12b-v2-vl` falla incluso con el formato bueno. Usar el de 90b.
- **No hay proveedor de generación de media todavía.** Todo el desarrollo va con
  `DEMO_MODE=mock` (`MockProvider`/`MockVideoProvider` desde `genblaze_core`, nunca desde
  `genblaze_core.testing`) + `PassthroughProvider` con imágenes locales de ffmpeg.
  `pipeline/providers.py:PassthroughProvider` **ya está escrito y con su demo() en verde**.

**Genblaze 0.4.5 — reglas duras:**
- **Construir SIEMPRE `Pipeline(..., preflight=False)`.** El default es `preflight=True` y usa
  `validate_model()`, que está invertido (#248): marca válidos modelos que dan 404.
- **`fallback_models` NO cubre timeouts de transporte.** Un `read operation timed out` no es
  `MODEL_ERROR` y el failover no salta. El `ChaosWrapper` debe lanzar `MODEL_ERROR` de verdad.
- `PipelineResult` no expone `.steps`; introspeccionar antes de usar.
- `run(raise_on_failure=True)` para que un step fallido no pase silencioso.
- `ObjectStorageSink` es de un solo uso: uno nuevo por run.
- Nada de `@dataclass` sobre subclases de `SyncProvider`.

**Contribuciones upstream ya abiertas** (citar en el README, es diferenciador):
PRs [#258](https://github.com/backblaze-labs/genblaze/pull/258),
[#259](https://github.com/backblaze-labs/genblaze/pull/259),
[#260](https://github.com/backblaze-labs/genblaze/pull/260) e
issue [#261](https://github.com/backblaze-labs/genblaze/issues/261).

---

## 1. Decisión de producto (5 líneas)

**Usuario:** Ana Ruiz, productora en Forge&Frame, estudio de 6 personas que entrega ~40 spots de producto/semana generados con IA para marcas DTC. Su cuello de botella no es generar: es **esperar 3–5 minutos por render completo para rechazarlo en el segundo 10** (rechaza ~30%).
**Qué hace en la app:** pega un brief, pulsa "New spot". El pipeline genera un spot multi-escena; el máster se sube a B2 **segmento a segmento (HLS incremental)** y a los ~7 segundos Ana ya está viendo el segundo 0:00 mientras la escena 5 aún se está generando. Rechaza en caliente → un juez de visión + AgentLoop refina y relanza la escena. Aprueba → el final queda en `approved/` con **Object Lock GOVERNANCE 30d** y manifest de provenance embebido y verificable (`genblaze verify --fetch`), listo para entregar al cliente.
**Por qué el preview progresivo le cambia la vida:** su ciclo de iteración pasa de "minutos por intento" a "segundos hasta primer fotograma": ~10× más iteraciones/hora con el mismo presupuesto de GPU, y los rechazos dejan de quemar minutos de render que nadie va a ver.

---

## 2. Arquitectura

Un solo servicio + estáticos. Todo Python porque el venv con genblaze ya existe y los agentes no pierden tiempo en toolchains.

| Componente | Tech | Por qué (velocidad, no elegancia) |
|---|---|---|
| Backend `server/` | **FastAPI + uvicorn**, un proceso | Un solo deploy; SSE y streaming chunked nativos; mismo intérprete que genblaze → el pipeline corre como background task, sin cola externa |
| Estado | **sqlite3 stdlib** (`data/firstframe.db`) | Cero infra; tablas jobs/events/decisions; idempotencia de webhooks con `INSERT OR IGNORE` |
| Frontend `web/` | **HTML + JS vanilla + CSS, sin build** | Servido por FastAPI `StaticFiles`; un agente lo escribe entero contra un contrato de API congelado; `<video>` + EventSource, MSE como fallback |
| Pipeline `pipeline/` | **genblaze 0.4.5** puro | Ya instalado; steps concretos en §4 |
| Media | **ffmpeg local** (ya requerido por FFmpegCompositor) | Remux fMP4, concat, overlays — gratis |
| Storage | **1 bucket B2** `genblaze-review-migarci2` (Object Lock activado en creación) | Layout en §3 |
| Deploy | **Fly.io con Dockerfile** (`python:3.13-slim` + `apt install ffmpeg`) | Necesitamos ffmpeg en la imagen y proceso persistente para streams largos y background pipelines → descarta Vercel serverless. Fallback: Render con Docker. Último recurso: local + `cloudflared tunnel` |

**Comunicación:** navegador ↔ FastAPI por REST + SSE (`/api/events`) + stream de vídeo (`/stream/{job_id}`). B2 ↔ backend por webhook firmado (`/webhooks/b2`) **y** poller (conmutable por env `EVENTS_MODE=webhook|poll|both`, default `both`). El navegador nunca habla con B2 directo salvo presigns AWS4 **path-style** (#246) para descargas de aprobados.

**Contrato de API (congelar en H1 en `server/API.md`; W3 solo depende de esto):**
```
GET  /api/jobs                      → [{id,status,title,scenes:[{n,status,ms}],created_at,first_frame_ms,total_render_ms}]
POST /api/jobs {brief}              → {id}          (arranca pipeline en background)
GET  /api/jobs/{id}                 → job + manifest_url + lock:{mode,retain_until} + provider_events[]
POST /api/jobs/{id}/decision {action:"approve"|"reject", note}
GET  /api/events                    → SSE: {type: job_update|render_started|render_complete|scene_ready|provider_failover|approved, ...}
GET  /stream/{job_id}               → HLS incremental servido desde B2 (m3u8 regenerado por segmento)
POST /api/chaos {provider}          → mata un proveedor para la demo de failover
POST /webhooks/b2                   → receptor Event Notifications (HMAC v1=)
GET  /api/verify/{job_id}           → ejecuta `genblaze verify --fetch` server-side, devuelve resultado
```

**Flujo de un job:** `POST /api/jobs` → `pipeline/runner.py:run_job()` genera escenas (una a una) → cada escena terminada entra en `server/assembler.py` que la remuxa a fMP4 con offset de timestamps y sube cada segmento como objeto propio a `incoming/{job}/seg/{00001}.m4s`, regenerando `incoming/{job}/index.m3u8` tras cada uno → el navegador reproduce ya vía `/stream/{job_id}` (polling del m3u8, 404-retry) → al completar el multipart, evento B2 → estado `in_review` → approve = embed manifest en el MP4 (Mp4Handler) + `put_object` a `approved/{job}/final.mp4` con `ObjectLockMode=GOVERNANCE` → reject = `POST` relanza escena vía AgentLoop y copia el descartado a `rejected/`.

**Vídeo con bitrate alto a propósito:** todas las escenas se transcodifican a parámetros idénticos — `libx264, 1280x720, 24fps, -g 48, -b:v 6M -maxrate 6M -bufsize 12M, yuv420p, -movflags empty_moov+default_base_moof+frag_keyframe` — así 5 MiB ≈ 6–7 s de vídeo (latencia de parte razonable) y los fragmentos fMP4 son concatenables.

---

## 3. Layout del bucket B2

**Bucket:** `genblaze-review-migarci2`, privado, **Object Lock activado en la creación** (no se puede activar después). Consecuencia asumida: límite nombre+file-info 2048 bytes → **todos los manifests van como objetos, jamás como metadata**.

```
refs/{job_id}/...                     insumos (imagen de producto) documentados con Pipeline.ingest
incoming/{job_id}/seg/init.mp4        init segment fMP4
incoming/{job_id}/seg/{00001}.m4s     segmentos, subidos conforme se generan
incoming/{job_id}/index.m3u8          playlist regenerada tras cada segmento
runs/{job_id}/scene-{n}/...           intermedios genblaze: keyframes, clips, audio, manifests de escena
provenance/{job_id}/manifest.json     manifest agregado del job (objeto, no metadata)
approved/{job_id}/final.mp4           máster con manifest embebido + Object Lock GOVERNANCE 30d
approved/{job_id}/manifest.json       escrito por el sink con manifest_lock=ObjectLockConfig(...)
rejected/{job_id}/take-{k}.mp4        tomas rechazadas (evidencia del loop de refinado)
```

**Lifecycle rules (4; corren 1×/día — son declaración de arquitectura, no se verán actuar en la demo; se enseñan en el README y en la consola):**
1. `incoming/` → `daysFromStartingToCancelingUnfinishedLargeFiles: 1` — un job de vídeo caído deja un multipart huérfano; B2 lo cancela solo. Mencionarlo por nombre en README y vídeo.
2. `rejected/` → `daysFromUploadingToHiding: 1`, `daysFromHidingToDeleting: 7`.
3. `runs/` → `daysFromUploadingToHiding: 3`, `daysFromHidingToDeleting: 7`.
4. `approved/` → `daysFromHidingToDeleting: 1` — purga agresiva de versiones ocultas **que no puede tocar lo bloqueado**: Object Lock gana. Es la combinación que demuestra entender el sistema.

**Event Notification rules (5 de 25; sin prefijos solapados dentro del mismo event type; HMAC compartido en env `B2_WEBHOOK_SECRET`):**
| # | Nombre | Event type | Prefijo | Reacción del backend |
|---|---|---|---|---|
| 1 | `segment-landed` | `b2:ObjectCreated:Upload` | `incoming/` | SSE `render_started` → la sala muestra "LIVE" y arranca el player |
| 2 | `render-complete` | `b2:ObjectCreated:MultipartUpload` | `incoming/` | job → `in_review`, cronómetro se detiene |
| 3 | `asset-approved` | `b2:ObjectCreated:Upload` | `approved/` | SSE `approved`, badge de lock |
| 4 | `manifest-written` | `b2:ObjectCreated:Upload` | `provenance/` | enlaza manifest en la UI |
| 5 | `cleanup-audit` | `b2:HideMarkerCreated:LifecycleRule` | `rejected/` | feed de auditoría de limpieza |

(3 y 4 son mismo type con prefijos disjuntos → legal. 1 y 2 son types distintos sobre el mismo prefijo → legal.)

**Webhook obligatorio:** responder 200 en <3 s ⇒ `POST /webhooks/b2` verifica HMAC (`hmac.new(secret, raw_body, sha256).hexdigest()` vs header `X-Bz-Event-Notification-Signature` tras `v1=`), hace `INSERT OR IGNORE INTO events(event_id)` y encola en `queue.Queue`; un worker thread procesa. At-least-once ⇒ todo handler idempotente por `eventId`.

**Fallback obligatorio (gating de cuenta):** `server/events.py:Poller` — thread cada 2 s: `list_multipart_uploads(Prefix="incoming/")` + `list_objects_v2` sobre `incoming/`, `approved/`, `provenance/`; diff contra DB; emite los MISMOS eventos internos. En cámara es indistinguible. `infra/b2_setup.py` intenta crear las reglas; si recibe 403/unauthorized imprime `WARN: event notifications gated → EVENTS_MODE=poll` y sigue.

**Application keys (multi-tenancy real, 20 min de trabajo, mucha credibilidad):** `infra/make_keys.py` crea `firstframe-server` (todas las capabilities, solo este bucket) y `firstframe-reviewer` (solo `readFiles`, este bucket) e imprime ambas. README: "el revisor externo nunca tiene una key que pueda escribir".

---

## 4. Pipeline Genblaze

**Regla previa para W1:** antes de escribir una línea, leer las firmas reales del paquete instalado: `python -c "import genblaze, inspect; print(inspect.getsourcefile(genblaze))"` y abrir `pipeline.py` (ahí está `ingest` en la línea 578). No fiarse de ejemplos online.

**Providers (`pipeline/providers.py`):**
- `PassthroughProvider(SyncProvider)` (~10 líneas, **se escribe lo primero**, SIN `@dataclass` — rompe `BaseProvider.__init__` en silencio): devuelve un asset local existente como salida de step 0. Necesario porque `Pipeline.input(fichero)` no existe.
- `ChaosWrapper(SyncProvider)`: envuelve a otro provider; si `chaos.is_dead(name)` (flag en DB puesto por `/api/chaos`) lanza el error de `MODEL_ERROR` → dispara `fallback_models` en cámara.
- Reales: NVIDIA NIM (imagen: `black-forest-labs/flux.1-schnell`, fallback `stabilityai/stable-diffusion-3-5-large-turbo`; gratis, ~40 req/min), GMICloud `pixverse-v5.6` ($0.03/asset; fallback `seedance-2-0`), OpenAI `tts-1` para VO (**nunca audio de GMI: roto, #251**), `gpt-4o-mini` visión como juez.
- Mocks para TODO el desarrollo: `from genblaze_core import MockProvider, MockVideoProvider, MockAudioProvider` (**no** `genblaze_core.testing`, importa pytest). `DEMO_MODE=mock` sustituye cada provider real por su mock + clips `ffmpeg -f lavfi -i testsrc2` — el sistema entero se desarrolla y ensaya a coste $0.

**Steps por escena (`pipeline/scenes.py:build_scene_pipeline(scene, job)`):**
```
step 0  keyframe   : NIM flux.1-schnell, PromptTemplate(template="...")  ← SIEMPRE kwarg template=
                     fallback_models=["stabilityai/stable-diffusion-3-5-large-turbo"]
                     envuelto en AgentLoop + ThresholdEvaluator(score_fn=pipeline/judge.py:judge_frame,
                     threshold=0.7, max_iterations=2). judge_frame descarga el frame LOCAL con
                     extensión .png (OpenAI rechaza fuentes https://, #253), llama a gpt-4o-mini
                     visión con el brief, devuelve score 0..1. Iteraciones encadenadas por parent_run_id.
step 1  voiceover  : OpenAI tts-1 (texto del brief por escena). Transcripción/JSON → metadata["text"]
                     (no existe Asset.text).
step 2  clip       : ChaosWrapper(GMICloud pixverse-v5.6), input_from=[0] (imagen→vídeo),
                     fallback_models=["seedance-2-0"] → así el botón de chaos enseña failover real.
step 3  composite  : FFmpegCompositor, input_from=[1, 2]  ← FAN-IN real: mux VO + audio_normalize
                     + overlay_text "DRAFT · FirstFrame" + convert_format a los params comunes de §2.
```
- Encadenado fluido: `Pipeline(steps=[...]).cache(StepCache(".cache/steps")).run(...)` — **jamás** `run(cache=...)`.
- Sink: `ObjectStorageSink(S3StorageBackend.for_backblaze("genblaze-review-migarci2"))` — **uno NUEVO por run** (single-use), `close()` en `finally`. Prefijo `runs/{job}/scene-{n}/`.
- Nada de `abatch_run(max_concurrency=0)` (deadlock) ni contar con concurrencia en `batch_run` (es secuencial siempre). Las escenas se corren secuencialmente desde `runner.py`, que es justo lo que el preview por segmentos convierte en ventaja: se emite mientras se genera.

**Orquestación (`pipeline/runner.py:run_job(job_id, brief)`):** genera el plan de 6 escenas desde el brief (plantilla fija + NIM chat opcional), corre las 6 pipelines de escena en secuencia encadenando `parent_run_id`, y tras cada escena entrega el mp4 al assembler (`server/assembler.py:feed(job_id, scene_path)`).

**Manifest y provenance (`pipeline/manifest.py`):** agrega los manifests de escena en `provenance/{job}/manifest.json` (objeto — límite 2048 bytes de metadata en buckets con lock). En approve: `Mp4Handler`/`SmartEmbedder` embebe el manifest en `final.mp4`, `put_object` a `approved/` con `ObjectLockMode="GOVERNANCE"` y `ObjectLockRetainUntilDate=now+30d`; el manifest.json de `approved/` se escribe con `manifest_lock=ObjectLockConfig(...)` (nadie más lo usa: puntos). `GET /api/verify/{job}` corre `genblaze verify final.mp4 --fetch` y devuelve la salida. `genblaze replay manifest.json` se menciona en README y vídeo. `Pipeline.ingest(assets=[...], source=..., sink=...)` documenta la imagen de producto de `refs/`.

**Regla dura:** `validate_model()` está invertido (#248) — el ensayo de H10 genera DE VERDAD con cada modelo del camino de demo (flux.1-schnell, sd-3.5 fallback, pixverse, seedance, tts-1, gpt-4o-mini). Nada de preflight. Presupuesto real total: 2 runs completos ≈ $0.50.

---

## 5. Hora 0: spike del assembler de segmentos + player

**Live Read: descartado, ya verificado.** El spike se ejecutó contra la cuenta real
(`scripts/probe_liveread.py`): con multipart abierto y parte 1 de 5 MiB subida, `GetObject`
con `Range` devuelve **404 NoSuchKey**, no el 416 que define la API. Probado con el header
`x-backblaze-live-read-enabled` en `before-send` y en `before-sign`, sobre
CreateMultipartUpload, UploadPart y GetObject. Cuenta free; la feature se factura a $15/TB.
**No volver a intentarlo.** Detalle completo en `VALIDACION.md`.

**Espina ya verificada en verde** (`scripts/probe_spine.py`, reproducible):
pipeline Genblaze → `ObjectStorageSink` → B2 con layout content-addressable →
manifest → `manifest_lock=ObjectLockConfig(mode="GOVERNANCE")` → **B2 rechaza el borrado
con `AccessDenied`**. Ese último paso es el momento del vídeo y ya funciona.

### Spike único — `spike/player_spike/` (¿Chrome reproduce un HLS creciente?)

Mini-FastAPI que sirve un `index.m3u8` que va creciendo mientras otro proceso genera
segmentos. Generar los segmentos con ffmpeg a parámetros idénticos (§2) y
`-f hls -hls_time 4 -hls_list_size 0 -hls_flags append_list+independent_segments`.
Criterio de éxito: `<video>` + hls.js empieza a reproducir con 2 segmentos escritos y
sigue reproduciendo mientras aparecen los siguientes, sin recargar la página.

**Decisión que sale del spike (en C1, no en la demo):** `<video>`+hls.js si basta,
`web/mse.js` con `SourceBuffer.appendBuffer` por fragmento si hls.js da guerra con la
playlist creciente. Escribir el fallback desde el principio, no cuando falle.

### Riesgo propio de esta arquitectura
El generador puede ir más lento que la reproducción y dejar al player sin buffer.
Mitigación: no arrancar hasta tener 2 segmentos de colchón, bitrate de preview bajo,
y `#EXT-X-PLAYLIST-TYPE:EVENT` en la playlist para que el player sepa que va a crecer.

## 6. Workstreams paralelizables

**Camino crítico: W0 → W2 → integración (H6) → deploy (H10–13) → ensayo (H13) → vídeo (H16).**

**W0 — Spike assembler de segmentos + player** *(bloquea a todos; 2 agentes)*
(a) Ejecutar §5 A y B, decidir GO/plan B y decidir `<video>` vs MSE. (b) `spike/**`. (c) Veredicto escrito en `spike/VERDICT.md` con los dos flags. (d) Depende de: W4 fase 1 (bucket+keys, primeros 20 min).

**W1 — Pipeline Genblaze** *(1 agente)*
(a) §4 completo, con `DEMO_MODE=mock` por defecto. (b) `pipeline/providers.py`, `pipeline/scenes.py`, `pipeline/judge.py`, `pipeline/runner.py`, `pipeline/manifest.py`, `pipeline/chaos.py`. (c) `python -m pipeline.runner --job demo1 --mock` produce 6 mp4 con params comunes en `runs/demo1/` + manifests + agregado, y con `--mock --chaos gmicloud` se ve el failover en logs. (d) Nada (mocks). Providers reales se enchufan en H6–H10.

**W2 — Backend** *(1–2 agentes; CRÍTICO)*
(a) FastAPI completo: jobs, decisiones, SSE, webhook+poller, assembler, streamer, presigns. (b) `server/app.py`, `server/db.py`, `server/b2.py` (clientes boto3 con header injection + `presign_path_style()` AWS4), `server/assembler.py` (remux `-output_ts_offset`, strip ftyp/moov, `LivePartUploader` con cortes EXACTOS de 5 MiB — todas las partes iguales salvo la última), `server/streamer.py` (ranges de 512 KiB, sobre 416 `await asyncio.sleep(0.7)` y retry, fin cuando DB marca completo y offset≥size), `server/events.py`, `server/jobs.py`, `server/API.md`, `requirements.txt`. (c) Con `DEMO_MODE=mock`: `POST /api/jobs` → en <10 s `/stream/{id}` reproduce en Chrome mientras quedan escenas por generar; approve deja objeto lockeado en `approved/` y verify pasa. (d) W0 (flags), W4 fase 1 (bucket). Interfaz con W1: `runner.run_job()` llama a `assembler.feed()` — firma pactada en `server/API.md` en H1.

**W3 — Frontend** *(1 agente)*
(a) Sala de revisión: lista de jobs en vivo (EventSource), player con badge "LIVE — rendering scene N/6", cronómetros "first frame: Xs / render total: Ys" (números en pantalla para el vídeo), botones Approve/Reject con nota, panel provenance (manifest JSON + badge lock GOVERNANCE + botón Verify), toast de failover ("pixverse MODEL_ERROR → fallback seedance"), botón oculto de chaos (tecla `k`). (b) `web/index.html`, `web/app.js`, `web/mse.js`, `web/styles.css`. (c) Funciona entera contra el backend en mock; sin login; usable en 1280×720 (formato del vídeo). (d) Solo `server/API.md` (H1). Puede desarrollar con `server/app.py --stub` (endpoints con datos fake que W2 incluye desde el principio).

**W4 — Infra B2 + deploy** *(1 agente)*
(a) Fase 1 (primeros 20 min): crear `genblaze-review-migarci2` **con Object Lock**, application keys, `.env.example`. Fase 2: lifecycle (4 reglas §3), event rules (5 reglas §3, con degradación a `WARN → poll` si 403), Dockerfile (`python:3.13-slim` + ffmpeg), `fly.toml`, deploy. (b) `infra/b2_setup.py` (idempotente, imprime tabla de reglas), `infra/make_keys.py`, `Dockerfile`, `fly.toml`, `.env.example`, `.dockerignore`. (c) `python infra/b2_setup.py` termina verde contra la cuenta real; `fly deploy` sirve la app con `DEMO_MODE=mock`. (d) Fase 1: nada. Deploy: W2+W3 integrados (H10).

**W5 — Demo, seed y ensayo** *(1 agente, arranca H10)*
(a) `demo/seed.py`: precarga 1 job aprobado+lockeado y 1 job terminado en revisión (para que la URL viva tenga datos sin generar nada); `demo/run_demo.sh`: lanza el job de demo en vivo; `demo/chaos_script.md`: guion exacto del failover; 2 ensayos completos cronometrados, incluido 1 con proveedores reales. (b) `demo/**`. (c) El recorrido del vídeo (§10) sale 2 veces seguidas sin improvisar. (d) W1–W4 integrados.

**W6 — README + submission** *(1 agente, arranca H8 en paralelo)*
(a) README con secciones explícitas **"How we use B2"** y **"How we use Genblaze"**, feature por feature con enlaces a línea de código (HLS incremental servido desde B2 → `server/assembler.py` / `server/streamer.py`; Event Notifications+HMAC → `server/events.py`; Object Lock+lifecycle → `infra/b2_setup.py`, `pipeline/manifest.py`; app keys → `infra/make_keys.py`; fallback_models/AgentLoop/fan-in/manifest_lock/ingest/replay → ficheros de `pipeline/`). Texto de Devpost. Guion del vídeo. (b) `README.md`, `SUBMISSION.md`, `docs/architecture.png` (un diagrama, draw.io o mermaid renderizado). (c) Un juez entiende qué feature está dónde sin abrir el código. (d) Estructura de ficheros estable (H8).

**Reglas anti-colisión:** cada workstream toca SOLO sus ficheros; los únicos ficheros compartidos son `server/API.md` (lo escribe W2 en H1, luego solo-lectura) y `.env.example` (solo W4). Commits frecuentes en `main`, sin branches: no hay tiempo para merges.

**Gotchas obligatorios para TODOS los agentes (pegar en cada prompt de agente):** `PromptTemplate(template=...)`; mocks desde `genblaze_core` (no `.testing`); `.cache()` fluido; no `@dataclass` sobre `SyncProvider`; `ObjectStorageSink` nuevo por run + `close()` en `finally`; nunca `abatch_run(max_concurrency=0)`; texto en `metadata["text"]`; imágenes para OpenAI descargadas en local con extensión real; presigns AWS4 path-style; nunca pinchar `genblaze` a tags de GitHub; `validate_model()` miente — generar de verdad en el ensayo.

---

## 7. Cronograma (~20h desde H0)

| Hora | Qué | Quién |
|---|---|---|
| H0–H2 | W0 spikes A+B; W4 fase 1 (bucket con lock + keys + .env) | 3 agentes |
| **H2 — C1** | **¿Chrome reproduce el HLS creciente? Decisión `<video>`+hls.js / MSE. Congelar `server/API.md`.** | — |
| H2–H6 | W1 (pipeline mock), W2 (backend), W3 (frontend), W4 fase 2 (reglas) en paralelo | 4 agentes |
| **H6 — C2** | **Integración mock end-to-end en local: crear job → ver en vivo → approve → lock → verify.** Si el streamer no reproduce: 45 min máx para conmutar a MSE/plan B; si no, recorte R8. | — |
| H6–H10 | Providers reales en pipeline (NIM, pixverse, tts-1, juez); chaos/failover; embed+verify; pulir UI con cronómetros | W1+W2+W3 |
| **H10 — C3** | **Un run 100% real completo grabable.** Si pixverse falla → seedance directo → mock con overlay "simulated render" (honesto). | — |
| H10–H13 | Deploy Fly + seed data (W5) + README (W6 ya en marcha desde H8) | W4+W5+W6 |
| **H13 — C4** | **URL viva, sin login, con datos precargados. FEATURE FREEZE.** Todo lo no-verde se recorta según §8, no se arregla. | — |
| H13–H16 | 2 ensayos completos del guion (uno con proveedores reales, chaos incluido); solo bugfixes del camino de demo | W5 |
| H16–H18 | Grabar y montar el vídeo de 3 min | 1 humano + 1 agente |
| H18–H19 | Devpost: formulario, vídeo subido, repo compartido, checklist §11 | W6 |
| H19–H20 | Buffer. **Submit a las 21:00 GMT+2 como muy tarde.** | — |

---

## 8. Recortes, en orden de sacrificio

1. Juez de visión en AgentLoop → juez de texto sobre el prompt (gpt-4o-mini sin imagen), o refine único hardcoded. Se mantiene el loop y el `parent_run_id`.
2. Voiceover TTS + fan-in de audio → fuera; el `FFmpegCompositor` queda solo con overlay_text+normalize (sigue habiendo transform y compositor).
3. Webhook de Event Notifications → solo poller (`EVENTS_MODE=poll`); las reglas quedan creadas y documentadas.
4. 6 escenas → 3 escenas (mínimo para que el preview progresivo tenga sentido).
5. Manifest embebido en MP4 + `verify --fetch` → solo `manifest.json` como objeto lockeado.
6. Application keys multi-tenant → fuera del código, se cita en README.
7. Deploy Fly → app local + `cloudflared tunnel --url http://localhost:8000` como URL pública.
9. **Mínimo submitible:** pipeline mock → sube a B2 → player near-live → approve con Object Lock real → manifest → README honesto + vídeo. Sigue puntuando en los 4 criterios.

---

## 9. Riesgos top-5

1. **El player se queda sin buffer** porque el generador va más lento que la reproducción. Mitigación: no arrancar hasta tener 2 segmentos de colchón; bitrate del preview bajo; el seed data de la demo nunca depende de una generación en vivo.
2. **Chrome no reproduce el fMP4 creciente por el proxy.** Mitigación: spike B en H0 con fichero local (sin B2 de por medio, aísla la variable); `web/mse.js` se escribe desde el principio como fallback; parámetros de encode fijados y únicos en todo el sistema (§2).
3. **Proveedores de vídeo reales fallan o cuestan** (cosmos enterprise-gated, colas de GMICloud facturan, `validate_model` miente). Mitigación: `DEMO_MODE=mock` es el camino por defecto de TODO el desarrollo y del seed de la URL viva; solo 2 runs reales presupuestados (~$0.50) en C3 y el ensayo; cada modelo del camino de demo genera de verdad en H10; si todo falla, mock con overlay honesto.
4. **Event Notifications gated al crear reglas por API.** Mitigación: poller escrito el minuto cero, mismo bus de eventos internos, `EVENTS_MODE` conmutable; el webhook es bonus de credibilidad, no dependencia.
5. **El assembler (remux+offset+partes exactas de 5 MiB) se come el cronograma.** Es LA pieza difícil. Mitigación: es el único foco de W2 en H2–H6 con checkpoint duro en C2 y salida R8 preparada; el resto del backend es CRUD que cualquier agente termina; seed data garantiza que la demo nunca depende de una generación en vivo que salga mal.

---

## 10. Guion del vídeo (3:00)

- **0:00–0:20 — Problema.** Pantalla dividida: izquierda, terminal con un render al 40% y un cronómetro subiendo; derecha, foto de "Ana, productora — 40 spots/semana". Texto en pantalla: "3–5 min por render · 30% rechazados · el rechazo llega al final". Voz: una frase.
- **0:20–0:45 — Primer fotograma.** App en la URL viva (visible en la barra). Click "New spot", brief pegado. El job aparece con badge LIVE (cada `b2:ObjectCreated:Upload` de un segmento aparece en el feed en vivo). A los ~7 s el player arranca. Overlay grande: **"viendo 0:00 — la escena 4/6 aún se está generando"** y contador "first frame: 7 s vs render total: 2:43".
- **0:45–1:15 — Rechazo + auto-refine.** Ana rechaza en el segundo 15 ("logo ilegible"). Panel del AgentLoop: juez de visión puntúa 0.4 → prompt refinado → escena relanzada; timeline de runs encadenados por `parent_run_id`. La toma mala cae a `rejected/` (y se ve la lifecycle rule que la purgará).
- **1:15–1:45 — Failover en cámara.** Tecla de chaos: GMICloud muere. Toast: "pixverse-v5.6 MODEL_ERROR → fallback: seedance-2-0". El render continúa sin intervención. Texto: "0 acciones humanas para recuperarse".
- **1:45–2:20 — Approve + provenance.** Approve → badge "Object Lock GOVERNANCE · 30 días". Corte a la consola de B2: intento de borrar `approved/demo/final.mp4` → error en cámara. Terminal: `genblaze verify final.mp4 --fetch` → manifest embebido, verificado. Panel provenance con el manifest.json.
- **2:20–3:00 — Arquitectura.** Un solo diagrama. Voz recorre: B2 (HLS servido directo desde el bucket, Event Notifications firmadas HMAC, Object Lock + lifecycle que no puede tocarlo, `daysFromStartingToCancelingUnfinishedLargeFiles` para renders muertos, app keys de solo-lectura para revisores) y Genblaze (pipeline por escena, `fallback_models`, AgentLoop+Evaluator con juez real, fan-in FFmpeg, `manifest_lock`, `ObjectStorageSink`, `replay`). Cierre: URL + "el primer segundo, en el primer segundo".

---

## 11. Checklist Devpost

- [ ] URL viva **sin muro de login**, con seed data (1 job aprobado+lockeado, 1 en revisión) y botón "Start demo render" en modo mock (coste $0 para los jueces). Probada desde incógnito y desde móvil.
- [ ] Vídeo ≤3 min subido (YouTube unlisted), siguiendo §10, con números en pantalla y el failover en cámara.
- [ ] Repo público; si privado, invitado **`b2genblaze`**.
- [ ] README con secciones explícitas "How we use B2" y "How we use Genblaze", feature por feature, con enlaces a fichero:línea (§6-W6).
- [ ] Lista de proveedores/modelos declarada: NVIDIA NIM (flux.1-schnell, sd-3.5-large-turbo), GMICloud (pixverse-v5.6, seedance-2-0), OpenAI (tts-1, gpt-4o-mini juez), Mock providers para desarrollo.
- [ ] `.env.example` completo y `infra/b2_setup.py` reproducible (un juez con su cuenta puede levantarlo).
- [ ] Mención explícita en el texto de Devpost de: HLS incremental servido desde B2, Event Notifications (HMAC), Object Lock vs lifecycle, `daysFromStartingToCancelingUnfinishedLargeFiles`, application keys restringidas, AgentLoop+Evaluator, fallback_models, fan-in, manifest_lock, replay.
- [ ] Submit ≥2h antes del cierre (21:00 GMT+2 del 2026-08-03). Verificar que el formulario quedó ENVIADO, no en borrador.
