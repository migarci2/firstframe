"""Pipeline de una escena: los 4 steps del PLAN §4.

    step 0  keyframe    imagen desde el brief          (fallback_models)
    step 1  voiceover    audio desde el texto           (modalidad distinta)
    step 2  clip         imagen -> video, input_from=[0], ChaosWrapper + fallback_models
    step 3  composite    FFmpegCompositor, input_from=[1, 2]  <- FAN-IN real

El fan-in del step 3 es lo que hace que esto sea un grafo y no una lista:
`FFmpegCompositor` exige un asset `video/` y uno `audio/` en `step.inputs`, y
`input_from=[1, 2]` es la unica forma de darle los dos.

Encima del pipeline va un `AgentLoop` (max_iterations=2) con un
`ThresholdEvaluator` cableado a `judge.judge_frame`. Si el juez de vision
suspende el keyframe, la escena entera se relanza con el prompt refinado con
la razon del juez, y `AgentLoop` encadena las iteraciones por `parent_run_id`
(`Pipeline.from_result`). Se evalua la escena completa y no solo el step 0
porque asi los indices de `input_from` siguen siendo los del PLAN y el manifest
de cada intento es autocontenido — es tambien lo que cuenta el guion del video
("escena relanzada", no "keyframe relanzado").

Reglas duras aplicadas aqui:
  - `Pipeline(..., preflight=False)` SIEMPRE (el preflight usa validate_model(),
    que esta invertido, #248).
  - `PromptTemplate(template=...)`, nunca posicional.
  - `.cache(StepCache(dir))` fluido, nunca `run(cache=...)`.
  - Mocks desde `genblaze_core`, nunca desde `genblaze_core.testing`.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from genblaze import (
    AgentContext,
    AgentLoop,
    FFmpegCompositor,
    Modality,
    Pipeline,
    PipelineResult,
    PromptTemplate,
    StepCache,
    StepType,
)

from pipeline import providers as P
from pipeline.judge import DEFAULT_THRESHOLD, FrameEvaluator, frame_evaluator

logger = logging.getLogger("firstframe.scenes")

# --- Modelos del camino de demo (PLAN §4) -----------------------------------
IMAGE_MODEL = "black-forest-labs/flux.1-schnell"
IMAGE_FALLBACKS = ["stabilityai/stable-diffusion-3-5-large-turbo"]
AUDIO_MODEL = "tts-1"
VIDEO_MODEL = "pixverse-v5.6"
VIDEO_FALLBACKS = ["seedance-2-0"]
CHAOS_KEY = "gmicloud"          # el nombre que mata `POST /api/chaos`
DRAFT_WATERMARK = "DRAFT - FirstFrame"

# --- Plantillas de prompt ----------------------------------------------------
# PromptTemplate(template=...) SIEMPRE por kwarg, nunca posicional. Y hay que
# render()arlas antes de pasarlas a step(): el SDK rechaza una plantilla sin
# renderizar fuera de batch_run() ("Step prompt is a PromptTemplate but was not
# rendered"), asi que la plantilla vive aqui y el step recibe el texto final.
KEYFRAME_TEMPLATE = PromptTemplate(template=(
    "Cinematic product still. First frame of scene {n} ({title}) of a short ad spot. "
    "Shot: {shot}. Brand context: {brief}. "
    "16:9, sharp focus on the product, legible label, clean uncluttered "
    "composition, soft studio lighting.{refine}"
))
VOICEOVER_TEMPLATE = PromptTemplate(template=(
    "{line}"
))
CLIP_TEMPLATE = PromptTemplate(template=(
    "Animate the still into a {seconds}-second shot: {motion}. "
    "Steady camera, single continuous take, product centred, label legible."
))
REFINE_TEMPLATE = PromptTemplate(template=(
    "\n\nThe previous attempt was rejected by the quality judge: {feedback}. "
    "Fix exactly that."
))


@dataclass(frozen=True)
class Scene:
    """Una escena del spot. La produce `runner.plan_scenes()`."""

    n: int
    title: str
    keyframe_prompt: str
    voiceover: str
    clip_prompt: str
    seconds: float = 4.0

    @property
    def slug(self) -> str:
        return f"scene-{self.n}"


@dataclass
class ProviderSet:
    """Providers resueltos para una escena. `mode` sale en el manifest."""

    image: object
    audio: object
    video: object
    compositor: FFmpegCompositor
    image_model: str = IMAGE_MODEL
    audio_model: str = AUDIO_MODEL
    video_model: str = VIDEO_MODEL
    image_fallbacks: list[str] = field(default_factory=lambda: list(IMAGE_FALLBACKS))
    video_fallbacks: list[str] = field(default_factory=lambda: list(VIDEO_FALLBACKS))
    mode: str = "mock"
    notes: list[str] = field(default_factory=list)


def demo_mode() -> str:
    """`DEMO_MODE=mock|real`. Default mock: no hay proveedor de media disponible."""
    return os.environ.get("DEMO_MODE", "mock").strip().lower()


def resolve_providers(scene: Scene, workdir: str | Path, *, mock: bool | None = None
                      ) -> ProviderSet:
    """Elige providers reales o mocks. Los reales se enchufan solo por env.

    Cada provider real se activa unicamente si su libreria importa Y su clave
    esta en el entorno. Si falta, ese hueco cae al mock con una nota — asi un
    entorno a medias produce un run "mixed" en vez de reventar.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    want_real = (mock is False) or (mock is None and demo_mode() == "real")

    label = f"S{scene.n} {scene.title}"
    ps = ProviderSet(
        image=P.mock_image_provider(workdir, label=label),
        audio=P.mock_audio_provider(workdir, seconds=scene.seconds,
                                    tone=280 + 40 * scene.n),
        video=P.mock_video_provider(workdir, seconds=scene.seconds, label=label),
        compositor=FFmpegCompositor(output_dir=workdir),
        mode="mock",
    )
    if not want_real:
        return ps

    real_bits = 0
    if os.environ.get("NVIDIA_API_KEY"):
        try:
            from genblaze_nvidia import NvidiaImageProvider

            ps.image = NvidiaImageProvider()
            real_bits += 1
        except Exception as exc:  # noqa: BLE001 - conector opcional
            ps.notes.append(f"NIM imagen no disponible ({exc}); mock")
    else:
        ps.notes.append("NVIDIA_API_KEY ausente; keyframe en mock")

    if os.environ.get("OPENAI_API_KEY"):
        try:
            from genblaze_openai import OpenAITTSProvider

            ps.audio = OpenAITTSProvider()
            real_bits += 1
        except Exception as exc:  # noqa: BLE001
            ps.notes.append(f"OpenAI TTS no disponible ({exc}); mock")
    else:
        # Nunca audio de GMICloud: roto entero (#251).
        ps.notes.append("OPENAI_API_KEY ausente; voiceover en mock")

    if os.environ.get("GMI_API_KEY"):
        try:
            from genblaze_gmicloud import GMICloudVideoProvider

            ps.video = GMICloudVideoProvider()
            real_bits += 1
        except Exception as exc:  # noqa: BLE001
            ps.notes.append(f"GMICloud video no disponible ({exc}); mock")
    else:
        ps.notes.append("GMI_API_KEY ausente; clip en mock")

    ps.mode = "real" if real_bits == 3 else ("mixed" if real_bits else "mock")
    return ps


