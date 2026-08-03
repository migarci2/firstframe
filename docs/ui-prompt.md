# UI generation prompt — FirstFrame review console

Self-contained prompt for a UI-generating model (v0, Lovable, Claude, …).
Copy everything below the line.

Notes before you use it:
- The growing-playlist player is the part that resists: segments are MPEG-TS, so
  Chrome will not play them without a vendored `hls.js`.
- The audience is a non-technical producer. An earlier version of this prompt asked
  for a dense engineering dashboard and the result was rejected as unusable. The
  machinery is now hidden behind a disclosure instead of deleted, because the
  storage and orchestration evidence still has to be reachable.

---

# Build a review console for AI-generated video

## Product

A production review room for teams generating video ads with AI. The core insight:
a generative render takes minutes, and today the reviewer waits for the whole thing
to finish only to reject it in the first seconds. This app streams the video
**while it is still being generated** — the reviewer sees second 0:00 while scene 4
of 6 is still rendering, and can kill a bad take immediately.

The entire value proposition is one contrast: **first frame in ~6 seconds vs
~26 seconds for the full render**. That number must dominate the screen.

## Layout

Single screen, 1280×800, no scrolling, no navigation, no login. Three columns:
narrow left rail (~260px), wide center column, right rail (~320px). Everything the
reviewer needs is visible simultaneously.

## Top bar

- Left: square logo mark + product name + subtitle "sala de revisión"
- Center: wide text input for the creative brief (placeholder shows a real example),
  a small numeric select for scene count, and a primary button "New spot"
- Right: three persistent status chips — generation mode (MOCK / FREE / REAL),
  storage health ("B2 OK"), and a live-connection dot with label "EN VIVO"
  that reflects an active server-sent-events stream

## Left rail — job queue

Header "JOBS" with a filter toggle (todos / sin fallidos) and a count.

Job cards, newest first, selected one highlighted with a left border accent:
- short monospace id (`j_fece44`) + status pill
- statuses with distinct colors: `RENDERING` (amber, subtle pulse), `IN REVIEW`
  (blue), `APPROVED` (green), `FAILED` (red)
- title, truncated to one line
- a metrics line: `ff 5.8 s` (first frame, accent color) and total `25 s` (muted)
- a row of small dots, one per scene, filling green as scenes complete

## Center column

### Player
Dominant element, 16:9. Over it:
- top-left: a large `● LIVE — generando escena N de M` badge in red, only while
  rendering. It disappears on completion.
- bottom-right: a translucent chip with the job title
- the video itself carries a `DRAFT` watermark burned in by the pipeline

Control bar under the video: Pause, timecode `0:05 / 0:14`, a small badge naming
the playback engine (`HLS.JS`), a live counter `7 segmentos en B2`, and a keyboard
hint `k · chaos`.

### The metrics band — this is the hero element
Two numbers side by side, huge, monospace:
`PRIMER FOTOGRAMA 5.8 s`  vs  `RENDER TOTAL 25 s`
and to the right, the multiplier `4.3×` with the caption "antes en pantalla".
Below, a horizontal proportion bar showing how much waiting is saved, labelled at
both ends. While a job is rendering, the total counts up live against the frozen
first-frame number, so the multiplier visibly grows.

### Scene strip
Header "ESCENAS" with `3/3` counter. One card per scene: status (READY / PENDING /
RUNNING), scene name (apertura, detalle, cierre), and its render duration.
Completed cards get a green top border.

### Decision zone
A review-note text input (placeholder suggests a real critique), a select for which
scene the note applies to, and two buttons: `Reject` (outlined, red) and `Approve`
(filled, green). One line of helper text explains what each does.

When approved, a green banner appears below:
`Object Lock GOVERNANCE · 30 días` with the retention timestamp and the object key.

## Right rail — evidence

Three stacked panels.

**AGENTLOOP** with an iteration counter. Empty state explains how to trigger it
("Rechaza una toma para ver el AgentLoop…"). Populated state shows, per iteration:
a 0–1 score with a colored bar, the model's reasoning as free text, and the action
taken (prompt refinado · escena relanzada).

