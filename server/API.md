# FirstFrame — contrato de API (CONGELADO)

Este fichero es la única fuente de verdad entre W2 (backend), W3 (frontend) y W1 (pipeline).
Lo escribe W2. A partir de aquí es **solo lectura** para todos los demás.

Base URL en local: `http://localhost:8000`. Sin auth, sin login, sin CORS (mismo origen:
FastAPI sirve `web/` en `/`).

Todos los cuerpos son JSON (`application/json`) salvo el stream HLS.
Todos los timestamps son epoch en **milisegundos** (int) salvo `created_at`, que además
viene en ISO-8601 UTC como `created_at_iso` por comodidad.

---

## Modo stub (para desarrollar el frontend sin backend real)

```bash
STUB=1 .venv/bin/uvicorn server.app:app --port 8000
```
Con `STUB=1` **todos** los endpoints devuelven datos fake coherentes y deterministas
(2 jobs precargados: uno `approved`, uno `in_review`; SSE emite eventos sintéticos cada 2 s).
El shape de las respuestas es idéntico al real. El frontend puede desarrollarse entero
contra esto desde el minuto uno.

---

## Modelos

### Job
```jsonc
{
  "id": "j_7f3a91",                  // string opaco, seguro para URLs
  "title": "Spot 15s zapatilla Aeron",
  "brief": "...",                    // texto original del usuario
  "status": "queued|rendering|in_review|approved|rejected|failed",
  "scenes": [ /* Scene[] */ ],
  "scene_count": 6,
  "created_at": 1785000000000,
  "created_at_iso": "2026-08-03T09:20:00Z",
  "first_frame_ms": 7120,            // null hasta que el 1er segmento está en B2
  "total_render_ms": 163000,         // null hasta que el job termina
  "stream_url": "/stream/j_7f3a91/index.m3u8",
  "manifest_url": "/api/jobs/j_7f3a91/manifest",   // null si aún no hay manifest
  "lock": {                          // null si no está aprobado
    "mode": "GOVERNANCE",
    "retain_until": "2026-09-02T09:31:00Z"
  },
  "error": null                      // string si status == "failed"
}
```

### Scene
```jsonc
{
  "n": 3,                            // 1-indexado
  "status": "pending|rendering|ready|failed",
  "ms": 24310,                       // duración de generación; null si aún no terminó
  "title": "Plano detalle de la suela",
  "path": "runs/j_7f3a91/scene-3/composite.mp4"  // clave B2, informativo
}
```

### ProviderEvent
```jsonc
{
  "at": 1785000031000,
  "scene": 2,
  "kind": "provider_call|provider_failover|judge_score|retry",
  "provider": "gmicloud",
  "model": "pixverse-v5.6",
  "fallback_model": "seedance-2-0",  // solo en provider_failover
  "detail": "MODEL_ERROR: chaos injected",
  "score": 0.42                      // solo en judge_score
}
```

---

## Endpoints

### `GET /api/jobs`
Lista de jobs, más reciente primero.
```jsonc
200 → { "jobs": [ Job, ... ] }
```
(Nota: envuelto en objeto, **no** array desnudo, para poder añadir campos sin romper.)

### `POST /api/jobs`
Crea un job y arranca el pipeline en background inmediatamente. Devuelve al instante.
```jsonc
req  → { "brief": "spot de 15s para una zapatilla...", "title": "opcional", "scenes": 6 }
201  → { "id": "j_7f3a91", "job": Job }
400  → { "error": "brief is required" }
```
`scenes` es opcional (default 6, máximo 6, mínimo 1).

### `GET /api/jobs/{id}`
```jsonc
200 → {
  "job": Job,
  "provider_events": [ ProviderEvent, ... ],
  "decisions": [ {"at":..., "action":"approve|reject", "note":"...", "scene": null} ],
  "objects": [ {"key":"approved/j_.../final.mp4","size":123,"at":...} ]  // lo que hay en B2
}
404 → { "error": "no such job" }
```

### `POST /api/jobs/{id}/decision`
```jsonc
req  → { "action": "approve" | "reject", "note": "logo ilegible", "scene": 3 }
200  → { "ok": true, "job": Job }
409  → { "error": "job not reviewable yet", "status": "rendering" }
```
- `approve`: embebe el manifest en el mp4 final, lo sube a `approved/{id}/final.mp4` con
  Object Lock GOVERNANCE +30d, emite SSE `approved`. Devuelve el job ya con `lock` puesto.
