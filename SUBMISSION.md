# FirstFrame — Devpost submission

Paste-ready text for the submission form. Repo: `migarci2/genblaze-hackathon`.
Technical detail with file:line links lives in [`README.md`](README.md);
diagrams in [`docs/architecture.md`](docs/architecture.md).

---

## Tagline

The first frame in seconds, while the rest of the render is still generating.
A review room for generative video built on Backblaze B2 and Genblaze.

---

## Elevator pitch (short description)

Generative video takes minutes to render, and the reviewer only finds out the shot is
wrong after paying for all of it. FirstFrame segments each scene into HLS and publishes it
to B2 the moment it exists, so playback starts on scene 1 instead of scene N — first frame
at 5.4 s against 22.8 s for the full render, a 4.2× gap, measured. Reject in the first
seconds and a vision judge inside a Genblaze `AgentLoop` refines and relaunches the scene;
approve and the master lands in `approved/` with Object Lock GOVERNANCE for 30 days,
manifest embedded in the MP4 and verifiable.

---

## Inspiration

A six-person studio ships roughly 40 AI-generated product spots a week for DTC brands and
rejects about a third of them. Their bottleneck is not generating — it is **waiting three
to five minutes for a full render in order to reject it at second ten**.

Everything in that sentence is wrong economically. The rejection is knowable from the first
frame. The GPU minutes after the rejection are pure waste. And the reviewer's iteration
loop, the thing that actually determines output quality, is capped at "minutes per attempt"
by a constraint that is an artefact of how the file is delivered, not of how it is made.

The original plan was to build this on **B2 Live Read** — read an object while it is still
uploading. It is patent-pending, Backblaze markets it for exactly this, and as far as we
could tell nobody in the field had touched it. We probed it against the real account
before writing a line of product code. It is not available on a free account. That result
is written up below, and it turned out to make the architecture better.

## What it does

Paste a brief, press **New spot**.

1. The job returns instantly and the player attaches within about a second.
2. The Genblaze pipeline generates the spot scene by scene. **Each finished scene is
   segmented and pushed to B2 immediately**, and the HLS playlist is rewritten after every
   single segment. You are watching second 0:00 while scene 3 is still being generated.
3. Two clocks on screen the whole time: **first frame** vs **full render**, with the ratio.
4. **Reject** in the first seconds with a note. The note goes into the keyframe prompt of a
   new `AgentLoop` pass, a vision judge scores the result, the refined take is appended to
   the *same live playlist* as a new scene, and the discarded take drops into `rejected/`
   as evidence.
5. **Approve** and the master gets its Genblaze manifest embedded inside the MP4, uploads
   to `approved/{job}/final.mp4` with `ObjectLockMode=GOVERNANCE` + 30 days, and its
   manifest is written by an `ObjectStorageSink` carrying
   `manifest_lock=ObjectLockConfig(...)`. B2 then refuses to delete that version — which
   the code proves in its own self-check rather than in a screenshot.
6. Press `k` at any time to kill a provider. The next `MODEL_ERROR` triggers
   `fallback_models`, `pixverse-v5.6` → `seedance-2-0`, and the render continues with zero
   human actions.

## How we built it

One FastAPI process (SSE + chunked streaming + background threads in the same interpreter
as Genblaze, so the pipeline needs no external queue), sqlite from the standard library,
vanilla HTML/JS with no build step, local ffmpeg, and one B2 bucket. Python end to end.

The hard part is the assembler. Genblaze is sequential — `batch_run` is always sequential
and `abatch_run(max_concurrency=0)` deadlocks — so there is no concurrency to win. The win
is that `runner.run_job` calls `on_scene(path)` the moment a scene's mp4 exists, not at the
end of the job. That callback hands the file to `server/assembler.py`, which transcodes it
to canonical parameters, segments it, uploads each segment to `incoming/{job}/seg/` **as
ffmpeg closes it**, and regenerates `incoming/{job}/index.m3u8` after each one with
`#EXT-X-PLAYLIST-TYPE:EVENT`, plus `#EXT-X-DISCONTINUITY` at every scene boundary because
each scene is an independent encode whose timestamps restart at zero. (The playlist a
player sees is rebuilt from the database on every request; the copy pushed to B2 is
throttled to one upload every 5 s and always on close.)

