# GUION del vídeo — FirstFrame (3:00)

Guion **ejecutable**: cada tramo dice qué se ve, qué se dice, qué se pulsa y cuánto dura.
Nada de improvisar delante de la cámara.

Todos los números de aquí están **medidos hoy (2026-08-03)** con la configuración de
`demo/run_demo.sh` (`DEMO_MODE=mock JUDGE_THRESHOLD=0 EVENTS_MODE=off HLS_SERVE_FROM=local`),
no estimados. Si al ensayar te salen otros, manda el que salga: los números en pantalla los
pinta la app sola, no los pones tú.

> Lo que **NO** se graba hoy y por qué está al final, en «Fuera del guion». Léelo **antes**
> de grabar: hay un plano (el borrado rechazado en la consola de B2) que no existe.

---

## 0. Antes de darle a REC (5 minutos, fuera de cámara)

```bash
cd ~/Escritorio/Projects/genblaze-hackathon
bash demo/run_demo.sh --reset        # ~50 s: limpia, arranca y siembra 2 jobs
```

Comprueba, en este orden:

1. La consola termina en `[run] ABRE http://localhost:8000` **sin** la línea
   `AVISO: B2 sin cuota` → si aparece ese aviso, el badge de Object Lock del tramo 5
   **no va a salir**. Ver «Fuera del guion».
2. `[seed] ... aprobado — lock={...}` con un `retain_until` de verdad. Si dice
   `PENDIENTE`, repite `bash demo/run_demo.sh --reset` dentro de unos minutos.
3. Abre `http://localhost:8000`: dos jobs en la columna izquierda, uno `APPROVED` y uno
   `IN REVIEW`, y el player reproduciendo. Consola del navegador **sin errores**.
4. Ventana del navegador a **1440×900** y sin barra de marcadores. El zoom al 100 %.
5. Ten preparado en un segundo escritorio: `PLAN.md §3` (layout del bucket) o el diagrama
   de arquitectura para el tramo 6.

Cronómetro de referencia: el recorrido completo, medido con navegador headless, tarda
**~2 min 5 s de acción real**. El resto del minuto es narración sobre planos fijos. Cabe
holgadamente en 3:00.

---

## 1 · 0:00 – 0:18 — El problema (18 s)

| | |
|---|---|
| **En pantalla** | Pantalla partida. Izquierda: terminal con una barra de render al 40 % y un cronómetro subiendo. Derecha: tarjeta «Ana Ruiz · productora · 40 spots/semana». Rótulo grande: **«3–5 min por render · 30 % se rechazan · el rechazo llega al final»**. |
| **Se dice** | «Ana genera cuarenta spots a la semana con IA. Rechaza uno de cada tres. El problema no es generar: es que para rechazar en el segundo diez tiene que esperar tres minutos a que termine el render entero.» |
| **Cómo se provoca** | Plano montado, no es la app. Puedes usar cualquier terminal; no hace falta que sea real. |
| **Duración** | 18 s |

---

## 2 · 0:18 – 0:52 — Primer fotograma (34 s)

| | |
|---|---|
| **En pantalla** | La app en la URL viva, **con la barra de direcciones visible**. Pegas el brief, eliges 4 escenas, pulsas `New spot`. Aparece el job con badge rojo **`LIVE — generando escena 2 de 4`**, el player arranca con la cabecera `FirstFrame · LIVE`, y a los ~5 s entra la escena 1. El **FEED EN VIVO** de la derecha se llena de `SEGMENT_LANDED · b2:ObjectCreated`. Abajo, el panel grande: **`PRIMER FOTOGRAMA 5.1 s` vs `RENDER TOTAL 24 s` · `4.7× ANTES EN PANTALLA`**. |
| **Se dice** | «Pego el brief y le doy a New spot. Cada escena que se genera se transcodifica, se corta en segmentos HLS y **cada segmento se sube a Backblaze B2 como un objeto independiente**, regenerando la playlist. A los cinco segundos Ana ya está viendo el segundo cero mientras la escena cuatro todavía se está generando. Cinco segundos contra veinticuatro: cuatro veces y media antes en pantalla.» |
| **Comandos / clics** | 1) clic en el campo Brief, pegar: `spot de 15 s para la zapatilla Aeron: amanecer en una playa vacía, plano detalle del logo, cierre con claim`. 2) desplegable **ESCENAS → 4**. 3) clic en **New spot**. 4) no toques nada más: deja correr. |
| **Tiempos reales** | playlist servible a **~1,0 s** del clic · primer fotograma en pantalla a **~4,4 s** del clic (el contador de la app marca `ff 5,1 s`, que cuenta desde la creación del job) · render de 4 escenas completo a **~22 s** del clic. |
| **Duración** | 34 s — encaja justo. Si te sobra render, **puedes cortar en el montaje**: la barra de progreso llegando al final es un buen punto de corte. |

