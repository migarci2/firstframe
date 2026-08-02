# FirstFrame — architecture

One FastAPI process, one B2 bucket, no queue, no external services. The whole
system exists to answer one question fast: **is this shot worth finishing?**

---

## 1. The system

```mermaid
flowchart LR
    subgraph Browser
        UI["Review room<br/>web/index.html"]
        P["hls.js player<br/>+ MSE fallback"]
    end

    subgraph Server["FastAPI — one process, one worker"]
        API["server/app.py<br/>REST + SSE"]
        JOBS["server/jobs.py<br/>job orchestration"]
        ASM["server/assembler.py<br/>scene mp4 to HLS segments"]
        STR["server/streamer.py<br/>playlist + segment serving"]
        EV["server/events.py<br/>HMAC webhook + poller + SSE bus"]
        DB[("sqlite<br/>server/db.py")]
    end

    subgraph Pipeline["Genblaze 0.4.5 — pipeline/"]
        RUN["runner.run_job<br/>scene by scene"]
        SC["scenes.build_scene_agent<br/>AgentLoop + ThresholdEvaluator"]
        JG["judge.judge_frame<br/>llama-3.2-90b-vision"]
        MAN["manifest.py<br/>aggregate / approve / verify"]
    end

    subgraph B2["Backblaze B2 — genblaze-review-migarci2, Object Lock enabled"]
        INC["incoming/{job}/<br/>HLS segments + index.m3u8"]
        RUNS["runs/{job}/scene-N/<br/>ObjectStorageSink outputs"]
        PROV["provenance/{job}/manifest.json"]
        APP["approved/{job}/<br/>final.mp4 + manifest.json<br/>GOVERNANCE 30d"]
        REJ["rejected/{job}/take-k.mp4"]
    end

    UI -->|POST /api/jobs| API
    API --> JOBS
    JOBS -->|background thread| RUN
    RUN --> SC --> JG
    SC -->|per-scene sink| RUNS
    RUN -->|on_scene mp4, per scene| ASM
    ASM -->|segment by segment| INC
    ASM --> DB
    STR --> P
    P -->|GET /stream/id/index.m3u8| STR
    RUN --> MAN --> PROV
    JOBS -->|approve| APP
    JOBS -->|reject| REJ
    INC -.->|ObjectCreated| EV
    APP -.->|ObjectCreated| EV
    EV -->|SSE| UI
    API --> DB
```

Dotted arrows are B2 Event Notifications. They are declared on the bucket but the
API is gated on this account, so `server/events.py:Poller` produces the same
internal events from `list_objects_v2`. See the honesty section of the README.

---

## 2. The critical path: why the first frame arrives in seconds

Genblaze runs steps sequentially — `batch_run` is sequential and
`abatch_run(max_concurrency=0)` deadlocks, so there is no concurrency to win here.
The win comes from **not waiting for the last scene to start serving the first one**.

```mermaid
sequenceDiagram
    participant U as Reviewer
    participant A as FastAPI
    participant R as runner.run_job
    participant S as assembler.feed
    participant B as B2
    participant P as Player

    U->>A: POST /api/jobs with brief
    A-->>U: 201 job id, returns immediately
    A->>S: start_leader — 2s LIVE slate
    S->>B: PUT incoming/JOB/seg/00001.ts
    S->>B: PUT incoming/JOB/index.m3u8
    P->>A: GET /stream/JOB/index.m3u8
    A-->>P: EVENT playlist, no ENDLIST — playback starts
    A->>R: run_job in a background thread
    R->>R: scene 1: keyframe, VO, clip, composite
    R->>S: on_scene(scene-1.mp4)
    S->>B: segments 2..N, playlist rewritten after each
    Note over P: reviewer is watching 0:00 while scene 3 is still generating
    R->>R: scene 2, scene 3 ...
    R->>B: provenance/JOB/manifest.json
    A->>S: finish — ENDLIST
    U->>A: POST decision approve
    A->>B: PUT approved/JOB/final.mp4 with ObjectLockMode=GOVERNANCE
```