def keyframe_prompt(scene: Scene, brief: str, feedback: str | None,
                    iteration: int) -> str:
    """Prompt del keyframe, con la razon del juez inyectada al refinar."""
    refine = ""
    if feedback and iteration > 0:
        refine = REFINE_TEMPLATE.render(feedback=feedback)
    return KEYFRAME_TEMPLATE.render(n=scene.n, title=scene.title,
                                    shot=scene.keyframe_prompt,
                                    brief=brief.strip(), refine=refine)


def build_scene_pipeline(
    scene: Scene,
    job: str,
    *,
    brief: str = "",
    workdir: str | Path | None = None,
    ctx: AgentContext | None = None,
    parent: PipelineResult | None = None,
    mock: bool | None = None,
    providers: ProviderSet | None = None,
    cache_dir: str | Path | None = ".cache/steps",
) -> Pipeline:
    """Construye el pipeline de 4 steps de una escena.

    `ctx` lo pasa el `AgentLoop` en cada iteracion; su `last_evaluation.feedback`
    es lo que refina el prompt del keyframe.
    `parent` encadena esta escena con la anterior por `parent_run_id`.
    """
    workdir = Path(workdir or f"runs/{job}/{scene.slug}")
    workdir.mkdir(parents=True, exist_ok=True)
    ps = providers or resolve_providers(scene, workdir, mock=mock)
    iteration = ctx.iteration if ctx else 0
    feedback = ctx.last_evaluation.feedback if (ctx and ctx.last_evaluation) else None

    pipe = Pipeline(
        name=f"{job}/{scene.slug}",
        # preflight=False SIEMPRE: el default usa validate_model(), invertido (#248).
        preflight=False,
    )
    pipe.metadata(job=job, scene=scene.n, scene_title=scene.title,
                  iteration=iteration, provider_mode=ps.mode,
                  provider_notes="; ".join(ps.notes) or None)
    if parent is not None:
        # Lineage entre escenas. El AgentLoop hace lo mismo entre iteraciones.
        pipe.from_result(parent)
    if cache_dir:
        # Cache FLUIDA. `run(cache=...)` no existe.
        pipe.cache(StepCache(cache_dir))

    # --- step 0: keyframe -----------------------------------------------------
    pipe.step(
        ps.image,
        model=ps.image_model,
        prompt=keyframe_prompt(scene, brief, feedback, iteration),
        modality=Modality.IMAGE,
        fallback_models=ps.image_fallbacks,
        metadata={"role": "keyframe", "scene": scene.n, "refined": bool(feedback)},
    )

    # --- step 1: voiceover ----------------------------------------------------
    pipe.step(
        ps.audio,
        model=ps.audio_model,
        prompt=VOICEOVER_TEMPLATE.render(line=scene.voiceover),
        modality=Modality.AUDIO,
        # El texto va en metadata: no existe Asset.text.
        metadata={"role": "voiceover", "scene": scene.n, "text": scene.voiceover},
    )

    # --- step 2: clip (imagen -> video) --------------------------------------
    pipe.step(
        # ChaosWrapper lanza MODEL_ERROR real -> es lo unico que dispara
        # fallback_models (un timeout de transporte NO lo hace).
        P.ChaosWrapper(ps.video, key=CHAOS_KEY, guarded_models=[ps.video_model]),
        model=ps.video_model,
        prompt=CLIP_TEMPLATE.render(seconds=f"{scene.seconds:g}",
                                    motion=scene.clip_prompt),
        modality=Modality.VIDEO,
        input_from=[0],
        fallback_models=ps.video_fallbacks,
        metadata={"role": "clip", "scene": scene.n, "chaos_key": CHAOS_KEY},
        duration=scene.seconds,
    )

    # --- step 3: composite (FAN-IN) ------------------------------------------
    pipe.step(
        ps.compositor,
        model="ffmpeg-mux",
        modality=Modality.VIDEO,
        step_type=StepType.MIX,
        # FAN-IN: voiceover (1) + clip (2) en el mismo step.inputs.
        input_from=[1, 2],
        metadata={"role": "composite", "scene": scene.n},
    )
    return pipe