- `reject`: copia la toma a `rejected/{id}/take-{k}.mp4`, pone el job en `rendering` y
  relanza la escena indicada (o la última). **La toma refinada se añade a la MISMA
  playlist como escena nueva**, así que entra en vivo. Secuencia de SSE:
  `rejected` → `judge_score` → `segment_landed`… → `scene_ready` → `render_complete`.
  Como la playlist ya tenía `#EXT-X-ENDLIST`, hls.js la considera VOD: al recibir
  `rejected`, el front debe **volver a llamar a `hls.loadSource(...)`** para reengancharse.
- `scene` es opcional; solo se usa en `reject`.

### `GET /api/events` — SSE
`Content-Type: text/event-stream`. Cada mensaje es
```
event: <type>
data: <json>

```
Un `event: ping` cada 15 s para mantener la conexión viva (ignóralo).
Al conectar se envía inmediatamente un `event: hello` con `{"jobs":[Job,...]}` para
sincronizar el estado sin una llamada extra a `/api/jobs`.

Tipos y payloads (todos llevan `job_id` y `at`):

| `type` | payload extra | cuándo |
|---|---|---|
| `hello` | `jobs: Job[]` | al conectar |
| `job_update` | `job: Job` | cualquier cambio de estado del job |
| `render_started` | `scene: n` | primer segmento del job aterriza en `incoming/` (badge LIVE) |
| `segment_landed` | `key`, `seq`, `duration` | cada segmento HLS subido a B2 (feed en vivo) |
| `scene_ready` | `scene: n`, `ms`, `job: Job` | una escena terminó de generarse |
| `render_complete` | `job: Job`, `total_render_ms` | todas las escenas listas → `in_review` |
| `provider_failover` | `provider`, `model`, `fallback_model`, `scene` | toast de failover |
| `judge_score` | `scene`, `score`, `iteration` | puntuación del juez de visión |
| `approved` | `job: Job`, `lock: {...}`, `key` | approve confirmado con lock en B2 |
| `rejected` | `job: Job`, `note`, `scene` | reject registrado |
| `chaos` | `provider`, `dead: bool` | proveedor matado/revivido |
| `ping` | `{}` | keepalive |

### `GET /stream/{job_id}/index.m3u8`
Playlist HLS. `Content-Type: application/vnd.apple.mpegurl`, `Cache-Control: no-store`.
Mientras el job está vivo lleva `#EXT-X-PLAYLIST-TYPE:EVENT` y **no** lleva `#EXT-X-ENDLIST`;
al terminar aparece `#EXT-X-ENDLIST`. Los `URI`/rutas de los segmentos son **relativos**
(`seg/00001.ts`), así que resuelven contra el mismo prefijo.