Everything else follows from that: the segment duration is the floor on "first frame", so
it is 2 s and not 4; every scene must come out of the pipeline with byte-identical encode
parameters or the player breaks at the seam; the browser cannot talk to a private B2 bucket
so the server proxies; and the whole app has to survive B2 being slow or unavailable
without the demo dying.

## Challenges

**B2 Live Read is gated on a free account.** We found this in the first hour instead of the
last, which is the only reason the project exists. With a multipart upload open and part 1
of 5 MiB uploaded, `GetObject` with `Range` returns **404 NoSuchKey**, not the **416** the
API defines when Live Read is on. We tested with `x-backblaze-live-read-enabled` injected
both in `before-send` (outside the SigV4 signature) and `before-sign` (inside it), across
`CreateMultipartUpload`, `UploadPart` and `GetObject`. It bills at $15/TB. Reproducible
with `scripts/probe_liveread.py`.

**hls.js will not forgive the first request.** A 404 on the first playlist fetch raises a
fatal `manifestLoadError` and it never retries. An empty-but-valid EVENT playlist raises
`levelEmptyError`, same outcome. And the normal case for this app is precisely that: the
user creates a job and the player attaches before any segment exists. Two fixes, both in
the code: publish a 2-second "LIVE — generating N scenes" slate the instant the job is
created (an honest label, not fake content, and excluded from the approved master), and
hold the first `m3u8` request server-side for up to 6 s waiting for segment one.

**We exhausted the account's daily transaction cap.** The first poller listed four prefixes
every 2 s — 120 Class C calls a minute. B2 started answering
`AccessDenied: Transaction cap exceeded` on `ListObjectsV2` and `HeadObject` and the app
went down. This produced the most "production" part of the codebase: every B2 call goes
through one wrapper that counts it by operation and stops calling for a cooldown when the
cap trips; reads are memoised with TTLs; the poller does one prefix per tick on an adaptive
interval (10 s with a live job, 60 s idle, scoped to `incoming/{job}/` not all of
`incoming/`) — from 3600 listings/hour to about 110; playback serves from local disk with
B2 as durable store and fallback; an approve interrupted by the cap keeps the master locally
and a retry thread uploads it and applies the Object Lock when quota returns, with nobody
re-approving anything; and `/api/health` reports the live transaction count.

**`fallback_models` does not cover what you assume it covers.** It fires only on
`ProviderErrorCode.MODEL_ERROR`. A transport timeout leaves the step dead with no failover.
So our chaos switch raises a genuine `MODEL_ERROR` — and it only kills *guarded* models,
not the fallback, because killing everything would leave the pipeline nowhere to go and
there would be no failover to demonstrate.

**`ObjectStorageSink` inside an `AgentLoop`.** The sink is single-use, and `AgentLoop`
passes the same `run_kwargs` to every iteration — so with the default the sink is closed
after iteration 1 and iteration 2 writes into a dead pool. `_owns_sink=False` plus an
explicit `close()` in a `finally`. Related: the sink only reads `file://` paths under the
system temp dir and never plumbs `output_dir`, and it **rewrites `asset.url`** to the
private B2 object during the run, so everything that re-reads an asset afterwards has to
sign the request.

**No media-generation credentials.** NIM's free tier gives chat and vision but its image
endpoint hangs. Rather than ship a demo made entirely of `ffmpeg testsrc2`, we wrote a
`SyncProvider` against Pollinations.ai — the one image API that answers 200 with zero
credentials — and verified it end to end through Genblaze into B2 with a passing manifest.

## Accomplishments

- **The number is real and the app measures it itself.** 5.4 s to first frame vs 22.8 s
  full render, 4.2×, recorded in the app's own database.
- **Object Lock that is actually proven.** Not "we mention Object Lock" — the self-checks
  upload, read the retention back, and assert that B2 rejects deleting the locked version.
  Including the subtlety that a `delete_object` *without* a `VersionId` only writes a hide
  marker and Object Lock does not stop it, so you have to delete the specific version to
  demonstrate anything.
