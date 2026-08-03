# Voiceover script — read this while recording
Solo el texto hablado, en inglés y en orden. Las acotaciones estan en `GUION.md`.
Lee despacio: sale a ~145 palabras por minuto, que es el ritmo que cabe en 3:00.

## 1 · The problem (0:00–0:18)

Ana ships forty AI-generated product spots a week. She rejects about a third of them. Generating isn't the problem — the problem is that to reject a shot at second ten, she has to wait three minutes for the whole render to finish.

## 2 · First frame (0:18–0:52)

I paste the brief and hit New spot. Every scene that finishes is transcoded, cut into HLS segments, and each segment lands in Backblaze B2 as its own object, with the playlist regenerated behind it. Within seconds she's watching second zero while the last scene is still being generated. That's roughly four times earlier on screen than waiting for the render.

## 3 · Reject mid-render (0:52–1:20)

I reject at second fifteen: “the logo is unreadable in the detail shot.” That note goes straight into the prompt for the AgentLoop's next pass. The rejected take drops into rejected/ in the bucket, the new run hangs off the previous one through parent_run_id — the manifest keeps the whole chain — and the refined take joins the same playlist, so it arrives live with no reload and no second link.

## 4 · Failover on camera (1:20–1:44)

I kill the video provider live. Genblaze raises a real MODEL_ERROR — not a timeout, which is the only thing fallback_models reacts to — and the pipeline falls over from pixverse-v5.6 to seedance-2-0 on its own. Zero human actions.

## 5 · Approve and provenance (1:44–2:18)

I approve. The master is concatenated, the provenance manifest is embedded inside the MP4 itself — in Genblaze's uuid box — and it goes to approved/ under Object Lock in GOVERNANCE mode for thirty days. From here, not even a lifecycle rule can delete that version. And genblaze verify proves it: canonical hash checks out, manifest embedded, auditable deliverable.

## 6 · Architecture and close (2:18–3:00)

All of this rests on two things. B2: incremental HLS served from the bucket segment by segment, Event Notifications signed with HMAC, Object Lock in GOVERNANCE mode that lifecycle rules cannot touch, daysFromStartingToCancelingUnfinishedLargeFiles so a dead render never leaves an orphaned multipart, and read-only application keys so an outside reviewer never holds a key that can write. And Genblaze: a pipeline per scene with fallback_models, an AgentLoop with an Evaluator and a real vision judge, FFmpeg fan-in, manifest_lock, ObjectStorageSink and replay. We also left three pull requests and an issue open on the Genblaze repo, for the bugs we hit building this.

---

**375 palabras** · a 145 wpm son ~2.6 min de voz pura.
El resto de los 3:00 es silencio sobre planos: no llenes los huecos, dejalos respirar.
