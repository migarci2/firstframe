# GUION del vídeo — FirstFrame (3:00)

Guion **ejecutable**: cada tramo dice qué se ve, qué se dice, qué se pulsa y cuánto dura.
**La narración va en inglés** (jurado de Backblaze, submission en inglés); las acotaciones
siguen en español porque son para ti, no para la cámara.
Nada de improvisar delante de la cámara.

> **ACTUALIZADO 2026-08-03 18:55.** Se graba contra **producción**, no contra localhost:
> `https://firstframe.migarci2.dev`, código de acceso **`FIRSTFRAME`**. Ahí el pipeline de
> Genblaze corre de verdad (en local `run_demo.sh` fuerza mock y sale carta de ajuste), los
> datos están en inglés y la barra de direcciones juega a favor. La app tiene ahora muro de
> acceso, vista de proyectos y paneles con nombre (`PROJECT` / `MONITOR` / `TIMELINE` /
> `INSPECTOR`): los tramos de abajo que hablen de la disposición vieja hay que releerlos
> mirando la pantalla, no de memoria.

Todos los números de aquí están **medidos** con la app real,
no estimados. Si al ensayar te salen otros, manda el que salga: los números en pantalla los
pinta la app sola, no los pones tú.

> Lo que **NO** se graba hoy y por qué está al final, en «Fuera del guion». Léelo **antes**
> de grabar: hay un plano (el borrado rechazado en la consola de B2) que no existe.

---

## 0. Antes de darle a REC (5 minutos, fuera de cámara)

Se graba contra producción, así que no hay que levantar nada. Comprueba:

```bash
curl -s https://firstframe.migarci2.dev/api/health   # ok:true, mode:free, b2:true
```

Y en el navegador, **en una ventana de incógnito** (para pasar por el muro como un juez):

1. Abre `https://firstframe.migarci2.dev` → sale la pantalla de acceso.
2. Código **`FIRSTFRAME`** → aterrizas en la **vista de proyectos**, con
   `Aeron SS26` y `Nova Q3` y un spot ya aprobado.
3. Abre un proyecto → el editor con los cuatro paneles y el vídeo reproduciendo.

Comprueba además, en este orden:

1. El spot aprobado enseña el badge de **Object Lock GOVERNANCE** con su fecha. Si no
   está, aprueba otro spot antes de grabar: es el momento fuerte del tramo 5.
2. Consola del navegador **sin errores**.
3. Ventana a **1440×900** y sin barra de marcadores. Zoom al 100 %.
4. Cierra el panel técnico (**Hide panel**) para los planos donde manda el vídeo, y ábrelo
   sólo en el tramo 5, que es donde la evidencia es el argumento.
5. Ten preparado en un segundo escritorio: `PLAN.md §3` (layout del bucket) o el diagrama
   de arquitectura para el tramo 6.

Cronómetro de referencia: el recorrido completo (tramos 2–5), medido dos veces con
navegador headless, tarda **1 min 40 s – 2 min 10 s de acción real**. El resto es
narración sobre planos fijos. Cabe en 3:00 con margen.

La variación entre las dos pasadas viene del render: 4 escenas tardan **22 s con la
máquina descansada y 36 s con ella cargada**. Antes de grabar, cierra lo que no
necesites (navegadores, IDE, agentes) y haz una pasada en vacío para ver en cuál de los
dos extremos estás.

---

## 1 · 0:00 – 0:18 — El problema (18 s)

| | |
|---|---|
| **En pantalla** | Pantalla partida. Izquierda: terminal con una barra de render al 40 % y un cronómetro subiendo. Derecha: tarjeta «Ana Ruiz · productora · 40 spots/semana». Rótulo grande: **«3–5 min por render · 30 % se rechazan · el rechazo llega al final»**. |
| **Se dice (EN)** | «Ana ships forty AI-generated product spots a week. She rejects about a third of them. Generating isn't the problem — the problem is that to reject a shot at second ten, she has to wait three minutes for the whole render to finish.» |
| **Cómo se provoca** | Plano montado, no es la app. Puedes usar cualquier terminal; no hace falta que sea real. |
| **Duración** | 18 s |

---

## 2 · 0:18 – 0:52 — Primer fotograma (34 s)