- **The lifecycle/retention tension, on purpose.** A 1-day hide-to-delete rule pointed at
  `approved/`, and a 30-day GOVERNANCE retention on the masters inside it. Retention wins.
  Junk is purged hard, the delivered master is untouchable.
- **`daysFromStartingToCancelingUnfinishedLargeFiles`.** The field nobody uses, aimed at
  the failure mode specific to generative video: a render that dies mid-flight leaves an
  orphan multipart upload that keeps billing quietly.
- **Multi-tenancy at the storage layer, verified live.** The reviewer key has exactly one
  capability, `readFiles`, scoped to `approved/`. `infra/make_keys.py` asserts the negatives
  against the real API: write → 401, list → 403 AccessDenied.
- **Four upstream contributions to the Genblaze SDK**, found by building with it.
- **$0.00 of cloud generation spend.**

## What we learned

The SDK's local paths work and its core bet — manifests and provenance — is good. What is
weak is the first hour, and we hit all of it:

- `Pipeline(preflight=True)` is the default and it runs `validate_model()`, which is
  inverted (#248): it reports models that 404 as valid and models that work as unknown. A
  check that lies is strictly worse than no check, because it spends the user's trust
  budget against you. We construct with `preflight=False` everywhere.
- The shipped `examples/batch_with_templates.py` crashes on its own first
  `PromptTemplate` line — before any provider, before any API key.
- `from genblaze_core.testing import MockProvider` raises `ModuleNotFoundError: pytest` on
  a clean install, and that exact line is the zero-API-key quickstart in the README that
  ships on PyPI. It breaks the specific path a careful evaluator takes.
- `@dataclass` on a `SyncProvider` subclass silently skips `BaseProvider.__init__` and
  surfaces ~1300 lines away as `AttributeError` on a private attribute.
- `genblaze` 0.4.5 does not install a CLI, so `genblaze verify` has to be done in-process.
- `genblaze-s3`'s preflight does a `HeadBucket`. With the transaction cap reached, that 403
  is classified as a permanent error and poisons the backend for the whole process
  lifetime — killing runs before they generate anything, even though uploads still work.

The general lesson, and it is the cheapest structural fix: **CI should execute the things
that make promises** — examples, README snippets, minimal installs. Every P0 above would
have been caught by that one job.

## What's next

- Deploy the live URL (Dockerfile and fly.toml are written; region `ams` to match the
  bucket).
- Turn on Live Read the day the account allows it. The architecture keeps HLS either way —
  Live Read would remove the segment-duration floor on first frame.
- Flip `EVENTS_MODE` to `both` when B2 enables the Event Notifications API for the account.
  The five rules and the signed receiver are already written and tested.
- Per-scene approval instead of per-job, so a spot can go out while one scene is still
  being refined.
- Cost per asset and per team on top of the transaction counter we already ship.
- Publish `PollinationsProvider` as a standalone Genblaze connector on PyPI.

---

## Providers and models used

| Role | Provider | Model | Fallback | Status |
|---|---|---|---|---|
| Keyframe | NVIDIA NIM | `black-forest-labs/flux.1-schnell` | `stabilityai/stable-diffusion-3-5-large-turbo` | declared; NIM's free tier does not serve image generation (verified) |
| Keyframe, credential-free | **Pollinations.ai** — our own `SyncProvider` | `flux` nominal, `sana` on the anonymous tier | `sana`, `turbo` | **real generation, verified end to end into B2** |
| Voiceover | OpenAI | `tts-1` | — | declared. Never GMI Cloud audio: the modality is broken, issue #251 |
| Clip | GMI Cloud | `pixverse-v5.6` | `seedance-2-0` | declared; this is the failover shown on camera |
| Vision judge | NVIDIA NIM | `meta/llama-3.2-90b-vision-instruct` | — | **free and verified working**. `nemotron-nano-12b-v2-vl` gives wrong answers and is not used |
| Scene planning | NVIDIA NIM | `meta/llama-3.3-70b-instruct` | fixed template | optional; the default path is the template so it never depends on an API |
| Composition | local ffmpeg | `FFmpegCompositor` | — | free, always on |
| Development | Genblaze | `MockProvider` / `MockVideoProvider` / `MockAudioProvider` | — | default path, $0 |

Storage: Backblaze B2, bucket `genblaze-review-migarci2`, region `eu-central-003`,
endpoint `https://s3.eu-central-003.backblazeb2.com`.
SDK: `genblaze` 0.4.5, `genblaze-core` 0.3.8, `genblaze-s3` 0.3.6, Python 3.13.

---

## How we use Backblaze B2

Every item links to the exact line in the README, which links to the exact line in the code.

**The key layout is the data architecture.** `refs/`, `incoming/`, `runs/`, `provenance/`,
`approved/`, `rejected/` — one prefix per lifecycle policy, per key scope, per event rule.
This is forced, not aesthetic: B2 rejects two event-notification rules sharing an event
type with overlapping prefixes, and both application keys and lifecycle rules are scoped by
prefix. We validate the overlap rule locally before calling the API, so a design mistake
shows up as a readable error instead of an opaque 400. Second consequence: a bucket with
Object Lock enabled drops the name + file-info limit from 7000 to 2048 bytes, so the
manifest is always the object *body*, never `Metadata=`.

**Incremental HLS served out of the bucket.** Each scene is segmented into ~2 s chunks
uploaded to `incoming/{job}/seg/` as ffmpeg closes them, with
`incoming/{job}/index.m3u8` regenerated after every segment,
`#EXT-X-PLAYLIST-TYPE:EVENT` while the job lives, `#EXT-X-ENDLIST` when it closes, and
`#EXT-X-DISCONTINUITY` at every scene boundary. This is the product. *Serve* is an explicit
verb in the storage criterion and it is the part of B2 the field leaves untouched.

**Object Lock GOVERNANCE, 30 days,** on the approved master and on its manifest — the
latter written by an `ObjectStorageSink` constructed with
`manifest_lock=ObjectLockConfig(...)`, the SDK parameter that hands provenance records to
B2 as WORM and that no SDK example uses. Proven by self-check: upload, read the retention
back, attempt the delete, assert B2 refuses.

**Native versioning.** `delete_object` without a `VersionId` only writes a hide marker and
Object Lock does not stop it. To show retention protecting the bytes you must delete the
specific version — and that is what B2 rejects. We use B2's native versioning for both the
proof and the `bypassGovernance` cleanup path.

**Four lifecycle rules,** including **`daysFromStartingToCancelingUnfinishedLargeFiles: 1`**
on `incoming/` — a dead video render leaves an orphan multipart upload whose parts keep
billing, and B2 cancels it in 24 h with no cron of ours. And a deliberately aggressive
1-day hide-to-delete rule on `approved/` that **cannot touch what is locked**: retention
wins over lifecycle. That pair is the whole argument that the bucket is a system and not a
folder. `infra/b2_setup.py` is idempotent and does not trust the API's own response — it
re-reads the bucket after writing and exits 1 if the read-back does not match.

**Restricted application keys — real multi-tenancy at the storage layer.**
`firstframe-server` holds 20 capabilities scoped to one bucket, with no
`writeBuckets`/`deleteBuckets`/`writeKeys`. `firstframe-reviewer` holds exactly one
capability, `readFiles`, scoped to `approved/`. The client's external reviewer does not get
an application-layer role check; they get a key that physically cannot write, cannot delete,
cannot list the bucket, and cannot see a byte outside `approved/`. `infra/make_keys.py`
verifies every key it creates against the live API — capabilities, bucketId and namePrefix
read back from `b2_authorize_account`, then `b2_get_upload_url` must return 401 and an S3
`list_objects_v2` must return 403 AccessDenied — and exits 1 if any check fails.

**Presigned URLs, AWS4 path-style.** Virtual-host-style presigns fail in the browser against
private B2 buckets; the fix is `addressing_style: "path"` and it is undocumented. The
self-check fetches the presigned URL over plain HTTP with no credentials and compares
the body.

**Event Notifications with HMAC-SHA256.** Five rules on the bucket, no overlapping prefixes
inside an event type, including `cleanup-audit` on `b2:HideMarkerCreated:LifecycleRule` —
which turns a lifecycle rule *acting* into a live audit feed. The receiver verifies
HMAC-SHA256 of the raw body against `X-Bz-Event-Notification-Signature: v1=<64 hex>` in
constant time, deduplicates by `eventId` because B2 delivers at-least-once, enqueues and
returns inside B2's 3-second budget, with a worker thread doing the real work.

**Transaction budgeting.** Every B2 call is counted by operation, the cap is detected and
absorbed, reads are memoised with TTLs, and the poller runs one prefix per tick on an
adaptive interval. `/api/health` reports it all.

## How we use Genblaze

**`AgentLoop` + `ThresholdEvaluator` with a real vision judge.** The `score_fn` calls
`meta/llama-3.2-90b-vision-instruct` on NIM with the actual keyframe and the brief and
returns 0..1. The SDK's only `AgentLoop` example uses mocks; ours looks at pixels. On a
fail, `feedback_fn` puts the judge's stated reason into the next iteration's keyframe
prompt, and iterations chain by `parent_run_id`. Two verified constraints are encoded in the
code: the content must be the OpenAI-style array with `image_url` + data URI — the inline
`<img src="data:...">` style returns *wrong answers* — and `nemotron-nano-12b-v2-vl` fails
even with the correct format. The judge degrades to a neutral score flagged
`degraded=True` rather than taking down a pipeline.

**Real fan-in: `input_from=[1, 2]` into `FFmpegCompositor`.** Step 3 receives the voiceover
(step 1) and the clip (step 2) in the same `step.inputs`. It is a DAG, not a chain, and it
is load-bearing: `FFmpegCompositor` refuses to run without both an `audio/` and a `video/`
asset, and `input_from` is the only way to give it both. The self-check asserts the
compositor's inputs were exactly `{audio, video}`. Composition is also free — local ffmpeg.

**`fallback_models` with a failover you can trigger on camera.** `flux.1-schnell` →
`sd-3.5-large-turbo` on the keyframe, `pixverse-v5.6` → `seedance-2-0` on the clip.
Declaring it is cheap, so we verified what actually triggers it: only
`ProviderErrorCode.MODEL_ERROR`, never a transport timeout. Our `ChaosWrapper` raises a
genuine `MODEL_ERROR`, kills only guarded models so the fallback survives, delegates through
`inner.invoke()` to preserve the wrapped provider's submit/poll/fetch cycle and retry
policy, and re-raises preserving the real provider's `error_code`. The self-check asserts
the step finished on `seedance-2-0` with `fallback_from == "pixverse-v5.6"`.

**Lineage by `parent_run_id`, at two levels.** Scene N hangs off scene N-1 via
`Pipeline.from_result`, and inside a scene `AgentLoop` chains iterations the same way. Since
the SDK rewrites `parent_run_id` when the loop refines, we keep the scene-to-scene edge
separately so the aggregate manifest publishes the whole tree rather than one of its axes.
A rejected take records `rejected_run_id`, so the manifest carries the bad-take →
good-take chain.

**Manifest embedded in the MP4, and verification.** `SmartEmbedder`/`Mp4Handler` write the
manifest into the MP4's uuid box on approve, and we immediately extract it again and compare
canonical hashes — if it cannot be read back we do not upload it as "verifiable". `verify`
is `genblaze verify --fetch`: download the master from B2, extract the embedded manifest,
check the canonical hash, run the manifest's own `verification_report()`, re-download every
declared asset to re-hash it, and confirm the master sitting in B2 right now is
byte-for-byte the one that was approved. Since 0.4.5 ships no CLI, we use it when present
and otherwise run the identical checks in-process, reporting which of the two ran.

**`ObjectStorageSink`, used correctly.** A new sink per scene run, closed in a `finally`,
with `_owns_sink=False` so `AgentLoop` iteration 2 does not write into a pool closed by
iteration 1.

**`Pipeline.ingest`** gives the approved master — assembled by ffmpeg, so produced by no
pipeline run — a real Genblaze manifest carrying its source, sha256, the run ids of every
scene it came from, and the key of the aggregate manifest. That ingest run carries the sink
with `manifest_lock`, so its manifest lands in B2 already WORM.

**Our own provider.** `PollinationsProvider` is a real `SyncProvider` doing real image
generation with zero credentials: process-wide serialization because the anonymous tier
queues one request per IP, backoff honouring `Retry-After`, magic-byte sniffing so a 200
that isn't an image never reaches B2, real sha256 and real dimensions read back off the
file rather than copied from the request, and an unknown model deliberately mapped to
`MODEL_ERROR` because that is the only code `fallback_models` reacts to.

**`PassthroughProvider`,** because `Pipeline.input(file)` does not exist — step 0 must be a
generating provider. Ten lines, and 8 of 10 official sample apps hit this.

**Upstream:** [PR #258](https://github.com/backblaze-labs/genblaze/pull/258),
[PR #259](https://github.com/backblaze-labs/genblaze/pull/259),
[PR #260](https://github.com/backblaze-labs/genblaze/pull/260),
[issue #261](https://github.com/backblaze-labs/genblaze/issues/261). Full write-up in
`research/sdk-feedback.md`.

---

## Honest limitations

Stated here because a judge with repo access should read it from us.

- **B2 Live Read is not available on this account** (verified, see Challenges), so the
  preview is incremental HLS by segments rather than a partial read of a growing object.
- **Event Notifications are gated at the account level** (`400 ... not enabled`). The five
  rules are declared, the signed receiver is written and tested, and the poller emits the
  same internal events, so `EVENTS_MODE=poll` is the effective mode.
- **`DEMO_MODE=mock` is the default path,** because we have no media-generation
  credentials. The mocks are the SDK's real mock providers with an `assets=` callable that
  synthesises genuine local media with ffmpeg. Every step, sink, manifest, lock and
  verification is the real thing; only the pixels are synthetic. `DEMO_MODE=real` swaps each
  provider independently, and a partial environment yields a `mixed` run rather than a
  crash.
- **The headline numbers come from mock-provider runs with the judge off.** NIM's free-tier
  vision judge takes ~30 s per scene and, on timeout, degrades to 0.50 — below threshold —
  triggering another iteration and another 30 s, pushing first frame to ~70 s. The judge is
  real and wired into a real `AgentLoop`; for a live demo it is switched off from the
  environment (`JUDGE_THRESHOLD=0`), not removed from the code.
- **The UI is in Spanish.** The code, the API and the documentation are in English.

---

## Built with

`python` · `fastapi` · `uvicorn` · `sqlite` · `genblaze` · `genblaze-core` · `genblaze-s3` ·
`backblaze-b2` · `boto3` · `ffmpeg` · `hls.js` · `nvidia-nim` · `pollinations.ai` ·
`docker` · `fly.io`

## Links

- **Repo:** https://github.com/migarci2/genblaze-hackathon
- **Live URL:** _pending deploy_
- **Video:** _pending_

## Submission checklist

- [ ] Live URL with no login wall, preloaded data, and a "New spot" button that costs the
      judges nothing (`DEMO_MODE=mock`). Tested from incognito.
- [ ] Video ≤3 min, unlisted, with the on-screen numbers and the failover on camera.
- [ ] Repo public, or `b2genblaze` invited if private.
- [x] README with explicit "How we use B2" and "How we use Genblaze" sections, feature by
      feature, with file:line links.
- [x] Providers and models declared.
- [x] `.env.example` complete and `infra/b2_setup.py` reproducible from scratch.
- [x] Devpost text names: incremental HLS served from B2, Event Notifications with HMAC,
      Object Lock vs lifecycle, `daysFromStartingToCancelingUnfinishedLargeFiles`,
      restricted application keys, `AgentLoop` + `Evaluator`, `fallback_models`, fan-in,
      `manifest_lock`, `Pipeline.ingest`, `ObjectStorageSink`.
- [ ] Submitted at least 2 h before close. Verify the form is **submitted**, not a draft.