The floor on "first frame" is the HLS segment duration: a segment cannot be
published until it is closed. That is why `HLS_SEG_SECONDS` defaults to 2, not 4.

---

## 3. One scene as a Genblaze DAG

```mermaid
flowchart TD
    K["step 0 — keyframe<br/>Modality.IMAGE<br/>fallback_models=[sd-3.5-large-turbo]"]
    V["step 1 — voiceover<br/>Modality.AUDIO<br/>text in metadata, not Asset.text"]
    C["step 2 — clip<br/>Modality.VIDEO · input_from=[0]<br/>ChaosWrapper + fallback_models=[seedance-2-0]"]
    X["step 3 — composite<br/>FFmpegCompositor · StepType.MIX<br/>input_from=[1,2] — FAN-IN"]

    K --> C
    V --> X
    C --> X

    J{"ThresholdEvaluator<br/>score_fn = judge.judge_frame<br/>threshold 0.7"}
    X --> J
    J -->|pass| OUT["scene-N.mp4 to assembler.feed"]
    J -->|fail| K2["AgentLoop iteration 2<br/>judge reason injected into the prompt<br/>chained by parent_run_id"]
    K2 --> K
```

`input_from=[1,2]` is a real fan-in, not a formality: `FFmpegCompositor` refuses to
run unless `step.inputs` contains both an `audio/` and a `video/` asset, and that is
asserted in `pipeline/scenes.py` `demo()`.

---

## 4. Bucket layout

The key layout *is* the data architecture: B2 forbids overlapping prefixes inside the
same event type, application keys are scoped by prefix, and lifecycle rules are
scoped by prefix. One prefix per lifecycle policy, per key scope, per event rule.

| Prefix | What lives there | Lifecycle | Lock |
|---|---|---|---|
| `refs/{job}/` | source assets, documented with `Pipeline.ingest` | — | — |
| `incoming/{job}/seg/*.ts` | HLS segments, uploaded as they close | cancel unfinished large files after 1d | — |
| `incoming/{job}/index.m3u8` | playlist, rewritten after every segment | same | — |
| `runs/{job}/scene-N/` | Genblaze `ObjectStorageSink` outputs | hide 3d, delete 7d later | — |
| `provenance/{job}/manifest.json` | aggregated job manifest, as an **object** | — | — |
| `approved/{job}/final.mp4` | master with embedded manifest | hide-to-delete 1d | GOVERNANCE 30d |
| `approved/{job}/manifest.json` | written by the sink with `manifest_lock` | hide-to-delete 1d | GOVERNANCE 30d |
| `rejected/{job}/take-k.mp4` | discarded takes, the audit trail of the refine loop | hide 1d, delete 7d later | — |

The aggressive `approved/` rule and the 30-day retention on the same objects are not a
contradiction — that pair is the point. Retention wins over lifecycle in B2, so garbage
is purged hard while the delivered master is untouchable.

---

## 5. Event plane

```mermaid
flowchart LR
    B2E["B2 Event Notifications<br/>5 rules, HMAC-SHA256"] -->|POST /webhooks/b2| WH["verify sig<br/>INSERT OR IGNORE by eventId<br/>enqueue, ack under 3s"]
    POLL["Poller thread<br/>1 prefix per tick<br/>10s active / 60s idle"] --> DEDUP
    WH --> DEDUP["events table<br/>idempotency key = eventId"]
    INT["internal emitters<br/>assembler, jobs, runner"] --> BUS
    DEDUP --> W["worker thread<br/>_dispatch"] --> BUS["publish()"]
    BUS --> SSE["GET /api/events<br/>text/event-stream"]
    SSE --> UI["review room"]
```

`EVENTS_MODE=webhook|poll|both|off`. Because the idempotency key is the object, not the
source, running `both` is safe: whichever arrives second is a no-op.

---

## 6. Deployment

`python:3.13-slim` + ffmpeg, one uvicorn worker, one persistent machine
(`Dockerfile`, `fly.toml`). Not serverless, on purpose: the assembler uploads segments
while the pipeline is still generating, and the browser holds an SSE connection open for
the whole render. A 10-second function cannot do either. Region `ams` matches the
bucket's `eu-central-003`.
