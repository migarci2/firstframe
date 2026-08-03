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

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import SyncProvider
from genblaze_core.runnable.config import RunnableConfig

from pipeline import prompts as PR
from pipeline import providers as P
from pipeline.free_provider import PollinationsProvider, _image_size
from pipeline.judge import DEFAULT_THRESHOLD, FrameEvaluator, frame_evaluator
from pipeline.kenburns import (
    KEN_BURNS_FALLBACK,
    KEN_BURNS_MODEL,
    KenBurnsProvider,
    move_for,
)

logger = logging.getLogger("firstframe.scenes")

# --- Modelos del camino de demo (PLAN §4) -----------------------------------
IMAGE_MODEL = "black-forest-labs/flux.1-schnell"
IMAGE_FALLBACKS = ["stabilityai/stable-diffusion-3-5-large-turbo"]
AUDIO_MODEL = "tts-1"
VIDEO_MODEL = "pixverse-v5.6"
VIDEO_FALLBACKS = ["seedance-2-0"]
CHAOS_KEY = "gmicloud"          # el nombre que mata `POST /api/chaos`
DRAFT_WATERMARK = "DRAFT - FirstFrame"

# --- Modo `free`: generacion REAL sin tarjeta (PLAN §0) ----------------------
# Pollinations para el keyframe (imagen de verdad, ~45 s en el tier anonimo) y
# Ken Burns para convertirlo en plano. Ningun modelo de video gratis existe hoy;
# lo honesto es que el manifest diga "kenburns-2.5d" y no "pixverse".
FREE_IMAGE_MODEL = "flux"
FREE_IMAGE_FALLBACKS = ["turbo"]
FREE_VIDEO_MODEL = KEN_BURNS_MODEL
FREE_VIDEO_FALLBACKS = [KEN_BURNS_FALLBACK]

# Los keyframes viven bajo tmp porque `ObjectStorageSink` SOLO sube file:// bajo
# tmp. El corpus (copia persistente, ver `KeyframeCorpus`) puede vivir donde sea.
KEYFRAME_TMP = Path(tempfile.gettempdir()) / "firstframe-keyframes"
DEFAULT_CORPUS = "data/keyframes"

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

# El prompt de generacion REAL (free y real) ya NO se arma aqui: lo construye
# `pipeline/prompts.py` desde el brief del usuario. El motivo esta entero en el
# docstring de ese fichero; el resumen es que la version que vivia aqui pegaba
# el brief CRUDO delante del plano ("serum facial premium, marmol blanco..."),
# y con eso el generador devolvia un primer plano de una cara: `facial` era el
# unico sustantivo que el text encoder reconocia, y `no people` — una negacion,
# que CLIP no sabe aplicar — remataba metiendo *gente* en el embedding.
#
# `negative_prompt` viaja como campo de Step (el pipeline lo saca de params) y
# entra en la clave de cache, asi que cambiarlo invalida el corpus a proposito.
FREE_NEGATIVE_PROMPT = PR.NEGATIVE_PROMPT


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


GEN_MODES = ("mock", "free", "real")


def demo_mode() -> str:
    """`DEMO_MODE=mock|free|real`. Se mantiene por compatibilidad con el server."""
    return os.environ.get("DEMO_MODE", "mock").strip().lower()


def gen_mode() -> str:
    """Modo de generacion: `GEN_MODE=mock|free|real` (default mock).

    Tres modos, no dos:
      mock  ffmpeg testsrc2. Sin red, instantaneo, es lo que corren los demo().
      free  generacion de imagen REAL (Pollinations) + Ken Burns. Sin tarjeta,
            sin claves, ~45 s por escena.
      real  conectores de pago/con clave (NIM, OpenAI, GMICloud).

    `DEMO_MODE` se sigue leyendo como respaldo porque es lo que ya exporta el
    Dockerfile y `server/jobs.py` (que ademas fuerza mock=True cuando vale
    "mock": por eso desplegar en free es cambiar esa variable y nada mas).
    """
    raw = (os.environ.get("GEN_MODE") or os.environ.get("DEMO_MODE") or "").strip().lower()
    if raw not in GEN_MODES:
        if raw:
            logger.warning("GEN_MODE=%r no es %s; uso mock", raw, "|".join(GEN_MODES))
        return "mock"
    return raw