| | |
|---|---|
| **En pantalla** | La app en la URL viva, **con la barra de direcciones visible**. Pegas el brief, eliges 4 escenas, pulsas `New spot`. Aparece el job con badge rojo **`LIVE — generando escena 2 de 4`**, el player arranca con la cabecera `FirstFrame · LIVE`, y a los ~5 s entra la escena 1. El **FEED EN VIVO** de la derecha se llena de `SEGMENT_LANDED · b2:ObjectCreated`. Abajo, el panel grande: **`PRIMER FOTOGRAMA 5.1 s` vs `RENDER TOTAL 24 s` · `4.7× ANTES EN PANTALLA`**. |
| **Se dice (EN)** | «I paste the brief and hit New spot. Every scene that finishes is transcoded, cut into HLS segments, and **each segment lands in Backblaze B2 as its own object**, with the playlist regenerated behind it. Within seconds she's watching second zero while the last scene is still being generated. That's roughly four times earlier on screen than waiting for the render.» |
| **Comandos / clics** | 1) clic en el campo Brief, pegar: `spot de 15 s para la zapatilla Aeron: amanecer en una playa vacía, plano detalle del logo, cierre con claim`. 2) desplegable **ESCENAS → 4**. 3) clic en **New spot**. 4) no toques nada más: deja correr. |
| **Tiempos reales** | playlist servible a **~1,0 s** del clic · primer fotograma en pantalla a **4,4–5,3 s** del clic (el contador de la app marca `ff 5,1–6,1 s`, que cuenta desde la creación del job) · render de 4 escenas completo a **22–36 s** del clic según carga. |
| **Duración** | 34 s. Si el render se te va a 36 s no cabe entero: **corta en el montaje** cuando la barra de progreso pase de la mitad y entra directo al tramo 3. El número final (`RENDER TOTAL`) se ve igualmente en el tramo 3. |

> El primer job después de arrancar el servidor sale ~2 s más lento (imports en frío):
> `ff 7,3 s`. Por eso `run_demo.sh` siembra dos jobs antes: cuando grabas, el intérprete
> ya está caliente.

---

## 3 · 0:52 – 1:20 — Rechazo en caliente + AgentLoop (28 s)

| | |
|---|---|
| **En pantalla** | Escribes la nota de revisión y pulsas **Reject**. El badge cambia a **`LIVE — refinando la toma rechazada`**. El panel **AGENTLOOP** de la derecha se llena en tiempo real: `Rechazo de la productora: "el logo queda ilegible en el plano detalle"` → `AgentLoop: juez de visión → prompt refinado → escena relanzada` → `score 0.42` → `toma a rejected/ · AgentLoop relanza la escena`. El FEED marca `REJECTED` y `JUDGE_SCORE`. A los ~8 s aparece una **ESCENA 5 · «Escena 4 — toma refinada 1»** y el job vuelve a `IN REVIEW`. |
| **Se dice (EN)** | «I reject at second fifteen: “the logo is unreadable in the detail shot.” That note goes straight into the prompt for the AgentLoop's next pass. The rejected take drops into `rejected/` in the bucket, the new run hangs off the previous one through `parent_run_id` — the manifest keeps the whole chain — and **the refined take joins the same playlist**, so it arrives live with no reload and no second link.» |
| **Comandos / clics** | 1) clic en el campo **Nota de revisión**, escribir `el logo queda ilegible en el plano detalle`. 2) desplegable **ESCENA → escena 2** (opcional; si lo dejas en «última» refina la última). 3) clic en **Reject**. |
| **Tiempos reales** | panel AgentLoop lleno a **~3 s** del clic · toma refinada en la playlist y job en `in_review` a **8,4–12,9 s**. |
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
| **Se dice (EN)** | «I kill the video provider live. Genblaze raises a real `MODEL_ERROR` — not a timeout, which is the only thing `fallback_models` reacts to — and the pipeline falls over from `pixverse-v5.6` to `seedance-2-0` on its own. Zero human actions.» |
| **Comandos / clics** | 1) tecla `k`. 2) clic en **Matar** en la fila `gmicloud`. 3) `Esc`. 4) pegar un brief corto (`spot 15 s zapatilla Aeron, contraluz de amanecer`) y clic en **New spot**. |
| **Tiempos reales** | modal abierto a **0,8 s** · proveedor muerto a **3 s** · toast de failover en pantalla a **~14 s** del inicio del tramo. |
| **Después** | **Revive el proveedor** antes de seguir: `k` → **Revivir** en `gmicloud`, o `curl -s -X POST localhost:8000/api/chaos -H 'content-type: application/json' -d '{"provider":"gmicloud","dead":false}'`. Si no, el job del tramo 5 también saldrá con failovers. |
| **Duración** | 24 s |

---

## 5 · 1:44 – 2:18 — Approve, Object Lock y provenance (34 s)