def build_scene_agent(
    scene: Scene,
    job: str,
    brief: str,
    *,
    workdir: str | Path | None = None,
    parent: PipelineResult | None = None,
    mock: bool | None = None,
    providers: ProviderSet | None = None,
    threshold: float | None = None,
    max_iterations: int = 2,
    cache_dir: str | Path | None = ".cache/steps",
) -> tuple[AgentLoop, FrameEvaluator]:
    """`AgentLoop` sobre la escena, juzgada por el juez de vision.

    Devuelve tambien el `FrameEvaluator` para poder leer los veredictos
    (score + razon) despues del run y meterlos en el manifest / la UI.
    """
    if threshold is None:
        threshold = float(os.environ.get("JUDGE_THRESHOLD", DEFAULT_THRESHOLD))
    evaluator, fe = frame_evaluator(f"{brief}\n\nScene {scene.n}: {scene.title}",
                                    threshold=threshold)

    def factory(ctx: AgentContext) -> Pipeline:
        return build_scene_pipeline(
            scene, job, brief=brief, workdir=workdir, ctx=ctx,
            # Solo la iteracion 0 cuelga de la escena anterior; el AgentLoop
            # encadena las siguientes con from_result(prior).
            parent=parent if ctx.iteration == 0 else None,
            mock=mock, providers=providers, cache_dir=cache_dir,
        )

    loop = AgentLoop(factory, evaluator, max_iterations=max_iterations,
                     stop_on_pipeline_failure=True)
    return loop, fe


def composite_asset(result: PipelineResult):
    """El mp4 final de la escena: ultimo asset video/mp4 del run."""
    for step in reversed(result.run.steps):
        for asset in reversed(step.assets):
            if (asset.media_type or "") == "video/mp4":
                return asset
    return None


def normalize_scene(src: str | Path, dest: str | Path, *,
                    label: str = DRAFT_WATERMARK) -> Path:
    """Re-encode a los parametros canonicos de §2 + marca de agua.

    `FFmpegCompositor` muxea con `-c copy`: hereda los parametros de sus
    entradas y no puede garantizar que las N escenas salgan identicas, y
    `FFmpegTransform overlay_text` re-encodea con los defaults de ffmpeg.
    Este paso, uno por escena, es lo que hace que `server/assembler.py` pueda
    fragmentar y concatenar sin re-encodear.
    """
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = [P.ffmpeg_bin(), "-loglevel", "error", "-y", "-i", str(src)]
    if label:
        args += ["-vf", f"drawtext=text='{P._esc(label)}':fontsize=28:x=30:"
                        f"y=30:fontcolor=white@0.85:box=1:boxcolor=black@0.35:boxborderw=10"]
    args += [*P.CANONICAL_VIDEO_ARGS, *P.CANONICAL_AUDIO_ARGS,
             "-movflags", "+faststart", str(dest)]
    subprocess.run(args, capture_output=True, text=True, check=True)
    return dest