# --- Corpus de keyframes ------------------------------------------------------

class KeyframeCorpus:
    """Cache persistente de imagenes generadas, indexada por prompt+seed+modelo.

    A 45 s por imagen, regenerar en cada prueba es inviable: sin esto no se
    puede ni ensayar la demo. La `StepCache` del SDK ya evita repetir un step,
    pero su directorio va namespaceado POR JOB (`runner.run_job`) — dos jobs
    distintos con el mismo brief volverian a pagar los 45 s. Este corpus es
    transversal a los jobs y sobrevive a un borrado de /tmp o a un
    redespliegue: por eso `--pregenerate` puede dejar la demo cargada.

    Layout: `<dir>/index.json` (clave -> fichero) + los ficheros, con el mismo
    nombre content-addressed que usa `PollinationsProvider`.
    """

    def __init__(self, directory: str | Path | None = None) -> None:
        self.dir = Path(directory or os.environ.get("KEYFRAME_CORPUS") or DEFAULT_CORPUS)
        self.index_path = self.dir / "index.json"

    @staticmethod
    def key(prompt: str, *, model: str | None, seed: Any, width: int, height: int,
            negative_prompt: str | None) -> str:
        blob = json.dumps({"prompt": prompt, "model": model, "seed": seed,
                           "width": width, "height": height,
                           "negative_prompt": negative_prompt},
                          sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def _index(self) -> dict[str, str]:
        try:
            return json.loads(self.index_path.read_text())
        except (OSError, ValueError):
            return {}

    def get(self, key: str) -> Path | None:
        name = self._index().get(key)
        if not name:
            return None
        path = self.dir / name
        return path if path.is_file() else None

    def put(self, key: str, path: str | Path) -> Path:
        path = Path(path)
        self.dir.mkdir(parents=True, exist_ok=True)
        dest = self.dir / path.name
        if not dest.is_file():
            shutil.copy2(path, dest)
        index = self._index()
        index[key] = dest.name
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, indent=1, sort_keys=True))
        tmp.replace(self.index_path)   # atomico: nunca un index a medias
        return dest

    def __len__(self) -> int:
        return len(self._index())


class CachedPollinations(PollinationsProvider):
    """`PollinationsProvider` + corpus persistente + seed deterministica.

    Dos arreglos sobre el provider base, ninguno de los cuales toca su fichero:

    1. **La seed.** `Pipeline` SACA `seed` de `params` y la pone en `Step.seed`
       (pipeline.py:1207), asi que `PollinationsProvider`, que la lee de
       `step.params`, nunca la veria y cada re-render daria una imagen distinta.
       Aqui se re-inyecta en params SOLO durante la llamada y se restaura al
       salir: si se dejara mutada, la clave que usa `StepCache.put` dejaria de
       coincidir con la de `get` y el cache del SDK no acertaria jamas.

    2. **El corpus.** Antes de pagar 45 s se mira si esa imagen ya existe. Es
       cache de PROVIDER, no de step: acierta aunque cambie el job, el numero
       de escena o el resto del pipeline.
    """

    def __init__(self, *, corpus: KeyframeCorpus | str | Path | None = None,
                 **kwargs: Any) -> None:
        kwargs.setdefault("output_dir", KEYFRAME_TMP)
        super().__init__(**kwargs)
        self.corpus = corpus if isinstance(corpus, KeyframeCorpus) else KeyframeCorpus(corpus)

    def _cache_key(self, step: Step, seed: Any) -> str:
        params = step.params or {}
        return KeyframeCorpus.key(
            str(step.prompt or ""), model=step.model, seed=seed,
            width=int(params.get("width", self.width)),
            height=int(params.get("height", self.height)),
            negative_prompt=step.negative_prompt,
        )

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        original = step.params
        seed = (original or {}).get("seed", step.seed)
        key = self._cache_key(step, seed)

        hit = self.corpus.get(key)
        if hit is not None:
            # El asset TIENE que apuntar bajo tmp: el sink rechaza cualquier
            # otra ruta. El corpus puede vivir en el repo, la copia no.
            self.output_dir.mkdir(parents=True, exist_ok=True)
            local = self.output_dir / hit.name
            if not local.is_file():
                shutil.copy2(hit, local)
            width, height = _image_size(local)
            asset = P.local_asset(local, width=width, height=height)
            asset.metadata = {"provider": self.name, "model": step.model,
                              "seed": seed, "cached": True,
                              "corpus": str(self.corpus.dir)}
            step.assets.append(asset)
            logger.info("keyframe desde el corpus (%s), 45 s ahorrados", local.name)
            return step

        if seed is not None and (original or {}).get("seed") is None:
            step.params = {**(original or {}), "seed": int(seed)}
        try:
            step = super().generate(step, config)
        finally:
            step.params = original   # ver docstring: no romper la clave de cache

        if step.assets:
            path = Path(step.assets[-1].url.removeprefix("file://"))
            if path.is_file():
                self.corpus.put(key, path)
        return step