> El primer job después de arrancar el servidor sale ~2 s más lento (imports en frío):
> `ff 7,3 s`. Por eso `run_demo.sh` siembra dos jobs antes: cuando grabas, el intérprete
> ya está caliente.

---

## 3 · 0:52 – 1:20 — Rechazo en caliente + AgentLoop (28 s)

| | |
|---|---|
| **En pantalla** | Escribes la nota de revisión y pulsas **Reject**. El badge cambia a **`LIVE — refinando la toma rechazada`**. El panel **AGENTLOOP** de la derecha se llena en tiempo real: `Rechazo de la productora: "el logo queda ilegible en el plano detalle"` → `AgentLoop: juez de visión → prompt refinado → escena relanzada` → `score 0.42` → `toma a rejected/ · AgentLoop relanza la escena`. El FEED marca `REJECTED` y `JUDGE_SCORE`. A los ~8 s aparece una **ESCENA 5 · «Escena 4 — toma refinada 1»** y el job vuelve a `IN REVIEW`. |
| **Se dice** | «Rechazo en el segundo quince: “el logo queda ilegible en el plano detalle”. Esa nota entra literal en el prompt de la nueva pasada del AgentLoop. La toma mala baja a `rejected/` en el bucket, el run nuevo cuelga del anterior por `parent_run_id` —el manifest guarda la cadena entera— y **la toma refinada se añade a la misma playlist**, así que entra en vivo sin recargar nada.» |
| **Comandos / clics** | 1) clic en el campo **Nota de revisión**, escribir `el logo queda ilegible en el plano detalle`. 2) desplegable **ESCENA → escena 2** (opcional; si lo dejas en «última» refina la última). 3) clic en **Reject**. |
| **Tiempos reales** | panel AgentLoop lleno a **~3 s** del clic · toma refinada en la playlist y job en `in_review` a **~8,4 s**. |
| **Duración** | 28 s |

> **Honestidad en la voz en off:** con la configuración de la demo (`JUDGE_THRESHOLD=0`)
> el `0.42` que se ve es **la puntuación del rechazo de la productora**, no la del modelo
> de visión. Di «el AgentLoop relanza la escena con la nota del revisor», no «el juez ha
> puntuado 0.42». Si quieres el juez de visión real en cámara, ver el plano opcional 5-bis.

---

## 4 · 1:20 – 1:44 — Failover en cámara (24 s)

| | |
|---|---|
| **En pantalla** | Pulsas `k`. Se abre el modal **CHAOS — MATAR UN PROVEEDOR EN DIRECTO** con cuatro proveedores. Matas `gmicloud`. Cierras y lanzas otro spot. A los ~6 s salta el toast: **`FAILOVER DE PROVEEDOR · GMICLOUD · ESCENA 1` / `pixverse-v5.6 MODEL_ERROR → fallback: seedance-2-0`**, y el panel AGENTLOOP lo registra escena a escena. El render **no se detiene**. Rótulo: **«0 acciones humanas para recuperarse»**. |
| **Se dice** | «Mato el proveedor de vídeo en directo. Genblaze lanza un `MODEL_ERROR` de verdad —no un timeout, que es lo único que dispara `fallback_models`— y el pipeline salta solo de `pixverse-v5.6` a `seedance-2-0`. Cero acciones humanas.» |
| **Comandos / clics** | 1) tecla `k`. 2) clic en **Matar** en la fila `gmicloud`. 3) `Esc`. 4) pegar un brief corto (`spot 15 s zapatilla Aeron, contraluz de amanecer`) y clic en **New spot**. |
| **Tiempos reales** | modal abierto a **0,8 s** · proveedor muerto a **3 s** · toast de failover en pantalla a **~14 s** del inicio del tramo. |
| **Después** | **Revive el proveedor** antes de seguir: `k` → **Revivir** en `gmicloud`, o `curl -s -X POST localhost:8000/api/chaos -H 'content-type: application/json' -d '{"provider":"gmicloud","dead":false}'`. Si no, el job del tramo 5 también saldrá con failovers. |
| **Duración** | 24 s |

