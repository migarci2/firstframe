# FirstFrame

**A review room for generative video that puts the first frame on screen while the rest is
still rendering.**

Live: **https://firstframe.migarci2.dev** — the review room is at `/app`, access code
**`FIRSTFRAME`**. Built on Backblaze B2 and Genblaze 0.4.5. Total cloud spend on generation
for this project: **$0.00**.

![The first frame lands at 9.3 s; the full render takes 65.7 s](https://raw.githubusercontent.com/migarci2/genblaze-hackathon/master/docs/gifs/concept-race.gif)
*Paste a brief, press New spot. The two clocks start; the first frame lands while the
"full render" clock is still counting. Ends with both clocks stopped and the ratio visible.*

---

## Inspiration

A small studio ships around 40 AI-generated product spots a week and rejects about a third
of them. Their bottleneck is not generating. It is **waiting three to five minutes for a
render in order to reject it at second ten.**

Every part of that is wrong economically. The rejection was knowable from the first frame.
The GPU minutes spent after the decision was already obvious are pure waste. And the
reviewer's iteration loop — the thing that actually determines whether the spot is any
good — is capped at "minutes per attempt" by a constraint that comes from *how the file is
delivered*, not from how it is made.

Nobody streams generative video while it is being generated. Everyone waits for the file.

The original architecture was going to be **B2 Live Read**: read an object while the
multipart upload is still open. Backblaze markets it for exactly this. We probed it against
the real account before writing a line of product code
(`scripts/probe_liveread.py`): with a multipart upload open and part 1 of 5 MiB already
uploaded, `GetObject` with `Range` returns **404 NoSuchKey**, not the **416** the API
defines when Live Read is active. We tested with `x-backblaze-live-read-enabled` injected in
`before-send` (outside the SigV4 signature) and in `before-sign` (inside it), across
`CreateMultipartUpload`, `UploadPart` and `GetObject`. It is a paid capability
($15/TB of marked upload capacity) and this is a free account.

So the design became incremental HLS published to the bucket segment by segment. That turned
out to be the better architecture for this problem: it is provider-agnostic, it degrades to
local disk, and it is what lets an external reviewer with a read-only key watch a spot with
no credentials of ours in the path.

---

## What it does

1. You paste a brief and press **New spot**. The job returns immediately and the player
   attaches in about a second.
2. The Genblaze pipeline generates the spot scene by scene. **Each finished scene is
   segmented and pushed to B2 the moment it exists**, and the HLS playlist is rewritten
   after every single segment. You are watching 0:00 while scene 3 is still being generated.
3. Two clocks run on screen the whole time — **first frame** and **full render** — with the
   ratio between them. The app records both into its own database.
4. **Reject** in the first seconds with a note. The note goes into the keyframe prompt of a
   new `AgentLoop` pass, the refined take is appended to the *same live playlist* as a new
   scene, and the discarded take drops into `rejected/` as evidence with a lineage edge back
   to the run that produced it.

   ![A spot playing while the remaining scenes are still generating](https://raw.githubusercontent.com/migarci2/genblaze-hackathon/master/docs/gifs/first-frame.gif)
   *Reject a scene with a note mid-render: the discarded take drops to `rejected/`, the
   refined take is appended to the same live playlist as a new scene, and the inspector shows
   the lineage edge back to the rejected run.*

5. **Approve** and the master gets its Genblaze manifest embedded inside the MP4, uploads to
   `approved/{job}/final.mp4` under `ObjectLockMode=GOVERNANCE` for 30 days, and the
   approved manifest is written by an `ObjectStorageSink` carrying
   `manifest_lock=ObjectLockConfig(...)`. B2 then physically refuses to delete that version.
6. Press `k` at any time to kill a provider. The next call raises a real `MODEL_ERROR`,
   `fallback_models` fires, `pixverse-v5.6` → `seedance-2-0`, and the render continues with
   zero human actions.

### The number

| | measured |
|---|---|
| **First frame on screen** | **9.3 s** |
| **Full render** | **65.7 s** |
| **Gap** | **7.1×** |

Job `j_47cdc2`, 6 scenes, recorded by the app itself and **still live on the deployed
instance** (`first_frame_ms=9303`, `total_render_ms=65652`). Open the review room with the
access code and check it — this is not a number from a slide.

The gap is a property of the architecture, not of the provider. Full render is the sum over
all scenes; first frame is one segment of one scene:

$$
t_{\text{first}} \;\approx\; t_{1} + t_{\text{seg}}
\qquad
t_{\text{full}} \;=\; \sum_{i=1}^{N} t_{i}
\qquad\Rightarrow\qquad
\frac{t_{\text{full}}}{t_{\text{first}}} \;\sim\; N
$$

The numerator is "all N scenes" and the denominator is always "one segment", so the ratio
scales with the length of the spot. At $N = 6$ we measured 7.1×. With slower, real providers
both numbers grow and the ratio grows with them.

The clock is not decoration: `first_frame_ms` stops when the first segment of **real
content** is servable. The "LIVE" slate published at `scene: 0` is explicitly excluded
(`server/assembler.py`).

**One honesty note about that measurement.** The vision judge is off (`JUDGE_THRESHOLD=0`)
for demo runs, from the environment rather than by removing it from the code. The reason is
concrete and measured: NIM's free-tier vision judge takes ~30 s per scene and, when it times
out, it degrades to a neutral 0.50 — below threshold — which triggers another iteration and
another 30 s. With the judge on, first frame goes to about 70 s. Set
`JUDGE_THRESHOLD_REJECT=0.7` and the reject path calls the real vision model instead; the
code documents exactly which parts of the demo are which (`server/jobs.py`, `_refine_scene`).

---

## How we built it

One FastAPI process — SSE, chunked streaming and background threads in the same interpreter
as Genblaze, so the pipeline needs no external queue. sqlite from the standard library.
Vanilla HTML/JS with no build step. Local ffmpeg. One B2 bucket. Python end to end.

The hard part is the assembler. Genblaze is sequential — `batch_run` is always sequential
and `abatch_run(max_concurrency=0)` deadlocks — so there is no concurrency to win here.
What we win is *ordering*: `runner.run_job` calls `on_scene(path)` the instant a scene's mp4
exists, not at the end of the job. That callback hands the file to `server/assembler.py`,
which transcodes it to canonical encode parameters, segments it into ~2 s chunks, uploads
each `.ts` to `incoming/{job}/seg/` **as ffmpeg closes it**, and regenerates
`incoming/{job}/index.m3u8` after each one with `#EXT-X-PLAYLIST-TYPE:EVENT`. Because each
scene is an independent encode whose PTS restarts at zero, the playlist emits
`#EXT-X-DISCONTINUITY` at every scene boundary — without it the player stalls at the seam.

Everything else follows from that: segment duration is the floor on "first frame", so it is
2 s and not 4; every scene must leave the pipeline with byte-identical encode parameters;
and the whole app has to survive B2 being slow, capped or unavailable without the demo dying.

### How we use Backblaze B2

B2 is not a bucket we drop finished files into. It is the delivery surface, the audit surface
and the tenancy boundary.

**The key layout is the data architecture.** One prefix per concern, because B2's own rules
force the decision: application keys are scoped by prefix, lifecycle rules are scoped by
prefix, and B2 rejects two event-notification rules that share an event type with
*overlapping* prefixes. Choose the prefixes badly and you cannot express the policy at all.
`infra/b2_setup.py` validates the overlap rule locally before calling the API, so a design
mistake surfaces as a readable error instead of an opaque 400.

```
refs/{job}/                       source assets, documented with Pipeline.ingest
incoming/{job}/seg/00001.ts       HLS segments, uploaded as ffmpeg closes them
incoming/{job}/index.m3u8         playlist, rewritten after every segment
runs/{job}/scene-{n}/             Genblaze ObjectStorageSink outputs
provenance/{job}/manifest.json    aggregated job manifest — as an OBJECT, never metadata
approved/{job}/final.mp4          master + embedded manifest + GOVERNANCE 30d
approved/{job}/manifest.json      written by the sink with manifest_lock=ObjectLockConfig
rejected/{job}/take-{k}.mp4       discarded takes: the evidence of the refine loop
```

That last comment in the block is a constraint we hit and had to design around: **enabling
Object Lock on a bucket drops the name + file-info limit from 7000 bytes to 2048.** A
3-scene Genblaze manifest is far larger than that, so the manifest is always the object
*body*, never `Metadata=` (`pipeline/manifest.py`).

**Object Lock, proven rather than mentioned.** Approve uploads the master with
`ObjectLockMode="GOVERNANCE"` and `ObjectLockRetainUntilDate=now+30d`, and writes the
approved manifest through an `ObjectStorageSink` constructed with
**`manifest_lock=ObjectLockConfig(mode="GOVERNANCE", retain_until=...)`** — the SDK parameter
that hands provenance records to B2 as WORM. No example in the SDK uses it.

The proof is in the self-checks, not in prose. And getting the proof right required a
subtlety that is easy to miss: **`delete_object` without a `VersionId` only writes a hide
marker, and Object Lock does not stop that** — the version underneath is untouched. A
screenshot of that "working" proves nothing. To show retention protects the actual bytes you
have to delete the *specific version*, and that is what B2 rejects. Both
`pipeline/manifest.py` and `server/b2.py` do exactly this in their `demo()`: upload, read the
retention back, delete the version, assert the `AccessDenied`.

![A delete bounces off an object under Object Lock GOVERNANCE](https://raw.githubusercontent.com/migarci2/genblaze-hackathon/master/docs/gifs/concept-object-lock.gif)
*Terminal running `python -m server.b2`: upload → read retention back → attempt to delete the
specific VersionId → B2 answers AccessDenied → self-check passes.*

**Four lifecycle rules, including the one nobody uses.** Rule 1 is
`daysFromStartingToCancelingUnfinishedLargeFiles: 1` on `incoming/` — the failure mode
specific to generative *video*: a render dies mid-flight and leaves an orphan multipart
upload whose parts keep occupying and keep billing. B2 cancels it in 24 h. Zero cron, zero
cleanup, zero silent cost leak. Rule 4 exists to be **in tension** with retention on the same
objects: a 1-day hide-to-delete rule pointed at `approved/`, against a 30-day GOVERNANCE
retention on the masters inside it. Retention wins. Junk and hidden versions get purged hard;
the master delivered to the client is untouchable for 30 days — not by us, not by a badly
written rule, not by a leaked key. `infra/b2_setup.py` is idempotent and does not trust the
API's own response: it re-reads the bucket after writing, compares, and exits 1 on mismatch.

**Multi-tenancy at the storage layer.** Two restricted application keys, both scoped to the
bucket: `firstframe-server` (20 caps, no `writeBuckets`, no `deleteBuckets`, no `writeKeys`)
and `firstframe-reviewer` (**`readFiles` only**, prefix **`approved/`**). The client's
external reviewer does not get an application-layer `if user.role == "reviewer"`. They get a
Backblaze key that physically cannot write, cannot delete, cannot even *list* the bucket, and
cannot see a byte outside `approved/`. There is no code of ours in the authorization path
that could have a bug in it. `infra/make_keys.py` verifies this against the live API for every
key it creates — it re-authorizes with the new key, compares capabilities / bucketId /
namePrefix against what was requested, and then asserts the negatives: `b2_get_upload_url`
must come back **401**, and an S3 `list_objects_v2` must come back **403 AccessDenied**. Any
failure exits 1.

![brief to scene to HLS segments to Backblaze B2 to player](https://raw.githubusercontent.com/migarci2/genblaze-hackathon/master/docs/gifs/concept-pipeline.gif)
*Terminal running `python infra/make_keys.py`: both keys created, capabilities compared, then
the negative assertions — write → 401, list → 403 AccessDenied — printing green.*

**Presigned URLs, AWS4 path-style.** Virtual-host-style presigns
(`https://bucket.s3.<region>.backblazeb2.com/key`) fail in the browser against private B2
buckets. The fix is undocumented: force `Config(s3={"addressing_style": "path"})`.
`presign_path_style()` asserts the shape of the URL it produces, and the self-check fetches
the presigned URL over plain HTTP with no credentials and checks the body.

**Event Notifications, written and gated.** Five rules under the 25-rule limit with no
overlapping prefixes inside an event type — including `cleanup-audit` on
`b2:HideMarkerCreated:LifecycleRule`, which turns a lifecycle rule *acting* into a live audit
feed. `POST /webhooks/b2` verifies HMAC-SHA256 of the raw body in constant time, does
`INSERT OR IGNORE INTO events(event_id)` because B2 delivers at-least-once, enqueues and
returns, because B2 requires a 200 in under 3 s. This account has the Event Notifications API
gated (B2 answers `400 ... not enabled`), so `infra/b2_setup.py` detects that specific
failure, prints `WARN: event notifications gated -> EVENTS_MODE=poll` and exits green. The
poller emits the *same internal events*, so `EVENTS_MODE=webhook|poll|both|off` is a
one-variable switch and nothing downstream knows the difference.

### How we use Genblaze

**`AgentLoop` + `ThresholdEvaluator` with a real vision judge.** Each scene is wrapped in an
`AgentLoop` whose `ThresholdEvaluator` has a `score_fn` that calls
`meta/llama-3.2-90b-vision-instruct` on NVIDIA NIM with the actual keyframe and the brief and
returns 0..1. The SDK's only `AgentLoop` example uses mocks; this one looks at pixels. On a
fail, the judge's stated reason is injected into the next iteration's keyframe prompt through
`feedback_fn`. Two things about that judge are verified facts, not preferences: the content
must be the OpenAI-style array with
`{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}` — the inline
`<img src="data:...">` style returns *wrong answers* (a pure red PNG described as "orange",
then "grey") — and `nemotron-nano-12b-v2-vl` fails even with the correct format. So
`pipeline/judge.py`'s `demo()` asserts the model can actually see a red frame before trusting
any score, and the judge degrades to a neutral score flagged `degraded=True` rather than
killing the run.

**A real fan-in: `input_from=[1, 2]` into `FFmpegCompositor`.** Step 3 takes the voiceover
(step 1) and the clip (step 2) in the same `step.inputs`. That is a DAG, not a chain, and it
is not decorative: `FFmpegCompositor` refuses to run unless it receives both an `audio/` and
a `video/` asset, and `input_from` is the only way to hand it both. The self-check asserts the
compositor's inputs were exactly `{audio, video}`.

**`fallback_models` with a failover you can trigger on camera.** Keyframe
`flux.1-schnell` → `stable-diffusion-3-5-large-turbo`; clip `pixverse-v5.6` → `seedance-2-0`.
Declaring fallbacks is cheap; demonstrating them is the criterion. `ChaosWrapper` raises a
genuine `MODEL_ERROR` when a provider is flagged dead, kills only *guarded* models so the
fallback still has somewhere to go, and delegates through `inner.invoke()` rather than
`inner.generate()` so the wrapped provider keeps its submit/poll/fetch cycle and re-raises
preserving the real `error_code`. The self-check asserts the step ended on `seedance-2-0`
with `fallback_from == "pixverse-v5.6"`.

![The timeline: one clip per scene, filling in as each render lands](https://raw.githubusercontent.com/migarci2/genblaze-hackathon/master/docs/gifs/timeline.gif)
*In the review room: press `k`, kill `pixverse-v5.6`, and the render continues — the
inspector shows the step finishing on `seedance-2-0` with `fallback_from` set.*

**Lineage by `parent_run_id`, at two levels.** Scene N hangs off scene N-1 via
`Pipeline.from_result(parent)`, and inside a scene the `AgentLoop` chains its own iterations
the same way. Because the SDK rewrites `parent_run_id` when the loop refines, we keep the
scene-to-scene edge separately as `chain_parent_run_id`, so the aggregate manifest can publish
the whole tree instead of one of its two axes. Rejecting a take creates a third kind of edge:
the refined run records `rejected_run_id`, so the manifest carries the bad-take → good-take
chain. All of it verified by assertion in `pipeline/runner.py`.

**Manifest embedded in the MP4, and actually verified.** On approve, `SmartEmbedder` /
`Mp4Handler` write the manifest into the MP4's uuid box, and we immediately extract it again
and compare canonical hashes — if it cannot be read back, we do not upload it as
"verifiable". `verify(job)` is `genblaze verify final.mp4 --fetch`: download the master from
B2, extract the embedded manifest, check the canonical hash, run the manifest's own
`verification_report()`, and **re-download every declared asset to re-hash it**, including the
check that matters to a third party — that the master sitting in B2 right now is byte-for-byte
the one that was approved. (Note for anyone reproducing this: **genblaze 0.4.5 does not
install a `genblaze` CLI** — no `entry_points`. Our code uses the CLI when it is on `PATH` and
otherwise runs the identical checks in-process, reporting which of the two ran in `method`.)

![Approving seals the master under Object Lock for 30 days](https://raw.githubusercontent.com/migarci2/genblaze-hackathon/master/docs/gifs/object-lock.gif)
*Approve a spot: the lock badge appears, the inspector shows the embedded manifest, and the
verify button returns green with the re-hashed assets listed.*

**`ObjectStorageSink`, used correctly.** A new sink per scene run, closed in a `finally` — it
is single-use. The non-obvious part is `_owns_sink=False`: `AgentLoop` passes the *same*
`run_kwargs` to every iteration, so with the default the sink is closed after iteration 1 and
iteration 2 writes into a dead pool.

**`Pipeline.ingest` for provenance of things the pipeline did not generate.** The approved
master is assembled by ffmpeg from scene outputs, so no pipeline run produced it.
`Pipeline.ingest` is the SDK's answer for exactly that: the master gets a real Genblaze
manifest with its source, its sha256, the run ids of every scene it came from, and the key of
the aggregate manifest. And because that ingest run carries a sink with `manifest_lock`, the
manifest lands in B2 already WORM.

**Two providers of our own.** `GEN_MODE=mock|free|real`. `mock` is ffmpeg `testsrc2`, instant
and offline; `real` is the paid connectors; **`free` is real generation with no card and no
API key at all** — and it exists because we wrote the two providers it needs.

- **`PollinationsProvider`** (`pipeline/free_provider.py`). We had no credentials for any
  image-generation provider — NIM's free tier gives chat and vision, but its `genai` image
  endpoint hangs. Rather than ship a demo made entirely of `testsrc2`, we wrote a real
  `SyncProvider` against the one image API that answers 200 with zero credentials. It behaves
  like a real provider because it has real problems: a process-wide lock because the anonymous
  tier queues one request per IP and a second concurrent request gets an instant 429; retries
  honouring `Retry-After`; magic-byte sniffing so a 200 that isn't an image never reaches B2;
  real `sha256` and real dimensions read back *off the file* rather than copied from the
  request (the anonymous tier silently caps resolution at 1024x576, so the `Asset` would
  otherwise lie); and an unknown model mapped deliberately to `MODEL_ERROR`, because that is
  the only code `fallback_models` reacts to. Measured honestly in its docstring: 44.9 s min /
  46.9 s mean / 52.8 s max per image, and it is rate limiting rather than generation time —
  identical latency at 512x288 and at 1024x576.
- **`KenBurnsProvider`** (`pipeline/kenburns.py`). There is no free video-generation model
  anywhere, so the image→video slot needed filling, and a slideshow reads on camera as exactly
  what it is. Three details are the difference between "looks generated" and "looks shot":
  supersample to 3× canonical size (3840x2160, lanczos) before `zoompan`, because applied
  directly to 1024x576 it judders and goes soft; write every expression against `on`, the
  output frame index, never the cumulative `z='min(zoom+0.0015,1.5)'` form everyone copies —
  cumulative expressions depend on the previous frame, so the move changes if `d` changes and
  the shot is not reproducible; and smoothstep easing with `move_for(n)` rotating
  push-in / pan / pull-out / rise by scene index, so a 3-scene spot never repeats a move.

Because the free tier costs 45 s per image, the scene pipeline wraps Pollinations in
`CachedPollinations` with a persistent keyframe corpus keyed on prompt + seed + model + size,
and a seed derived from the prompt itself: same brief, same image, instantly; refined prompt,
new image — which is exactly what refining should mean. The SDK's own `StepCache` is not
enough here because `runner.run_job` namespaces it per job, so two jobs with the same brief
would each pay the 45 s again.

**`PassthroughProvider`** (`pipeline/providers.py`) exists because `Pipeline.input(file)` does
not. Step 0 must be a generating provider — there is no way to start a pipeline from an asset
that already exists. Ten lines fixes it, and it is what `scripts/probe_spine.py` uses to
exercise the whole storage spine without spending a cent.

---

## Challenges we ran into

The interesting ones were not "we could not make it work". They were cases where something
reported success and was wrong.

**`Pipeline` ships with `preflight=True`, and the check it runs is inverted.** With the
default, `run()` calls `validate_model()` on every step before executing anything, and that
function returns `ok_authoritative` for model slugs that 404 on real submit, and
`unknown_permissive` for slugs that work. This is the worst bug shape there is: the other
failures fail loudly, this one *succeeds* loudly. You read "preflight passed", believe the
slug is good, and spend twenty minutes debugging credentials, region and quota — because the
SDK already told you the model was fine. It inverts in both directions too, which trains users
to ignore the warning channel entirely. We construct with `preflight=False` everywhere, with a
comment at the line explaining why.

**`fallback_models` only fires on `MODEL_ERROR`.** Not on transport timeouts. We found this by
measuring, not by reading: `Pipeline._try_fallback_models` keys off
`ProviderErrorCode.MODEL_ERROR`, and a step that dies on a socket timeout simply dies. This
changed the chaos switch from "make the provider hang" to "raise a genuine `MODEL_ERROR`", and
it changed what we are willing to claim about resilience: declaring fallbacks buys you model
outages, not network outages.

**The `.m4a` mime type was guessed from the host, and the container did not have the answer.**
`mimetypes.guess_type()` reads the *system* mime database. A slim base image does not know
`.m4a` and returns `application/octet-stream`. `FFmpegCompositor` requires an input whose
`media_type` starts with `audio/`, so the step-3 fan-in blew up with "No audio asset found"
**only inside the container** — never on the laptop. The fix is a fixed map of the types the
pipeline actually produces, so nothing depends on the host
(`pipeline/providers.py:94-106`). This is the class of bug that only shows up when you
actually deploy the thing.

**`genblaze-s3`'s preflight does a `HeadBucket`, and `preflight=False` does not disable it.**
`HeadBucket` is a Class B transaction. With B2's daily transaction cap reached it returns 403,
and `is_sticky_preflight_error` classifies that 403 as a *permanent* error — the same category
as bad credentials — so the backend caches the failure and stays poisoned for the whole
process lifetime, killing runs before they generate anything, even though uploads (Class A)
were still working fine. And passing `preflight=False` does not turn it off: it only defers
the check to the first I/O ("leave the verify-on-first-use machinery alone" in the SDK's own
source). Our region and bucket are verified at setup time, so we mark the verification as done
and let the real operation be the thing that fails if there is a real problem
(`pipeline/manifest.py:98-118`).

**We exhausted the account's daily transaction cap, and it changed the design.** The first
poller listed four prefixes every 2 s: 120 Class C calls a minute. B2 started answering
`AccessDenied: Transaction cap exceeded` on `ListObjectsV2` and `HeadObject`, which took the
whole app down. What the code does now is the most "production" part of the codebase: every B2
call goes through one `_call()` wrapper that counts it by operation, detects the cap and stops
calling for a cooldown instead of hammering the wall; repeated reads are memoised with a TTL;
the poller does one prefix per tick, rotating, on an adaptive interval (10 s with a live job,
60 s idle, scoped to `incoming/{job}/` rather than all of `incoming/`) — from 3600 listings an
hour to about 110; playback reads local disk first with B2 as durable store and fallback,
because serving every segment from B2 costs one Class B transaction per segment *per viewer*;
an approve that hits the cap leaves the master locally and marks the job unlocked, and a retry
thread uploads it and applies the Object Lock when quota returns, with nobody re-approving
anything; and `/api/health` reports the running transaction count by operation plus cap state.
`server/b2.py`'s `demo()` asserts the whole app degrades to local disk under a simulated cap.

**hls.js does not forgive the first request.** A 404 on the first playlist fetch raises a fatal
`manifestLoadError` and it never retries. An empty-but-valid EVENT playlist raises
`levelEmptyError` — same outcome. And the normal case for this app is exactly that: the user
creates a job and the player attaches before any segment exists. Two fixes, both in the code:
publish a 2-second "LIVE — generating N scenes" slate the instant the job is created (an honest
label, not fake content, and excluded from both the approved master and the first-frame clock),
and hold the first `m3u8` request server-side for up to 6 s waiting for segment one. Segments
are also requested while they are still landing, so the segment endpoint retries internally for
~2 s instead of answering 404.

**And one that had nothing to do with either SDK.** The campaign video is rendered with
Remotion, and its loader died with `Cannot read properties of undefined`.
`@remotion/bundler/dist/esbuild-loader` calls `typescript.sys.readFile` to read the tsconfig,
and **TypeScript 7 — the native Go port — does not expose `ts.sys`**. It was not webpack and it
was not the Node version. Pinned to TypeScript 5.9 and it built.

---

## Accomplishments we're proud of

- **The number is real and the app measures itself.** 9.3 s to first frame against 65.7 s for
  the full render, 7.1×, job `j_47cdc2`, still readable in the deployed instance's database.
- **Object Lock that is proven rather than mentioned** — including the `VersionId` subtlety
  that separates a screenshot of Object Lock from Object Lock actually working.
- **The lifecycle/retention tension, on purpose.** A 1-day hide-to-delete rule aimed at
  `approved/` against a 30-day GOVERNANCE retention on the masters inside it, so that the
  system purges hard and still cannot touch the delivered master.
- **`daysFromStartingToCancelingUnfinishedLargeFiles`** — aimed at the failure mode specific
  to generative video: the dead render that leaves an orphan multipart upload billing quietly.
- **Authorization with none of our code in the path**, verified against the live API: write →
  401, list → 403.
- **Two Genblaze providers we wrote ourselves**, which is why the deployed instance can
  generate real images for a judge with no credentials and no cost.
- **Four upstream contributions to the Genblaze SDK**, all found by building with it:

| | |
|---|---|
| [PR #258](https://github.com/backblaze-labs/genblaze/pull/258) | `PromptTemplate("literal")` crashes — and the shipped `examples/batch_with_templates.py` dies on its own first line. `__init__` shim; 9 tests, 7 of which fail on `main`. |
| [PR #259](https://github.com/backblaze-labs/genblaze/pull/259) | `from genblaze_core.testing import MockProvider` raises `ModuleNotFoundError: pytest` on a clean install — and that line *is* the zero-API-key quickstart in `libs/core/README.md`. Lazy pytest import; 2 subprocess regression tests. |
| [PR #260](https://github.com/backblaze-labs/genblaze/pull/260) | Misplaced `run()` kwargs die with a bare `TypeError`. Now they name the call site that works, for 11 options across `run`/`arun`/`batch_run`/`abatch_run`; 20 tests plus a config-location table generated from live signatures. |
| [Issue #261](https://github.com/backblaze-labs/genblaze/issues/261) | `@dataclass` on a provider subclass silently skips `BaseProvider.__init__` and surfaces ~1300 lines away as an `AttributeError` on a private attribute. Filed as an issue rather than a PR because hard-failing at class definition is a maintainer's policy call. |

- **$0.00 of cloud generation spend**, and every module ships a `demo()` that self-checks:
  `python -m server.b2`, `python -m server.events`, `python -m pipeline.kenburns`, and the
  rest.

---

## What we learned

**A check that lies is worse than no check.** The preflight inversion cost us more time than
any hard failure in the project, because a wrong safety check spends the user's trust budget
and then spends it against you. We now treat "it reported success" as an unverified claim: the
Object Lock demo deletes the specific version, `make_keys.py` re-authorizes with the key it
just made, `b2_setup.py` re-reads the bucket instead of trusting the API's response, and the
manifest is extracted back out of the MP4 before we are willing to call it verifiable.

**Streaming changes what the product is, not just how fast it feels.** Once the first frame
arrives in seconds, "reject" stops being a verdict on a finished artifact and becomes a step
in a loop. That is why rejection feeds the prompt of the next `AgentLoop` pass and why the
refined take is appended to the same live playlist — the reviewer never leaves the timeline.

**Storage-layer authorization beats application-layer authorization.** A restricted
application key scoped to `approved/` is a guarantee; `if user.role == "reviewer"` is a hope.

**Quotas are a design input, not an operational detail.** Blowing the transaction cap produced
the counting wrapper, the adaptive poller, the memoised reads and the deferred-lock retry —
all of which the app would need in production anyway.

**And, for the SDK: CI should execute the things that make promises.** Examples, README
snippets and minimal installs. Every P0 we found upstream would have been caught by that one
job. The full write-up, including the items we did not PR and why, is in
`research/sdk-feedback.md`.

---

## What's next

- **Live Read the day the account allows it.** The architecture keeps HLS either way; Live
  Read would remove the segment-duration floor on first frame, which is currently the hard
  bound on the 9.3 s.
- **Flip `EVENTS_MODE` to `both`** when B2 enables the Event Notifications API on the account.
  The five rules and the signed receiver are already written and tested; nothing downstream
  changes.
- **Per-scene approval** instead of per-job, so a spot can go out while one scene is still
  being refined. The lineage model already supports it — a scene is an independently
  addressable run.
- **Cost and transactions per asset and per team**, on top of the operation counter we already
  ship in `/api/health`.
- **Publish `PollinationsProvider` and `KenBurnsProvider` as standalone Genblaze connectors**
  on PyPI. They were written to solve our problem, but "evaluate this SDK end to end with no
  credentials" is everyone's problem.