def prompt_seed(text: str) -> int:
    """Seed deterministica a partir del prompt final.

    Del PROMPT y no de (job, escena): asi el mismo brief reutiliza la imagen
    aunque cambie el job, y un prompt refinado por el juez genera una imagen
    NUEVA (que es justo lo que pide el refinado).
    """
    return int(hashlib.sha256(text.encode()).hexdigest()[:8], 16) % (2 ** 31)


class GuardedImageProvider(SyncProvider):
    """Keyframe real con red de seguridad: si el proveedor se agota, mock.

    Por que
    -------
    El tier anonimo de Pollinations serializa por IP y admite UNA peticion en
    cola. Dos jobs a la vez (o alguien generando en otra terminal) y el
    segundo se come un 429. `PollinationsProvider` ya reintenta 4 veces con
    backoff honrando `Retry-After`, pero cuando agota los intentos lanza y
    `raise_on_failure=True` mata el JOB ENTERO: la UI se queda en "No se pudo
    terminar" y no hay video. Visto en vivo, no teorizado.

    Reintentar mas no arregla nada — la ranura sigue ocupada. Lo que arregla
    el sintoma es que agotar un proveedor NO pueda matar el job: esa escena
    cae al keyframe mock y el spot se termina. Una escena degradada es
    infinitamente mejor que un job muerto, y es lo que hace ya el resto de la
    app (claves ausentes -> `mixed`, video muerto -> `fallback_models`).

    Honestidad, que es la otra mitad del arreglo
    --------------------------------------------
    Degradar en silencio seria peor que fallar. Cada degradacion deja rastro
    en los tres sitios donde alguien puede mirar:

      - `step.model` pasa a ser el modelo mock: el manifest NO puede decir que
        esto lo genero flux.
      - `step.metadata`: `degraded`, `degraded_reason` y el par
        `fallback_from`/`fallback_model`, que es lo que `manifest.py` mete en
        `SceneRecord.fallbacks` y `runner.run_job` convierte en un evento
        `provider_failover` — o sea, sale solo en el feed y en el panel
        tecnico, sin tocar ni el runner ni el server.
      - `on_degrade`: sube `provider_mode` del run a `degraded` (ver
        `build_scene_pipeline`), asi que el manifest agregado tampoco lo
        esconde.

    MODEL_ERROR se re-lanza a proposito: es el UNICO codigo ante el que el
    `Pipeline` prueba `fallback_models=`. Tragarselo aqui desactivaria el
    failover de modelo, que es una demo aparte. Se degrada por lo demas
    (RATE_LIMIT, TIMEOUT, SERVER_ERROR...), que es justo lo que un 429 agotado
    produce.
    """

    name = "pollinations-guarded"

    def __init__(self, primary: Any, fallback: Any, *,
                 fallback_model: str = "mock-image",
                 on_degrade: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.primary = primary
        self.fallback = fallback
        self.fallback_model = fallback_model
        self.on_degrade = on_degrade
        self.degraded: str | None = None

    def get_capabilities(self) -> Any:
        return self.primary.get_capabilities()

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        try:
            return self.primary.generate(step, config)
        except ProviderError as exc:
            if exc.error_code is ProviderErrorCode.MODEL_ERROR:
                raise                      # que salte `fallback_models=` primero
            reason = f"{exc.error_code.name}: {exc}"[:200]
        except Exception as exc:  # noqa: BLE001 - nada puede matar el job aqui
            reason = f"UNEXPECTED: {exc}"[:200]

        failed_model = step.model or FREE_IMAGE_MODEL
        logger.warning("keyframe degradado a mock (%s se agoto): %s",
                       failed_model, reason)
        self.degraded = reason
        # El manifest tiene que poder decir que esto NO lo genero el modelo real.
        step.model = self.fallback_model
        step.metadata = {**(step.metadata or {}),
                         "degraded": True, "degraded_reason": reason,
                         "fallback_from": failed_model,
                         "fallback_model": self.fallback_model}
        if self.on_degrade is not None:
            self.on_degrade(failed_model, reason)
        step = self.fallback.generate(step, config)
        for asset in step.assets:
            asset.metadata = {**(asset.metadata or {}), "degraded": True,
                              "degraded_reason": reason}
        return step


def free_providers(scene: Scene, workdir: Path, ps: ProviderSet) -> ProviderSet:
    """Enchufa generacion real gratuita: Pollinations + Ken Burns."""
    # El mock que ya trae `ps.image` se recicla como red de seguridad en vez de
    # tirarse: es exactamente el keyframe que produciria un run mock.
    ps.image = GuardedImageProvider(CachedPollinations(), ps.image)
    ps.image_model = FREE_IMAGE_MODEL
    ps.image_fallbacks = list(FREE_IMAGE_FALLBACKS)
    ps.video = KenBurnsProvider(
        output_dir=workdir, seconds=scene.seconds,
        # scene.n-1: la escena 1 abre con push-in, la 2 panea, la 3 se aleja.
        scene=max(0, scene.n - 1),
        # Si el sink ya reescribio la url del keyframe a B2, se recupera por sha.
        image_dirs=[KEYFRAME_TMP, workdir],
    )
    ps.video_model = FREE_VIDEO_MODEL
    ps.video_fallbacks = list(FREE_VIDEO_FALLBACKS)
    ps.mode = "free"
    ps.notes.append("keyframe REAL con Pollinations (gratis, ~45 s en el tier anonimo)")
    ps.notes.append("si Pollinations se agota (429), el keyframe cae a mock y el "
                    "job sigue; queda como provider_mode=degraded")
    ps.notes.append(f"clip por Ken Burns ffmpeg ({FREE_VIDEO_MODEL}), no hay video gratis")
    ps.notes.append("voiceover en mock: no hay TTS gratis sin tarjeta")
    return ps


def resolve_providers(scene: Scene, workdir: str | Path, *, mock: bool | None = None
                      ) -> ProviderSet:
    """Elige providers mock, free o reales. Ver `gen_mode()`.

    Cada provider real se activa unicamente si su libreria importa Y su clave
    esta en el entorno. Si falta, ese hueco cae al mock con una nota — asi un
    entorno a medias produce un run "mixed" en vez de reventar.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mode = gen_mode()
    if mock is True:
        mode = "mock"                                  # --mock manda sobre el env
    elif mock is False and mode == "mock":
        mode = "real"                                  # --real sin GEN_MODE

    label = f"S{scene.n} {scene.title}"
    ps = ProviderSet(
        image=P.mock_image_provider(workdir, label=label),
        audio=P.mock_audio_provider(workdir, seconds=scene.seconds,
                                    tone=280 + 40 * scene.n),
        video=P.mock_video_provider(workdir, seconds=scene.seconds, label=label),
        compositor=FFmpegCompositor(output_dir=workdir),
        mode="mock",
    )
    if mode == "mock":
        return ps
    if mode == "free":
        return free_providers(scene, workdir, ps)

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
                    iteration: int, *, mode: str = "mock") -> str:
    """Prompt del keyframe, con la razon del juez inyectada al refinar.

    Cuando hay un generador de imagen de verdad detras (`free` y `real`) el
    prompt se REESCRIBE desde el brief con `pipeline.prompts`: sujeto en ingles
    y siempre un objeto, ancla de estilo derivada del brief y compartida por las
    3 escenas, y arco apertura/detalle/heroe. `scene.keyframe_prompt` solo entra
    para rescatar la nota del revisor que le pega `runner.refine_scene`.

    En `mock` se deja la plantilla vieja tal cual: ahi no hay modelo que
    engañar (sale un testsrc2 de ffmpeg) y el manifest de los runs mock ya
    grabados sigue cuadrando.
    """
    refine = ""
    if feedback and iteration > 0:
        refine = REFINE_TEMPLATE.render(feedback=feedback)
    if mode in ("free", "real", "mixed"):
        return PR.keyframe_prompt(brief, n=scene.n, title=scene.title,
                                  extra=refine,
                                  scene_prompt_hint=scene.keyframe_prompt)
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
    if isinstance(ps.image, GuardedImageProvider):
        # `Pipeline.metadata()` es acumulativo y no se vuelca a `Run.metadata`
        # hasta `_finalize()`, DESPUES de correr los steps: por eso una llamada
        # desde dentro del step 0 todavia llega al manifest. Es lo que permite
        # que una degradacion en caliente aparezca como provider_mode=degraded
        # sin tocar el runner ni el server.
        def _degraded(failed_model: str, reason: str) -> None:
            ps.mode = "degraded"
            ps.notes.append(f"keyframe degradado a mock: {failed_model} -> {reason}")
            pipe.metadata(provider_mode="degraded",
                          provider_notes="; ".join(ps.notes),
                          degraded_reason=reason)

        ps.image.on_degrade = _degraded
    if parent is not None:
        # Lineage entre escenas. El AgentLoop hace lo mismo entre iteraciones.
        pipe.from_result(parent)
    if cache_dir:
        # Cache FLUIDA. `run(cache=...)` no existe.
        pipe.cache(StepCache(cache_dir))

    # --- step 0: keyframe -----------------------------------------------------
    kf_prompt = keyframe_prompt(scene, brief, feedback, iteration, mode=ps.mode)
    kf_params: dict[str, Any] | None = None
    if ps.mode == "free":
        # `seed` y `negative_prompt` los saca el pipeline de params y los sube a
        # campos de Step; los dos entran en la clave de `StepCache` y en la del
        # corpus, asi que el mismo prompt reutiliza imagen y el prompt refinado
        # por el juez genera una nueva.
        kf_params = {"seed": prompt_seed(kf_prompt),
                     "negative_prompt": FREE_NEGATIVE_PROMPT}
    pipe.step(
        ps.image,
        model=ps.image_model,
        prompt=kf_prompt,
        modality=Modality.IMAGE,
        fallback_models=ps.image_fallbacks,
        params=kf_params,
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

        # --- modo free: cableado, prompts, corpus y camino imagen -> video ---
        # Sin red: se sustituye SOLO el provider de imagen por el mock y se deja
        # el resto del modo free intacto (Ken Burns, seeds, negative prompt).
        prev_gen = os.environ.get("GEN_MODE")
        os.environ["GEN_MODE"] = "free"
        try:
            assert gen_mode() == "free"
            os.environ["GEN_MODE"] = "no-existe"
            assert gen_mode() == "mock", "un GEN_MODE invalido tiene que degradar a mock"
            os.environ["GEN_MODE"] = "free"

            sc = scenes[0]
            wd = tmp / "free"
            ps = resolve_providers(sc, wd)
            assert ps.mode == "free", ps.mode
            assert isinstance(ps.image, GuardedImageProvider), type(ps.image)
            assert isinstance(ps.image.primary, CachedPollinations)
            assert isinstance(ps.video, KenBurnsProvider), type(ps.video)
            assert ps.video_model == KEN_BURNS_MODEL, ps.video_model
            assert ps.video_fallbacks == [KEN_BURNS_FALLBACK], ps.video_fallbacks
            # --mock manda sobre el env: no romper el camino sin red.
            assert resolve_providers(sc, wd, mock=True).mode == "mock"

            # Prompt de free: lo arma pipeline.prompts desde el brief. Ni el
            # nombre de marca (el modelo intentaria escribirlo), ni el numero
            # de escena, ni una negacion de persona en el prompt POSITIVO.
            text = keyframe_prompt(sc, "serum para la marca ACME, marmol blanco",
                                   None, 0, mode="free")
            assert "ACME" not in text, text
            assert "scene 1" not in text.lower(), text
            assert "no people" not in text and "face" not in text.lower(), text
            assert "Still life product photography" in text, text
            # ...y fidelidad: lo que pide el brief tiene que llegar al modelo.
            assert "dropper bottle" in text and "white marble" in text, text
            # El arco: tres escenas, tres planos distintos, mismo ancla.
            arc = [keyframe_prompt(Scene(i, t, "", "", ""), "serum, marmol blanco",
                                   None, 0, mode="free")
                   for i, t in ((1, "apertura"), (2, "detalle"), (3, "cierre"))]
            assert len({a.split(".")[0] for a in arc}) == 3, arc
            assert all(PR.parse_brief("serum, marmol blanco").palette in a
                       for a in arc)
            assert prompt_seed(text) == prompt_seed(text), "la seed no es deterministica"
            assert prompt_seed(text) != prompt_seed(text + "!"), "la seed no depende del prompt"

            # Corpus: ida y vuelta + clave sensible a la seed.
            corpus = KeyframeCorpus(tmp / "corpus")
            png = P.make_keyframe(tmp / "kf.png", label="corpus")
            k1 = KeyframeCorpus.key("p", model="flux", seed=1, width=1280,
                                    height=720, negative_prompt=None)
            k2 = KeyframeCorpus.key("p", model="flux", seed=2, width=1280,
                                    height=720, negative_prompt=None)
            assert k1 != k2 and corpus.get(k1) is None
            corpus.put(k1, png)
            assert corpus.get(k1) is not None and corpus.get(k2) is None
            assert corpus.get(k1).read_bytes() == png.read_bytes()
            assert len(KeyframeCorpus(tmp / "corpus")) == 1, "el index no persiste"

            # --- 429 agotado: la escena degrada, el JOB NO muere -------------
            # Es lo que se vio en vivo: el tier anonimo serializa por IP, dos
            # jobs a la vez y el segundo se comia un 429 que mataba el job
            # entero. Aqui se simula agotando el proveedor primario.
            class _Exhausted:
                def get_capabilities(self):
                    return None

                def generate(self, step, config=None):
                    raise ProviderError(
                        "pollinations HTTP 429: Too Many Requests",
                        error_code=ProviderErrorCode.RATE_LIMIT, attempts=4)

            dwd = tmp / "degraded"
            dps = resolve_providers(sc, dwd)
            dps.image = GuardedImageProvider(
                _Exhausted(), P.mock_image_provider(dwd, label="degradado"))
            dres = build_scene_pipeline(sc, "degjob", brief="spot", workdir=dwd,
                                        providers=dps, cache_dir=None).run(
                                            raise_on_failure=True)
            dsteps = dres.run.steps
            assert all(st.status == StepStatus.SUCCEEDED for st in dsteps), \
                [(st.step_index, st.status, st.error) for st in dsteps]
            dkf = dsteps[0]
            assert dkf.assets, "la escena degradada tiene que producir keyframe"
            # 1. Honesto: el manifest no puede decir que esto lo genero flux.
            assert dkf.metadata.get("degraded") is True, dkf.metadata
            assert "429" in dkf.metadata.get("degraded_reason", ""), dkf.metadata
            assert dkf.model == "mock-image", dkf.model
            assert dres.run.metadata.get("provider_mode") == "degraded", \
                dres.run.metadata
            # 2. Visible: fallback_from/fallback_model son lo que manifest.py
            #    mete en SceneRecord.fallbacks y runner convierte en el evento
            #    provider_failover del feed.
            assert dkf.metadata.get("fallback_from") == FREE_IMAGE_MODEL
            assert dkf.metadata.get("fallback_model") == "mock-image"
            # 3. El spot se termina: fan-in y mp4 concatenable como cualquiera.
            assert composite_asset(dres) is not None, "el job degradado no dio mp4"
            # MODEL_ERROR NO se traga: es lo unico que dispara fallback_models.
            class _BadModel(_Exhausted):
                def generate(self, step, config=None):
                    raise ProviderError("modelo desconocido",
                                        error_code=ProviderErrorCode.MODEL_ERROR)

            guard = GuardedImageProvider(_BadModel(), P.mock_image_provider(dwd))
            try:
                guard.generate(Step(provider="x", model="flux",
                                    modality=Modality.IMAGE, prompt="p"))
                raise AssertionError("MODEL_ERROR tenia que propagarse")
            except ProviderError as exc:
                assert exc.error_code is ProviderErrorCode.MODEL_ERROR

            # El camino real de free salvo la llamada a la red: keyframe local
            # -> Ken Burns -> fan-in con la voz -> parametros canonicos.
            ps.image = P.mock_image_provider(wd, label="free")
            ps.image_model, ps.image_fallbacks = IMAGE_MODEL, []
            res = build_scene_pipeline(sc, "freejob", brief="spot", workdir=wd,
                                       providers=ps, cache_dir=None).run(
                                           raise_on_failure=True)
            steps = res.run.steps
            assert all(s.status == StepStatus.SUCCEEDED for s in steps), \
                [(s.step_index, s.status, s.error) for s in steps]
            kf = steps[0]
            assert kf.seed == prompt_seed(kf.prompt), (kf.seed, kf.prompt)
            assert kf.negative_prompt == FREE_NEGATIVE_PROMPT
            clip = steps[2].assets[0]
            assert clip.metadata["motion"] == move_for(sc.n - 1).name, clip.metadata
            assert clip.metadata["provider"] == "kenburns", clip.metadata
            free_mp4 = normalize_scene(
                Path(composite_asset(res).url.replace("file://", "")),
                tmp / "free.mp4")
            assert video_params(free_mp4) == video_params(finals[0]), \
                "una escena free no seria concatenable con una mock"
        finally:
            os.environ.pop("GEN_MODE", None)
            if prev_gen is not None:
                os.environ["GEN_MODE"] = prev_gen
    finally:
        for key, val in (("CHAOS_FILE", prev_chaos), ("JUDGE_THRESHOLD", prev_judge)):
            os.environ.pop(key, None)
            if val is not None:
                os.environ[key] = val
        shutil.rmtree(tmp, ignore_errors=True)

    print("demo OK: 4 steps, fan-in audio+video en el compositor, parametros "
          f"identicos entre escenas, lineage por parent_run_id, failover "
          f"{VIDEO_MODEL} -> {VIDEO_FALLBACKS[0]} con el chaos activo, modo free "
          f"cableado (corpus, seed por prompt y clip Ken Burns concatenable) y "
          f"un 429 agotado degradando la escena a mock sin matar el job")


if __name__ == "__main__":
    demo()