**Para W3 — verificado en Chrome con hls.js 1.5, no hace falta que hagas nada especial:**
llama a `hls.loadSource('/stream/{id}/index.m3u8')` **inmediatamente** después del
`POST /api/jobs`. Dos protecciones del backend hacen que eso siempre funcione:
1. Al crear el job se publica una **cabecera de 2 s** ("FirstFrame · LIVE / generando N
   escenas"), así que la playlist existe ~2 s después del POST aunque la primera escena
   real tarde 10 s. Es el segmento de `scene: 0`; no entra en el `final.mp4` aprobado.
2. El servidor además **retiene** la primera petición del m3u8 hasta 6 s esperando al
   primer segmento.

Motivo: si el primer GET devuelve **404**, hls.js lanza `manifestLoadError` FATAL y
**no reintenta nunca**; si devuelve una playlist EVENT **vacía**, lanza `levelEmptyError`
con el mismo resultado. Ambos casos probados y descartados.

Mientras la escena 1 se genera verás `bufferStalledError` / `bufferNudgeOnStall` en la
consola: son **no fatales**, hls.js se recupera solo y sigue reproduciendo cuando entran
los siguientes segmentos. No los trates como error en la UI.

```
404 → el job no existe, o pasaron 6 s sin primer segmento (reintenta cada ~700 ms)
```

### `GET /stream/{job_id}/seg/{name}`
Un segmento. `Content-Type: video/mp2t` (`.ts`) o `video/mp4` (`.mp4`/`.m4s`).
Soporta `Range` (206). Si el objeto aún no existe en B2 el servidor reintenta
internamente hasta ~2 s antes de devolver 404.

> **Compat:** `GET /stream/{job_id}` (sin sufijo) redirige 307 a `/stream/{job_id}/index.m3u8`.

### `POST /api/chaos`
```jsonc
req  → { "provider": "gmicloud", "dead": true }   // dead opcional, default true (toggle si se omite)
200  → { "provider": "gmicloud", "dead": true }
```
Marca un proveedor como muerto en la DB. `pipeline/chaos.py:is_dead(name)` lo lee.

### `GET /api/verify/{job_id}`
Corre `genblaze verify` server-side sobre el final aprobado.
```jsonc
200 → { "ok": true, "verified": true, "output": "...texto crudo del CLI...", "exit_code": 0 }
409 → { "error": "job not approved" }
```

### `GET /api/jobs/{id}/manifest`
Devuelve el `provenance/{id}/manifest.json` tal cual (JSON), leído de B2.
```jsonc
200 → { ...manifest... }
404 → { "error": "no manifest yet" }
```

### `GET /api/download/{job_id}`
```jsonc
200 → { "url": "https://s3.eu-central-003.backblazeb2.com/genblaze-review-.../approved/...?X-Amz-..." , "expires_in": 3600 }
404 → { "error": "no such job" }
409 → { "error": "job not approved", "job_status": "in_review" }
```
El presign es una firma **local** (0 transacciones de B2) y por eso no se comprueba con
`head_object`: con la cuota agotada eso hacia que un job aprobado contestara
"not approved yet". La fuente de verdad del estado es la DB.
Presign **AWS4 path-style** (obligatorio: los buckets privados de B2 en navegador fallan
con virtual-host style, SDK issue #246).

### `POST /webhooks/b2`
Receptor de B2 Event Notifications. No lo llama el frontend.
- Header `X-Bz-Event-Notification-Signature: v1=<64 hex>` = HMAC-SHA256 del **cuerpo crudo**
  con `B2_WEBHOOK_SECRET`.
- Responde `200 {"ok":true,"queued":N}` en <3 s (ack inmediato, worker aparte).
- `401` si la firma no valida. Idempotente por `eventId`.

### `GET /api/health`
```jsonc
200 → {
  "ok": true, "mode": "mock", "events_mode": "both", "stub": false, "b2": true, "jobs": 3,
  "b2_capped": false,
  "degraded": null,
  "warning": null,                       // string si b2_capped: enséñalo como aviso en la UI
  "b2_transactions": {                   // consumo desde el arranque
    "total": 18,
    "by_op": {"put_object": 15, "get_object_retention": 2, "list_objects_v2": 1},
    "cache_hits": 9,
    "capped": false, "cap_hit_ever": false, "cap_at": null, "cap_detail": null,
    "calls_skipped_by_cap": 0, "cap_retry_in_s": 0
  },
  "poller": {"running": true, "ticks": 12, "listings": 12,
             "active_interval_s": 10, "idle_interval_s": 60},
  "hls_served_from": "auto (disco local primero)"
}
```
**Para W3:** si `degraded` es `true`, pinta un banner con `warning` (la cuenta de B2 se
quedó sin cuota diaria de transacciones). La app sigue entera: reproducción, approve,
manifest y verify funcionan desde disco local; lo único que falta es el badge de lock
hasta que la cuota vuelva. `b2_transactions.total` es un número bonito para el dashboard.

### `POST /api/health/reset-b2-stats`
Pone el contador a cero y sale del enfriamiento del cap. Para medir un job limpio.

---

## Interfaz interna backend ↔ pipeline (W1 ↔ W2) — CONGELADA

El backend importa el runner de forma **perezosa y tolerante**: si `pipeline.runner` no
existe todavía, cae a un modo stub que genera clips con `ffmpeg testsrc2`. W1 no bloquea a W2.

```python
# pipeline/runner.py
def run_job(job_id: str, brief: str, *, scenes: int = 6, on_scene=None, on_event=None) -> dict:
    """Genera el spot escena a escena. SÍNCRONA (el backend la corre en un thread).

    on_scene(scene_no: int, mp4_path: str, meta: dict) -> None
        Se llama en cuanto una escena tiene su mp4 final en disco local, con los
        parámetros comunes de §2 del plan. El backend se lo pasa al assembler.
        meta puede traer {"ms": int, "title": str, "b2_key": str}.

    on_event(kind: str, payload: dict) -> None
        Telemetría opcional para la UI: kind ∈ {"provider_call","provider_failover",
        "judge_score","retry"}. payload libre; los campos de ProviderEvent se guardan.

    Devuelve {"scenes": [...], "manifest_key": "provenance/{job}/manifest.json"} o similar;
    el backend solo usa las claves que existan.
    """
```

Y a la inversa, lo que W1 puede llamar del backend:

```python
# server/assembler.py
def feed(job_id: str, scene_path: str, *, scene_no: int | None = None) -> dict:
    """Transcodifica el mp4 de una escena a los params comunes, lo segmenta en HLS
    y sube cada segmento a incoming/{job}/seg/, regenerando incoming/{job}/index.m3u8
    en B2 tras cada segmento. Idempotente por (job_id, scene_no). Síncrona.
    Devuelve {"segments": n, "duration": secs, "playlist_key": "..."}"""

def finish(job_id: str) -> None:
    """Cierra la playlist: añade #EXT-X-ENDLIST y la sube por última vez."""
```

## Parámetros de encode comunes (§2 del plan) — los cumple todo mp4 que entre al assembler
```
-c:v libx264 -s 1280x720 -r 24 -g 48 -pix_fmt yuv420p -profile:v main
-c:a aac -ar 48000 -ac 2   (silencio si la escena no trae audio)
```
Segmentación: `-f hls -hls_time 4 -hls_list_size 0 -hls_flags append_list+independent_segments`.

## Variables de entorno
| Var | Default | Qué hace |
|---|---|---|
| `DEMO_MODE` | `mock` | `mock` usa mocks/ffmpeg; cualquier otro valor intenta providers reales |
| `EVENTS_MODE` | `both` | `webhook` \| `poll` \| `both` \| **`off`** (ni poller ni webhook tocan B2: modo de emergencia si se agota la cuota) |
| `B2_POLL_ACTIVE_S` | `10` | intervalo del poller **con un job vivo** (1 prefijo por tick) |
| `B2_POLL_IDLE_S` | `60` | intervalo del poller en reposo |
| `B2_CAP_RETRY_S` | `300` | enfriamiento tras un `Transaction cap exceeded` |
| `B2_PLAYLIST_UPLOAD_EVERY_S` | `5` | cada cuánto se re-sube el m3u8 a B2 (local siempre al día) |
| `HLS_SERVE_FROM` | `auto` | `local` prohíbe leer segmentos de B2 aunque falten en disco |
| `STUB` | `0` | `1` = todos los endpoints devuelven datos fake |
| `B2_KEY_ID`, `B2_APP_KEY`, `B2_REGION`, `B2_BUCKET` | — | credenciales (de `.env`) |
| `B2_WEBHOOK_SECRET` | `firstframe-dev-secret` | secreto HMAC de las Event Notifications |
| `FIRSTFRAME_DB` | `data/firstframe.db` | ruta de la sqlite |
| `FIRSTFRAME_HLS` | `data/hls` | copia local de los segmentos (fallback del streamer) |
| `FIRSTFRAME_WORK` | `data/work` | mp4 de escena, final.mp4, manifest local |
| `HLS_SEG_SECONDS` | `2` | duración del segmento HLS (= suelo del "first frame") |
| `SCENE_COUNT` | `6` | escenas por defecto si el POST no lo dice |
| `MAX_ITERATIONS` | `1` | iteraciones del AgentLoop por escena (acota la latencia) |
| `JUDGE_THRESHOLD` | *(sin poner)* | `0` apaga el juez de visión. **Ponerlo a 0 para la demo en vivo**: el juez de NIM free tier timeoutea a los ~30 s por escena y con él el first frame se va a 70 s |
| `FORCE_STUB_RUNNER` | `0` | `1` ignora `pipeline.runner` y genera escenas con ffmpeg |
| `PORT` | `8000` | puerto de uvicorn |

## Autocomprobaciones (cada pieza trae la suya)
```bash
.venv/bin/python -m server.db          # esquema + idempotencia por event_id
.venv/bin/python -m server.events      # firma HMAC (6 casos malos) + 200 webhooks < 3 s
.venv/bin/python -m server.assembler   # 2 escenas -> playlist creciente -> ENDLIST -> master
.venv/bin/python -m server.streamer    # rangos, path traversal, 404-retry
set -a && . ./.env && set +a
.venv/bin/python -m server.b2          # put/get/range/presign/lock + B2 rechaza el borrado
.venv/bin/python -m server.jobs        # end-to-end: crear -> escenas -> approve -> lock -> verify
```
