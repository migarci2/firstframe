# FFEditor — el TIMELINE, pero editable

El panel `TIMELINE` pintaba las escenas y nada más. Ahora se puede **reordenar**
arrastrando, **recortar** arrastrando los bordes y **dejar una escena fuera del
montaje** sin borrarla.

Pruébalo sin tocar el resto de la app:

```
web/editor/demo.html          # ábrelo tal cual, no necesita servidor
web/editor/demo.html?job=j_x  # engancha un job real vía /api/jobs/{id}/edl
```

---

## Qué se eligió y por qué

**[SortableJS](https://github.com/SortableJS/Sortable) 1.15.6 — MIT — 44 KB**,
vendorizado en `web/vendor/Sortable.min.js` (licencia en `Sortable.LICENSE`).

Es la única de las candidatas que encajaba en lo que ya tenemos. Se le pasa el
`<div>` que ya existe y le añade el arrastre encima: no exige React, ni
bundler, ni ser dueño del panel. Cero dependencias, UMD, un `<script>` y ya.

Las asas de recorte **no** usan librería: son ~40 líneas de pointer events.

### Lo que se descartó, y por qué

| Candidato | Licencia | Veredicto |
|---|---|---|
| **OpenCut-app/OpenCut** | MIT | No es una librería, es una app Next.js 15 + React + Zustand + Tailwind entera. No publica dist ni paquete npm. Habría que adoptar su stack, no integrarlo. |
| **designcombo/react-video-editor** | **NOASSERTION** | Sin licencia OSS clara — descartado solo por eso en un proyecto que se entrega. Además su UMD (386 KB) no es autocontenido: externaliza dependencias, o sea bundler. |
| **etro-js/etro** | **GPL-3.0** | Sí trae IIFE de navegador (147 KB) y funcionaría sin build, pero la GPL es vírica: meterlo obliga a relicenciar FirstFrame entero. Y encima no resuelve esto — es un motor de composición en canvas, no tiene UI de timeline. |
| **xzdarcy/react-timeline-editor** | MIT | Solo publica ESM con `import react`. React + JSX + bundler. |
| **vis-timeline** | Apache-2.0/MIT | UMD real, pero 423 KB y su modelo de datos son **fechas**, no offsets de media. Doblegarlo cuesta más que escribirlo. |
| **interact.js** | MIT | El mejor descarte. Hace drag *y* resize con una sola API, y `edges:{left,right}` es literalmente el gesto de recorte. Se dejó fuera por dos razones: son **96 KB más** para algo que son 40 líneas de pointer events, y montado junto a Sortable **los dos se pelean por el mismo `pointerdown`** — un arrastre de recorte dispararía también un reorder. Menos código y menos peso hacerlo a mano. |

**Conclusión honesta**: ningún editor de vídeo open source completo se puede
integrar aquí. Todos los que hay son aplicaciones React, no componentes. Lo que
sí encaja es una librería de arrastre buena (Sortable) más la lógica de edición
propia — que es lo que se ha hecho.

---

## Cómo se monta

```html
<link rel="stylesheet" href="editor/ffeditor.css">
<script src="vendor/Sortable.min.js"></script>
<script src="editor/ffeditor.js"></script>
```

```js
var ed = FFEditor.mount(document.getElementById('tl'), {
  jobId:   job.id,
  scenes:  job.scenes,          // [{n, title, status, seconds, start}]
  onChange: function (edl, meta) { /* meta.kind: reorder|trim|toggle|reset */ },
  onSeek:   function (scene, t) { Player.video.currentTime = t; }
});
```

El elemento anfitrión se vacía y se llena solo. Para engancharlo al panel actual
basta con montarlo sobre `#tl` **en lugar de** llamar a `renderTimeline()`, y
sustituir la llamada a `playhead()` por `ed.setPlayhead(video.currentTime)`.

### El handle

| | |
|---|---|
| `ed.setScenes(scenes)` | llegaron escenas nuevas del pipeline; **conserva la edición** |
| `ed.setPlayhead(t)` | pinta el playhead en tiempo de montaje |
| `ed.getEDL()` / `ed.duration()` | estado actual |
| `ed.save()` | persiste (devuelve Promise) |
| `ed.reset()` / `ed.destroy()` | |
| `FFEditor.load(jobId)` | lee la EDL guardada de un job |

---

## El modelo: EDL, no `scenes`

`scenes` es lo que genera el AgentLoop. La **EDL** es cómo se montan:

```json
[{ "n": 3, "in": 0,   "out": null, "enabled": true  },
 { "n": 1, "in": 1.2, "out": 4.0,  "enabled": true  },
 { "n": 2, "in": 0,   "out": null, "enabled": false }]
```

Están separadas a propósito, y el detalle que importa es **`out: null`**, que
significa "hasta el final, dure lo que dure":

- una escena que **recortaste** mantiene tu corte aunque el pipeline la relance
  con otra duración;
- una que **no tocaste** crece sola cuando llega su duración real.

Sin eso, cada `scene_ready` te pisaría la edición. Es la razón de que esto no
sea un array de índices.

---

## Backend — `server/editor.py`

Fichero aparte, router propio. Para engancharlo, en `server/app.py`:

```python
from server import editor
app.include_router(editor.router)
```

| Endpoint | Qué hace |
|---|---|
| `GET  /api/jobs/{id}/edl` | la EDL guardada, ya reconciliada con las escenas de ahora |
| `PUT  /api/jobs/{id}/edl` | guarda; sanea entradas basura, duplicadas o fuera de rango |
| `POST /api/jobs/{id}/edl/reset` | vuelve al orden natural |
| `GET  /api/jobs/{id}/cut` | el montaje resuelto: cada clip con `path`, `in`, `out`, `at` |

Crea su propia tabla `edls` (`CREATE TABLE IF NOT EXISTS` en el primer uso), así
que **no toca `db.py`**. Autocomprobación: `.venv/bin/python -m server.editor`.

---

## Qué funciona y qué falta

**Funciona, verificado en navegador contra `demo.html`:**

- reordenar arrastrando (Sortable) y con `Alt+←/→` desde el teclado;
- recortar por los dos bordes, con el clip encogiendo en vivo y topes a 0.4 s;
- saltar/reincorporar una escena;
- el playhead sigue el montaje **editado**, no el original;
- la edición sobrevive a que el pipeline entregue o añada escenas;
- persistencia y saneado en el servidor, con autocomprobación que pasa.

**Falta:**

1. **Engancharlo al panel real.** Está a propósito sin tocar: `web/index.html`,
   `web/app.js` y `web/styles.css` los estaba editando otro agente. Es cambiar
   `renderTimeline()` por un `FFEditor.mount()` — ver "Cómo se monta".
2. **`include_router(editor.router)`** en `server/app.py`, por lo mismo.
3. **Que el render final ejecute la EDL.** `editor.cut_plan()` ya da los clips
   resueltos y `editor.ffmpeg_plan()` los comandos de corte; falta que
   `assembler.concat_master()` los use en vez de concatenar las escenas en orden
   natural. Hoy el montaje se ve y se guarda, pero el MP4 final aún sale sin él.
4. No hay multipista, ni transiciones, ni deshacer. Fuera de alcance.
