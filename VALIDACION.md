# Validación contra servicios reales (2026-08-02)

Todo lo de abajo está probado ejecutando código contra las APIs reales, no leído en docs.

## B2 — cuenta `ffc72b988bce`, región `eu-central-003`

| Cosa | Estado | Detalle |
|---|---|---|
| Auth S3 + nativa | ✅ | endpoint `https://s3.eu-central-003.backblazeb2.com` |
| Bucket con Object Lock | ✅ | `genblaze-review-migarci2`, `isFileLockEnabled=true` |
| Bucket scratch | ✅ | `genblaze-scratch-migarci2`, sin lock |
| Multipart upload normal | ✅ | 2 partes de 5 MiB, completado y verificado |
| **Live Read** | ❌ **NO-GO** | Ver abajo |
| Event Notifications | ✅ | key trae `writeBucketNotifications` + `readBucketNotifications` |
| Object Lock / legal hold | ✅ | `writeFileRetentions`, `writeFileLegalHolds`, `writeBucketRetentions` |
| Lifecycle rules | ✅ | `writeBucketLifecycleRules` |
| App keys restringidas | ✅ | `writeKeys` ⇒ multi-tenancy real por prefijo es ejecutable |

### Live Read: NO-GO (detalle)
Con multipart abierto y parte 1 de 5 MiB ya subida, `GetObject` con `Range` devuelve
**`404 NoSuchKey`**, no el `416 Range Not Satisfiable` que define la API cuando Live Read
está activo. Probado con el header `x-backblaze-live-read-enabled: true` inyectado:
- en `before-send` (fuera de la firma SigV4)
- en `before-sign` (dentro de la firma)
- sobre `CreateMultipartUpload`, `UploadPart` y `GetObject`

Cuenta free; Live Read se factura a $15/TB de capacidad de subida ⇒ gated.
**Consecuencia:** preview progresivo por segmentos (HLS incremental servido desde B2)
pasa a ser la arquitectura principal. Reproducir con `scripts/probe_liveread.py`.

## NVIDIA NIM — key free

| Cosa | Estado | Detalle |
|---|---|---|
| Auth | ✅ | `integrate.api.nvidia.com/v1/models` → 200, 102 modelos |
| Chat / LLM | ✅ | catálogo completo de texto |
| **Visión (juez del AgentLoop)** | ✅ | `meta/llama-3.2-90b-vision-instruct` acierta con imagen real |
| **Generación de imagen** | ❌ | `ai.api.nvidia.com/v1/genai/...` **cuelga sin responder** (timeout 60s+). El host resuelve (404 en la raíz) pero el endpoint de generación no devuelve. La key free no parece dar acceso a `genai`. |
| Generación de vídeo | ❌ sin probar | `cosmos-*` está documentado como enterprise-gated |

### Formato del juez de visión (importante)
El estilo `<img src="data:...">` inline **da respuestas incorrectas**
(`nemotron-nano-12b-v2-vl` dijo "orange" y luego "grey" ante un PNG rojo puro `(253,0,0)`).
Hay que usar el array de contenido estilo OpenAI:
```json
{"role":"user","content":[
  {"type":"text","text":"..."},
  {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]}
```
Con ese formato, `meta/llama-3.2-90b-vision-instruct` responde `"Red."` correctamente.
`nemotron-nano-12b-v2-vl` sigue fallando ("orange") ⇒ **usar el de 90b para el juez**.

## Genblaze 0.4.5

- `Pipeline.__init__` tiene **`preflight: bool = True` por defecto**, y el preflight usa
  `validate_model()`, que está **invertido** (issue #248). ⇒ **construir siempre con
  `Pipeline(..., preflight=False)`**. El default del SDK activa una comprobación que miente.
- `Pipeline.run()` no acepta `cache=`; la caché es fluida.
- `PipelineResult` **no tiene `.steps`** (mi suposición al escribir el probe fue falsa;
  introspeccionar antes de usar).
- `run()` avisa: en genblaze-core 0.4.0 lanzará `PipelineError` ante fallo de step;
  hoy hay que pasar `raise_on_failure=True` para ese comportamiento.
- **`fallback_models` NO cubre timeouts de transporte.** Un `read operation timed out`
  no es `MODEL_ERROR`, así que el failover no salta. Si queremos enseñar failover en cámara
  hay que provocar un `MODEL_ERROR` de verdad (el `ChaosWrapper` del plan lo hace bien),
  y además envolver los timeouts por nuestra cuenta si queremos resiliencia real.

## Bloqueante actual

**No hay ningún proveedor de generación de media funcionando.** NIM da chat y visión gratis,
pero no imagen. Hace falta una de:
- `OPENAI_API_KEY` → `dall-e-2` ($0.02/img), `gpt-image-*`, `tts-1` para voz
- `GMI_API_KEY` → imagen barata (`bria-*` $0.007, `seedream-5.0-lite`) y el vídeo más barato
  (`pixverse-v5.6`, $0.03/asset). **Nunca para audio: roto entero, issue #251.**
- `GEMINI_API_KEY` → `imagen-3.0-fast-generate-001` ($0.02)

Mientras tanto **la construcción NO está bloqueada**: el plan usa `DEMO_MODE=mock`
(`MockProvider`/`MockVideoProvider` desde `genblaze_core`, más clips `ffmpeg testsrc2`)
como camino por defecto de todo el desarrollo.