---

## 5 · 1:44 – 2:18 — Approve, Object Lock y provenance (34 s)

| | |
|---|---|
| **En pantalla** | Vuelves al job del tramo 3 y pulsas **Approve**. Toast verde: **`APROBADO · OBJECT LOCK GOVERNANCE` — `approved/{job}/final.mp4` retención hasta 2026-09-02`**, banda verde bajo los botones **`Object Lock GOVERNANCE · 30 días`**, y en el panel **PROVENANCE** la fila `lock GOVERNANCE hasta …`. Después pulsas **Verify** y el panel imprime el resultado del CLI: `✓ MANIFEST VERIFICADO (exit 0)`, `manifest: embebido en la caja uuid de genblaze`, `schema_version 1.5`, `canonical_hash …`, `hash_ok True`. |
| **Se dice** | «Apruebo. El máster se concatena, se le **embebe el manifest de provenance dentro del propio MP4** —en la caja `uuid` de Genblaze— y se sube a `approved/` con **Object Lock GOVERNANCE a treinta días**. A partir de aquí ni una regla de lifecycle puede borrar esa versión. Y `genblaze verify` lo comprueba: hash canónico correcto, manifest embebido, entregable auditable.» |
| **Comandos / clics** | 1) clic en el job del tramo 3 en la lista. 2) clic en **Approve**. 3) esperar el toast (~7 s). 4) clic en **Verify** (arriba a la derecha del panel PROVENANCE). |
| **Tiempos reales** | approve completo (concat + embed + subida con lock) **~6,6 s** · verify: clic → panel relleno **~6 s** (di la frase del manifest mientras corre, no dejes silencio). |
| **Duración** | 34 s |

> **Este es el tramo frágil.** El badge de Object Lock necesita una lectura
> (`get_object_retention`, Class B) y la cuenta tiene el tope diario de transacciones
> tocado. Mira el indicador del header: si pone **`B2 OK`**, adelante; si pone
> **`B2 CAPPED`**, el badge puede no salir. Antes de esta toma:
> `curl -s -X POST localhost:8000/api/health/reset-b2-stats` y aprueba **inmediatamente
> después**. Si el badge no sale, la toma no vale: repítela. Verificado hoy: sale.

### 5-bis · plano opcional del juez de visión (solo si sobra tiempo, +12 s)

El juez de visión real (`meta/llama-3.2-90b-vision-instruct` en NVIDIA NIM) **tarda 48,7 s
por llamada** medidos, así que no cabe en el flujo en vivo. Si lo quieres en cámara, grábalo
aparte y móntalo como inserto:

```bash
JUDGE_THRESHOLD_REJECT=0.7 REFINE_MAX_ITERATIONS=1 bash demo/run_demo.sh --no-seed
# y en la app: Reject sobre un job en revisión -> el AgentLoop llama a NIM de verdad
```

Plano recomendado: la terminal con el log del servidor mientras devuelve el veredicto
(`score` + `reason` en texto). Advertencia: con los keyframes de `DEMO_MODE=mock` el juez
responde cosas como *«No running shoe or logo in frame»* y puntúa 0.0 — es **correcto** (está
juzgando un placeholder sintético), pero en cámara se lee mal. Úsalo solo si narras que el
modo mock no genera producto real.

---

## 6 · 2:18 – 3:00 — Arquitectura y cierre (42 s)

| | |
|---|---|
| **En pantalla** | Un solo diagrama, estático. Bloques: `navegador → FastAPI (REST + SSE + HLS) → B2` y `FastAPI → Genblaze pipeline`. Sobreimpresiones que van apareciendo mientras las nombras. Últimos 5 s: la URL viva a pantalla completa. |
| **Se dice** | «Todo esto se apoya en dos cosas. **B2**: HLS incremental servido desde el bucket segmento a segmento, Event Notifications firmadas con HMAC, Object Lock GOVERNANCE que las reglas de lifecycle no pueden tocar, `daysFromStartingToCancelingUnfinishedLargeFiles` para que un render muerto no deje un multipart huérfano, y application keys de solo lectura para que el revisor externo nunca tenga una clave que pueda escribir. Y **Genblaze**: un pipeline por escena con `fallback_models`, AgentLoop con Evaluator y juez de visión, fan-in con FFmpeg, `manifest_lock`, `ObjectStorageSink` y `replay`. Además dejamos tres PRs y un issue abiertos en el repo de Genblaze con los bugs que nos encontramos por el camino.» |
| **Cierre** | Rótulo: la URL + **«el primer segundo, en el primer segundo»**. |
| **Comandos / clics** | Ninguno. Plano fijo. |
| **Duración** | 42 s |

---

## Resumen cronometrado

| # | Tramo | Entra | Sale | Dura |
|---|---|---|---|---|
| 1 | Problema | 0:00 | 0:18 | 18 s |
| 2 | Primer fotograma | 0:18 | 0:52 | 34 s |
| 3 | Rechazo + AgentLoop | 0:52 | 1:20 | 28 s |
| 4 | Failover en cámara | 1:20 | 1:44 | 24 s |
| 5 | Approve + provenance | 1:44 | 2:18 | 34 s |
| 6 | Arquitectura + cierre | 2:18 | 3:00 | 42 s |
| | **Total** | | | **3:00** |

Acción real cronometrada en la app (tramos 2–5): **~2 min 5 s**. El resto es narración
sobre plano fijo y se ajusta en el montaje.

---

## Fuera del guion — lo que hoy NO es grabable

1. **El borrado rechazado en la consola web de B2.** El plan original (§10, 1:45–2:20)
   pedía cortar a la consola de B2, intentar borrar `approved/{job}/final.mp4` y enseñar
   el error en cámara. **No se puede**: listar y leer objetos son transacciones Class B/C
   y la cuenta tiene el tope diario agotado — la consola web no llega a pintar el bucket.
   Sustituido por el toast de Object Lock + el panel PROVENANCE + `Verify`, que cuentan lo
   mismo desde dentro de la app. Si el usuario sube el cap de transacciones, este plano
   vuelve a ser grabable tal cual estaba en `PLAN.md §10`.
2. **Live Read.** Descartado en `PLAN.md §0`: no está disponible en la cuenta. No aparece
   en ningún tramo. Si alguien lo menciona en el vídeo, es un error.
3. **Las reglas de lifecycle actuando.** Corren una vez al día; no hay nada que filmar.
   Se nombran en el tramo 6 y se enseñan en el README, como estaba previsto.
4. **Vídeo que parezca un anuncio.** `DEMO_MODE=mock` genera cartas de ajuste de ffmpeg
   con la marca `DRAFT · FirstFrame` y el rótulo de escena. Es honesto (no hay proveedor
   de generación de vídeo disponible, `PLAN.md §0`) pero **no intentes venderlo como
   material final**: enfoca los planos a los paneles, los cronómetros y el feed, no al
   contenido del vídeo. Nunca digas «mira qué spot ha salido».
5. **El juez de visión dentro del flujo en vivo.** 48,7 s por llamada medidos. Va como
   inserto (5-bis) o no va.
6. **El badge de Object Lock, si `/api/health` dice `b2_capped: true`.** Intermitente hoy.
   Instrucciones para maximizar la probabilidad en el tramo 5.

---

## Si algo se cae en mitad de la grabación

| Síntoma | Qué hacer |
|---|---|
| El player se queda negro y no arranca | Es `bufferStalledError` de hls.js, **no fatal**: se recupera solo cuando entra el siguiente segmento. Espera 3 s antes de cortar. |
| Un job se queda en `rendering` para siempre | `tail -f /tmp/firstframe-demo.log`. Si hay traza, reinicia con `bash demo/run_demo.sh --no-seed`; los jobs anteriores se conservan. |
| La sala se llena de jobs de las tomas falsas | `.venv/bin/python demo/seed.py` (purga fallidos, títulos de prueba y la cola sobrante) o `--reset` para empezar de cero. |
| Aparece el banner rojo «BACKEND DEGRADADO» | La cuota de B2 se agotó a mitad. La app sigue entera desde disco local; lo único que se pierde es el badge de lock. Encuadra evitando la esquina superior derecha del player o repite la toma más tarde. |
| El toast de failover no sale | El proveedor no está muerto de verdad: comprueba `cat data/chaos.json` → debe poner `{"gmicloud": true}`. |