def video_params(path: str | Path) -> str:
    """Firma de parametros de un mp4. Igual en todas las escenas = concatenable."""
    out = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "stream=codec_name,width,height,r_frame_rate,pix_fmt,sample_rate,channels",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout
    return "|".join(sorted(line.strip() for line in out.strip().splitlines()))


def demo() -> None:
    """Autocomprobacion: 2 escenas en mock, 4 steps, fan-in y failover reales."""
    import shutil
    import tempfile

    from genblaze_core.models.enums import StepStatus

    from pipeline import chaos

    tmp = Path(tempfile.mkdtemp(prefix="scenes-demo-"))
    prev_chaos = os.environ.get("CHAOS_FILE")
    prev_judge = os.environ.get("JUDGE_THRESHOLD")
    os.environ["CHAOS_FILE"] = str(tmp / "chaos.json")
    # Umbral 0 -> el AgentLoop pasa a la primera y la demo no depende de la red.
    os.environ["JUDGE_THRESHOLD"] = "0.0"
    try:
        scenes = [
            Scene(1, "apertura", "producto sobre marmol", "Presentamos el serum.",
                  "camara acercandose", seconds=1.0),
            Scene(2, "detalle", "primer plano de la etiqueta", "Formula ligera.",
                  "giro lento", seconds=1.0),
        ]
        finals: list[Path] = []
        parent = None
        for scene in scenes:
            wd = tmp / scene.slug
            loop, fe = build_scene_agent(scene, "demojob", "spot de un serum",
                                         workdir=wd, parent=parent, mock=True,
                                         cache_dir=None)
            res = loop.run(raise_on_failure=True)
            parent = res.final

            steps = res.final.run.steps
            assert len(steps) == 4, f"esperaba 4 steps, hay {len(steps)}"
            assert all(s.status == StepStatus.SUCCEEDED for s in steps), \
                [(s.step_index, s.status, s.error) for s in steps]
            roles = [s.metadata.get("role") for s in steps]
            assert roles == ["keyframe", "voiceover", "clip", "composite"], roles

            # FAN-IN real: el compositor recibio audio Y video.
            comp_inputs = {(a.media_type or "").split("/")[0] for a in steps[3].inputs}
            assert comp_inputs == {"audio", "video"}, comp_inputs

            out = composite_asset(res.final)
            assert out is not None, "la escena no produjo mp4"
            src = Path(out.url.replace("file://", ""))
            finals.append(normalize_scene(src, tmp / f"{scene.slug}.mp4"))
            assert fe.last() is not None, "el juez deberia haber emitido veredicto"

        # Parametros identicos entre escenas -> concatenable sin re-encode.
        sigs = {video_params(f) for f in finals}
        assert len(sigs) == 1, f"escenas con parametros distintos: {sigs}"
        assert "h264" in next(iter(sigs)) and "1280" in next(iter(sigs)), sigs

        # Lineage: la escena 2 cuelga de la 1.
        assert parent.run.parent_run_id, "falta parent_run_id entre escenas"

        # --- chaos: el clip cae y salta a seedance --------------------------
        chaos.kill(CHAOS_KEY)
        loop, _ = build_scene_agent(scenes[0], "demojob", "spot", workdir=tmp / "chaos",
                                    mock=True, cache_dir=None, max_iterations=1)
        res = loop.run(raise_on_failure=True)
        clip = res.final.run.steps[2]
        assert clip.status == StepStatus.SUCCEEDED, (clip.status, clip.error)
        assert clip.model == VIDEO_FALLBACKS[0], f"no salto el failover: {clip.model}"
        assert clip.metadata.get("fallback_from") == VIDEO_MODEL, clip.metadata
        chaos.revive(CHAOS_KEY)
    finally:
        for key, val in (("CHAOS_FILE", prev_chaos), ("JUDGE_THRESHOLD", prev_judge)):
            os.environ.pop(key, None)
            if val is not None:
                os.environ[key] = val
        shutil.rmtree(tmp, ignore_errors=True)

    print("demo OK: 4 steps, fan-in audio+video en el compositor, parametros "
          f"identicos entre escenas, lineage por parent_run_id y failover "
          f"{VIDEO_MODEL} -> {VIDEO_FALLBACKS[0]} con el chaos activo")


if __name__ == "__main__":
    demo()