**PROVENANCE** with a `Verify` button in its header. Definition-list of:
job_id, created (ISO timestamp), escenas, manifest path, and lock status with its
retention date. Below, a `LINAJE · parent_run_id` section listing runs and takes as
a small vertical timeline, and `OBJETOS EN B2 (3)` listing the object keys.
Clicking Verify shows an inline result: `✔ MANIFEST VERIFICADO (exit 0)` with a
canonical hash, or the failure.

**FEED EN VIVO** — a chronological event log, newest at top. Each row: monospace
timestamp, a color-coded event type, and a detail string. Event types include
`SEGMENT_LANDED`, `SCENE_READY`, `JOB_UPDATE`, `RENDER_COMPLETE`, `PROVIDER_FAILOVER`.

## Transient elements

- **Failover toast** over the video showing both models:
  `pixverse-v5.6 MODEL_ERROR → fallback: seedance-2-0`. Max 3 stacked.
- **Chaos modal**, opened with the `k` key: a list of 4 providers, each with a
  kill/revive switch. This deliberately breaks a provider to demo recovery.
- **Buffer notice** when generation is slower than playback:
  "Buffer al día: esperando la siguiente escena…"
- **Degraded banner** when object storage loses quota, explaining that playback is
  being served from local disk.

## Who this is for

**A producer at a content studio. Not technical.** She does not know and does not
care what a manifest, a hash or a model provider is. Her whole flow is three steps:
write a brief, watch the video appear, say yes or ask for changes.

## The governing rule: hide the machinery, don't delete it

- **Default view is calm and human.** No jargon, no identifiers, no paths, no
  event codes. One obvious thing to do at any moment.
- **A discreet "technical details" control** reveals everything else: provenance,
  lineage, storage objects, the event log, retention state, the evaluation loop
  with its scores. That view can be as dense as it likes — different audience.

Translate every technical string into plain language in the main view:

| Instead of | Say |
|---|---|
| `PRIMER FOTOGRAMA 5.8 s vs RENDER TOTAL 25 s · 4.3×` | "Puedes verlo 4 veces antes de que termine" — one number, not three |
| `● LIVE — generando escena 3 de 3` | "Generando… ya puedes ver el principio" |
| `pixverse-v5.6 MODEL_ERROR → fallback: seedance-2-0` | "Un proveedor falló; seguimos con otro sin perder el trabajo" |
| `Object Lock GOVERNANCE · 30 días · approved/j_fece44/final.mp4` | "Aprobado y protegido: nadie puede borrarlo durante 30 días" |
| `j_fece44`, `parent_run_id`, `sha256` | not in the main view at all |

## Visual direction

Calm, not dense. The video is the absolute protagonist; everything else is
secondary. Few colors, generous whitespace, readable non-monospace type except
where monospace earns its place. A new user should understand the screen in three
seconds with nobody explaining it.

No gradients, no glassmorphism, no decorative emoji, no marketing copy. Think of a
tool people are glad to use every day, not a NASA control panel.

All visible copy in Spanish. Identifiers and event names, where they survive at
all, stay as-is.

## Data shapes

```json
{ "id": "j_fece44", "title": "Aeron Runner — amanecer en la playa",
  "status": "approved", "scene_count": 3, "first_frame_ms": 5812,
  "total_render_ms": 25004, "stream_url": "/stream/j_fece44/index.m3u8",
  "manifest_url": "/api/jobs/j_fece44/manifest",
  "lock": { "mode": "GOVERNANCE", "retain_until": "2026-09-02T00:15:31Z" },
  "scenes": [ { "n": 1, "status": "ready", "ms": 4600, "title": "apertura" } ] }
```

Events arrive over SSE as `{ type, job_id, ...payload }`.

## Constraints

- No build step, no framework, no CDN: plain HTML, CSS and vanilla JS in separate
  files. Any library must be vendored locally.
- Video playback via HLS with a growing playlist; the player must start once two
  segments are buffered and keep playing as new segments appear.
- Must degrade visibly rather than break: if the stream dies, if storage is out of
  quota, if a render fails — say so on screen with the reason.
- Keyboard: `k` opens chaos. Focus states visible throughout.
