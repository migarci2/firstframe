# UI generation prompt — FirstFrame review console

Self-contained prompt for a UI-generating model (v0, Lovable, Claude, …).
Copy everything below the line.

Notes before you use it:
- The audience is a non-technical producer. An earlier version of this prompt asked
  for a dense engineering dashboard and the result was rejected as unusable. The
  machinery is now hidden behind a disclosure instead of deleted, because the
  storage and orchestration evidence still has to be reachable for reviewers.
- The growing-playlist player is the part that resists: segments are MPEG-TS, so
  Chrome will not play them without a vendored `hls.js`.

---

# Build a review console for AI-generated video

## Product

Teams that generate video ads with AI wait minutes for a render, then reject it in
the first seconds. This app streams the video **while it is still being generated**:
the reviewer watches second 0:00 while the last scene is still rendering, and can
kill a bad take immediately instead of paying for the whole render first.

## Who this is for

**Ana, a producer at a content studio. Not technical.** She does not know and does
not care what a manifest, a hash or a model provider is. Her entire flow is three
steps: **write a brief → watch it appear → approve or ask for changes.** The
interface should not ask her for anything else.

## The governing rule: hide the machinery, don't delete it

- **Default view is calm and human.** No jargon, no identifiers, no file paths, no
  event codes. At any moment there is exactly **one obvious thing to do**.
- **A discreet "technical details" control** — a side panel, a modal, a second tab —
  reveals everything else: provenance, lineage, storage objects, the event log,
  retention state, the evaluation loop with its scores, and a way to deliberately
  kill a provider to demo recovery. That view can be as dense as it likes; the
  audience there is different.

## Translate every technical string

| Instead of | Say |
|---|---|
| `PRIMER FOTOGRAMA 5.8 s vs RENDER TOTAL 25 s · 4.3×` | "Puedes verlo 4 veces antes de que termine" — **one** number, not three |
| `● LIVE — generando escena 3 de 3` | "Generando… ya puedes ver el principio" |
| `pixverse-v5.6 MODEL_ERROR → fallback: seedance-2-0` | "Un proveedor falló; seguimos con otro sin perder el trabajo" |
| `Object Lock GOVERNANCE · 30 días · approved/j_fece44/final.mp4` | "Aprobado y protegido: nadie puede borrarlo durante 30 días" |
| `j_fece44`, `parent_run_id`, `sha256`, `SEGMENT_LANDED` | not in the main view at all |

Job cards are identified by the spot's title, never by its id.

## Main view

One screen, 1280×800, no login, no navigation. The video is the absolute
protagonist; everything else is secondary and quieter.

**Create**: a single generous text field for the brief with a real example as
placeholder, and one primary button. Nothing else — no scene-count selector unless
it earns its place.

**Watch**: the player, large. While rendering, a calm line of status in plain
language and a sense of progress. No counters, no engine badges, no segment counts.

**The one number**: the product's whole argument said as a sentence a person would
say. It is the only piece of data allowed to be loud.

**Decide**: a note field and two clear actions — approve, or ask for changes. One
line explaining what each does. On approval, a plain-language confirmation that the
work is now protected and cannot be deleted or altered.

**The queue**: past and running spots as simple cards — title, a human status, and
how long it took. Nothing more.

## Behaviour that must survive the redesign

- Create a spot from a brief
- Play the video while it is still being generated (HLS with a growing playlist,
  starting once two segments are buffered, continuing as new ones appear)
- Approve, and reject with a note that relaunches a scene
- Show, in human terms, that approved work is retained and tamper-proof
- Errors and degraded states explained in plain language, never as stack traces or
  status codes — including "storage is out of quota, playing from local disk"

## Visual direction

Calm, not dense. Few colors, generous whitespace, readable non-monospace type
except where monospace genuinely helps. A new user should understand the screen in
three seconds with nobody explaining it.

No gradients, no glassmorphism, no decorative emoji, no marketing copy. Think of a
tool people are glad to open every day, not a NASA control panel.

All visible copy in Spanish.

## Data shapes

```json
{ "id": "j_fece44", "title": "Aeron Runner — amanecer en la playa",
  "status": "approved", "scene_count": 3, "first_frame_ms": 5812,
  "total_render_ms": 25004, "stream_url": "/stream/j_fece44/index.m3u8",
  "manifest_url": "/api/jobs/j_fece44/manifest",
  "lock": { "mode": "GOVERNANCE", "retain_until": "2026-09-02T00:15:31Z" },
  "scenes": [ { "n": 1, "status": "ready", "ms": 4600, "title": "apertura" } ] }
```

Statuses are `rendering`, `in_review`, `approved`, `failed`.
Events arrive over SSE as `{ type, job_id, ...payload }`, including
`scene_ready`, `render_complete`, `provider_failover` and `job_update`.

## Constraints

- No build step, no framework, no CDN: plain HTML, CSS and vanilla JS in separate
  files. Any library must be vendored locally.
- Must degrade visibly rather than break: if the stream dies, if storage runs out
  of quota, if a render fails — say so on screen, in plain language, with what it
  means for the user.
- Visible focus states throughout; the screen must be usable from the keyboard.