| | |
|---|---|
| **En pantalla** | Vuelves al job del tramo 3 y pulsas **Approve**. Toast verde: **`APROBADO · OBJECT LOCK GOVERNANCE` — `approved/{job}/final.mp4` retención hasta 2026-09-02`**, banda verde bajo los botones **`Object Lock GOVERNANCE · 30 días`**, y en el panel **PROVENANCE** la fila `lock GOVERNANCE hasta …`. Después pulsas **Verify** y el panel imprime el resultado del CLI: `✓ MANIFEST VERIFICADO (exit 0)`, `manifest: embebido en la caja uuid de genblaze`, `schema_version 1.5`, `canonical_hash …`, `hash_ok True`. |
| **Se dice (EN)** | «I approve. The master is concatenated, the **provenance manifest is embedded inside the MP4 itself** — in Genblaze's `uuid` box — and it goes to `approved/` under **Object Lock in GOVERNANCE mode for thirty days**. From here, not even a lifecycle rule can delete that version. And `genblaze verify` proves it: canonical hash checks out, manifest embedded, auditable deliverable.» |
| **Comandos / clics** | 1) clic en el job del tramo 3 en la lista. 2) clic en **Approve**. 3) esperar el toast (~7 s). 4) clic en **Verify** (arriba a la derecha del panel PROVENANCE). |
| **Tiempos reales** | approve completo (concat + embed + subida con Object Lock) **4,8–6,6 s** · verify: clic → panel relleno **6–9 s** (di la frase del manifest mientras corre, no dejes silencio). |
| **Duración** | 34 s |

> **Este es el tramo frágil.** El badge de Object Lock necesita una lectura
> (`get_object_retention`, Class B) y la cuenta tiene el tope diario de transacciones
> tocado: cuando el backend entra en enfriamiento por el cap, ni lo intenta y el badge
> no sale (la subida sí ocurre — las subidas son Class A y funcionan).
> **Receta verificada hoy, funcionó las dos veces:**
> ```bash
> curl -s -X POST localhost:8000/api/health/reset-b2-stats
> ```
> y pulsa **Approve** en los segundos siguientes. Mira el indicador del header: con
> **`B2 OK`** adelante. Si sale `lock — (sin aprobar)` en el panel PROVENANCE, la toma no
> vale: resetea el contador y repite con otro job en revisión.
> (`demo/seed.py` hace este mismo reset antes de aprobar el job sembrado, por eso la sala
> arranca con un `APPROVED` que ya tiene su `retain_until` de verdad.)

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
| **Se dice (EN)** | «All of this rests on two things. **B2**: incremental HLS served from the bucket segment by segment, Event Notifications signed with HMAC, Object Lock in GOVERNANCE mode that lifecycle rules cannot touch, `daysFromStartingToCancelingUnfinishedLargeFiles` so a dead render never leaves an orphaned multipart, and read-only application keys so an outside reviewer never holds a key that can write. And **Genblaze**: a pipeline per scene with `fallback_models`, an AgentLoop with an Evaluator and a real vision judge, FFmpeg fan-in, `manifest_lock`, `ObjectStorageSink` and `replay`. We also left three pull requests and an issue open on the Genblaze repo, for the bugs we hit building this.» |
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

Acción real cronometrada en la app (tramos 2–5), dos ensayos completos: **1:40** con la
máquina descansada, **2:10** con ella cargada. El resto es narración sobre plano fijo y se
ajusta en el montaje. En los dos ensayos la consola del navegador terminó **sin un solo
error**.

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
4. **Vídeo que parezca un anuncio.** `GEN_MODE=mock` genera cartas de ajuste de ffmpeg
   con la marca `DRAFT · FirstFrame` y el rótulo de escena. Es honesto (no hay proveedor
   de generación de vídeo disponible, `PLAN.md §0`) pero **no intentes venderlo como
   material final**: enfoca los planos a los paneles, los cronómetros y el feed, no al
   contenido del vídeo. Nunca digas «mira qué spot ha salido».
   Si quieres **imagen generada de verdad** para un inserto, `bash demo/run_demo.sh --free`
   usa el proveedor gratuito real — pero cuesta **~47 s por escena** y destroza el número
   del primer fotograma. Grábalo aparte, nunca en el tramo 2.
5. **El juez de visión dentro del flujo en vivo.** 48,7 s por llamada medidos. Va como
   inserto (5-bis) o no va.
6. **El badge de Object Lock sin el reset previo del contador.** Con `b2_capped: true` el
   backend ni intenta leer la retención y el badge no aparece, aunque el máster **sí** esté
   subido y bloqueado. Con el reset de `/api/health/reset-b2-stats` justo antes sale — dos
   de dos en los ensayos de hoy. Receta en el tramo 5.

---

## Si algo se cae en mitad de la grabación

| Síntoma | Qué hacer |
|---|---|
| El player se queda negro y no arranca | Es `bufferStalledError` de hls.js, **no fatal**: se recupera solo cuando entra el siguiente segmento. Espera 3 s antes de cortar. |
| Un job se queda en `rendering` para siempre | `tail -f /tmp/firstframe-demo.log`. Si hay traza, reinicia con `bash demo/run_demo.sh --no-seed`; los jobs anteriores se conservan. |
| La sala se llena de jobs de las tomas falsas | `.venv/bin/python demo/seed.py` (purga fallidos, títulos de prueba y la cola sobrante) o `--reset` para empezar de cero. |
| Aparece el banner rojo «BACKEND DEGRADADO» | La cuota de B2 se agotó a mitad. La app sigue entera desde disco local; lo único que se pierde es el badge de lock. Encuadra evitando la esquina superior derecha del player o repite la toma más tarde. |
| El toast de failover no sale | El proveedor no está muerto de verdad: comprueba `cat data/chaos.json` → debe poner `{"gmicloud": true}`. |
