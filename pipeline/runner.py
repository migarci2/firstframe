"""Orquestacion del job: plan de escenas -> N pipelines encadenados -> agregado.

`run_job()` es lo que llama el backend en background. Lo importante para el
producto es el callback: **`on_scene(path)` se invoca en cuanto una escena
termina**, no al final del job. Eso es lo que permite a `server/assembler.py`
ir fragmentando y subiendo mientras las escenas que faltan aun se generan, y
es de donde sale el "primer fotograma en 7 s vs render total 2:43".

Encadenado: la escena N cuelga de la N-1 por `parent_run_id`
(`Pipeline.from_result`), y dentro de cada escena el `AgentLoop` encadena sus
iteraciones igual. El manifest agregado publica ese arbol entero.

CLI:
    .venv/bin/python -m pipeline.runner --job demo1 --mock
    .venv/bin/python -m pipeline.runner --job demo1 --mock --chaos gmicloud
    .venv/bin/python -m pipeline.runner --job demo1 --mock --approve --verify

    # generacion de imagen REAL (gratis, sin tarjeta) + Ken Burns: ~45 s/escena
    GEN_MODE=free .venv/bin/python -m pipeline.runner --job real1 --scenes 3
    .venv/bin/python -m pipeline.runner --free --job real1 --frames /tmp/real-scenes

    # corpus de la demo: genera y cachea los briefs de ejemplo (una sola vez)
    .venv/bin/python -m pipeline.runner --pregenerate
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pipeline import chaos, manifest as M
from pipeline import providers as P
from pipeline.scenes import (
    CHAOS_KEY,
    DRAFT_WATERMARK,
    KeyframeCorpus,
    Scene,
    build_scene_agent,
    composite_asset,
    gen_mode,
    normalize_scene,
    resolve_providers,
    video_params,
)

logger = logging.getLogger("firstframe.runner")

DEFAULT_SCENES = 3          # PLAN §8 recorte 4: 6 -> 3 escenas
DEFAULT_SECONDS = 4.0

# Plantilla fija del plan de escenas. Es el camino por defecto: no depende de
# ninguna API y produce siempre el mismo numero de escenas.
#
# Los planos estan elegidos con el generador REAL delante (PLAN §0): Pollinations
# es bueno en planos generales, entornos, bodegones y macro, y MALO en primeros
# planos de cara (caras de plastico que delatan la IA al instante). Por eso no
# hay ni un beat con personas: donde el guion pedia "el producto en uso, manos
# en cuadro" ahora hay un bodegon del producto en su contexto. En mock da igual
# — pero en free es la diferencia entre un spot y una demo de IA cutre.
_BEATS: list[tuple[str, str, str, str]] = [
    ("apertura",
     "wide establishing shot of the space where the product lives, product small "
     "in frame on a clean surface, generous negative space, no people",
     "{brief_short}",
     "slow push-in towards the product"),
    ("detalle",
     "macro shot of the product surface, material and texture, dramatic side "
     "light, seamless backdrop",
     "Cada detalle cuenta.",
     "slow drift across the surface, shallow depth of field"),
    ("contexto",
     "still life of the product in a lifestyle setting, empty room, soft window "
     "light, no people",
     "Encaja donde vivas.",
     "lateral dolly across the scene"),
    ("materia",
     "overhead flat lay of the raw materials behind the product, arranged on "
     "stone, studio light",
     "Hecho de lo esencial.",
     "slow overhead rise"),
    ("beneficio",
     "abstract macro of the result the product delivers, droplets and light "
     "refraction, no people",
     "Resultados que se ven.",
     "slow rack focus onto the highlight"),
    ("cierre",
     "hero product shot centred on a plain gradient backdrop, soft floor "
     "reflection, studio lighting",
     "Disponible ya.",
     "static hero shot, slow zoom out"),
]

# --- Ancla de estilo ---------------------------------------------------------
# Las 3 escenas tienen que parecer del MISMO spot. Un generador de imagen sin
# memoria entre llamadas no lo hace solo: si no le atas paleta, luz y
# tratamiento, salen tres anuncios de tres marcas distintas. Esto es lo unico
# que comparten los tres prompts, y es lo que da continuidad visual.
_STYLE_ANCHORS: list[str] = [
    "Consistent style across the whole spot: warm neutral palette of sand, cream "
    "and terracotta, soft directional morning light, gentle haze, shallow depth "
    "of field, fine 35mm film grain, minimal uncluttered set",
    "Consistent style across the whole spot: cool monochrome palette of slate, "
    "glass and steel blue, crisp studio light with clean edge shadows, polished "
    "surfaces, high micro-contrast, modern uncluttered set",
    "Consistent style across the whole spot: deep moody palette of forest green, "
    "charcoal and amber, a single warm key light in darkness, volumetric haze, "
    "glossy reflections, cinematic contrast",
    "Consistent style across the whole spot: bright airy palette of white, pale "
    "blue and soft pastel, diffused overcast daylight, pale seamless backdrop, "
    "soft shadows, editorial minimalism",
]


def style_anchor(brief: str) -> str:
    """Ancla de estilo del spot. Deterministica por brief, distinta entre briefs.

    Deterministica para que re-ejecutar el mismo job acierte en el cache (y no
    pague otros 45 s por imagen); distinta entre briefs para que dos spots
    seguidos no salgan clonados en el video de la demo.
    """
    h = int(hashlib.sha256(" ".join(brief.split()).lower().encode()).hexdigest()[:8], 16)
    return _STYLE_ANCHORS[h % len(_STYLE_ANCHORS)]


def _beat_indices(n: int) -> list[int]:
    """Que beats coger para un spot de n escenas: SIEMPRE apertura y cierre.

    Con n=3 (el default) sale apertura -> detalle -> cierre: entorno, textura y
    heroe, que es un mini arco de verdad y no tres planos sueltos.
    """
    n = max(1, min(n, len(_BEATS)))
    if n == 1:
        return [0]
    return [0, *range(1, n - 1), len(_BEATS) - 1]


@dataclass
class JobResult:
    job_id: str
    brief: str
    scenes: list[M.SceneRecord] = field(default_factory=list)
    scene_paths: list[str] = field(default_factory=list)
    final_mp4: str | None = None
    aggregate: dict[str, Any] = field(default_factory=dict)
    aggregate_written: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    first_scene_ms: int = 0
    provider_mode: str = "mock"

    @property
    def failovers(self) -> list[dict[str, Any]]:
        return self.aggregate.get("failovers", [])


# --- Plan de escenas ---------------------------------------------------------

def plan_scenes(brief: str, n: int = DEFAULT_SCENES, *,
                seconds: float = DEFAULT_SECONDS,
                style: str | None = None) -> list[Scene]:
    """Plan por plantilla. Deterministico, sin red, siempre disponible.

    El ancla de estilo se cose DENTRO de `keyframe_prompt` en vez de vivir en un
    campo aparte: asi viaja gratis por `SceneRecord.spec`, y el refinado en
    caliente (`refine_scene`) reconstruye la escena con el mismo look sin tener
    que volver a calcularlo.
    """
    short = " ".join(brief.split())[:90]
    subject = short.rstrip(".")
    style = style or style_anchor(brief)
    out: list[Scene] = []
    for pos, i in enumerate(_beat_indices(n)):
        title, shot, line, motion = _BEATS[i]
        out.append(Scene(
            n=pos + 1,
            title=title,
            keyframe_prompt=f"{shot}; subject: {subject}. {style}",
            voiceover=line.format(brief_short=short),
            clip_prompt=motion,
            seconds=seconds,
        ))
    return out


def plan_scenes_llm(brief: str, n: int = DEFAULT_SCENES, *,
                    seconds: float = DEFAULT_SECONDS) -> list[Scene] | None:
    """Plan con NIM chat (gratis y verificado). None si falla: se usa la plantilla.

    Opcional a proposito: el camino por defecto no puede depender de una API.
    """
    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not api_key:
        return None
    import urllib.error
    import urllib.request

    prompt = (
        f"You are a commercial director. Break this ad brief into exactly {n} "
        f"scenes for a short product spot with a mini arc: establish, then a "
        f"detail, then a hero closing shot.\n\n"
        f"BRIEF: {brief.strip()}\n\n"
        "HARD RULES for every keyframe description:\n"
        "- Wide shots, still lifes, product and macro texture ONLY.\n"
        "- NEVER a human face, a portrait, a person or hands in frame.\n"
        "- No on-screen text, no readable logos, no brand names.\n\n"
        'Reply with ONLY a JSON object, no prose and no markdown fence, keys '
        'exactly: "style" (one sentence naming the palette, the lighting and the '
        'film treatment SHARED by every scene, so the spot looks like one piece) '
        'and "scenes" (array of objects with keys "title" (1-2 words), "keyframe" '
        '(visual description of the first frame), "voiceover" (one short spoken '
        'sentence in the language of the brief), "motion" (camera movement)).'
    )
    payload = {"model": "meta/llama-3.3-70b-instruct", "temperature": 0.4,
               "max_tokens": 900,
               "messages": [{"role": "user", "content": prompt}]}
    try:
        req = urllib.request.Request(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            raw = json.loads(resp.read().decode())["choices"][0]["message"]["content"]
        items, style = _parse_plan(raw)
        # Si el modelo se olvida del estilo, el ancla local: nunca sin ancla, o
        # las 3 escenas salen de tres anuncios distintos.
        style = style or style_anchor(brief)
        scenes = [
            Scene(n=i + 1,
                  title=str(it["title"])[:24],
                  # El ancla se cose en el prompt de cada escena, igual que en la
                  # plantilla: es lo que las hace parecer el mismo spot.
                  keyframe_prompt=f"{str(it['keyframe']).rstrip('. ')}. {style}",
                  voiceover=str(it["voiceover"]),
                  clip_prompt=str(it.get("motion", "slow push-in")),
                  seconds=seconds)
            for i, it in enumerate(items[:n])
        ]
        if scenes:
            logger.info("plan de escenas generado por NIM chat (%d escenas, "
                        "estilo: %s...)", len(scenes), style[:60])
            return scenes
    except Exception as exc:  # noqa: BLE001 - el plan LLM es un extra
        logger.warning("plan por LLM fallo (%s); uso la plantilla", exc)
    return None


def _parse_plan(raw: str) -> tuple[list[dict[str, Any]], str | None]:
    """Saca (escenas, estilo) de la respuesta del LLM.

    Acepta las dos formas: el objeto `{"style": ..., "scenes": [...]}` que se
    pide ahora y el array pelado que devolvia la version anterior (los modelos
    de 70B ignoran el formato de vez en cuando y no vale la pena tirar el plan
    por eso).
    """
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(raw[start:end + 1])
            if isinstance(data, dict) and isinstance(data.get("scenes"), list):
                return data["scenes"], (str(data["style"]).strip()
                                        if data.get("style") else None)
        except ValueError:
            pass
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("la respuesta del LLM no traia ni objeto ni array JSON")
    return json.loads(raw[start:end + 1]), None


# --- Utilidades --------------------------------------------------------------

def scene_to_spec(scene: Scene) -> dict[str, Any]:
    return {"n": scene.n, "title": scene.title,
            "keyframe_prompt": scene.keyframe_prompt,
            "voiceover": scene.voiceover, "clip_prompt": scene.clip_prompt,
            "seconds": scene.seconds}


def scene_from_spec(spec: dict[str, Any]) -> Scene:
    return Scene(n=int(spec["n"]), title=str(spec.get("title", "")),
                 keyframe_prompt=str(spec.get("keyframe_prompt", "")),
                 voiceover=str(spec.get("voiceover", "")),
                 clip_prompt=str(spec.get("clip_prompt", "")),
                 seconds=float(spec.get("seconds", DEFAULT_SECONDS)))


def _call_on_scene(on_scene, n: int, path: str, meta: dict[str, Any]) -> None:
    """Llama al callback con la aridad que acepte.

    El backend (`server/jobs.py`) espera `(n, path, meta)`; los tests y el CLI
    usan `(path)`. En vez de imponer una firma se introspecciona la suya, que
    es una linea y evita un adaptador en el otro lado.
    """
    import inspect

    try:
        params = inspect.signature(on_scene).parameters
        positional = [q for q in params.values()
                      if q.kind in (q.POSITIONAL_ONLY, q.POSITIONAL_OR_KEYWORD)]
        n_args = 3 if any(q.kind == q.VAR_POSITIONAL for q in params.values()) \
            else len(positional)
    except (TypeError, ValueError):
        n_args = 1
    if n_args >= 3:
        on_scene(n, path, meta)
    elif n_args == 2:
        on_scene(n, path)
    else:
        on_scene(path)


def media_workdir(job_id: str, slug: str) -> Path:
    """Directorio de trabajo de una escena, SIEMPRE bajo el temp del sistema.

    GOTCHA VERIFICADO: `ObjectStorageSink` solo sube ficheros `file://` que
    esten bajo `tempfile.gettempdir()` o `/tmp`
    (`genblaze_core/_utils.py:ALLOWED_FILE_ROOTS`), y NUNCA plumbea el
    `output_dir` del provider a `AssetTransfer(allowed_roots=...)`. Un workdir
    dentro del repo hace fallar el 100% de las transferencias con
    "Access denied: ... outside allowed directories. Files must be under temp
    or output_dir" y el sink aborta el manifest entero.

    Los intermedios viven aqui; lo duradero (scene-N.mp4, final.mp4,
    manifest.json) va a `runs/{job}/`, que no pasa por el sink.
    """
    d = Path(tempfile.gettempdir()) / "firstframe" / job_id / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _local_media(asset, workdir: Path, dest: Path, *, step=None) -> Path:
    """Trae el mp4 de la escena a disco local.

    `ObjectStorageSink` REESCRIBE `asset.url` al objeto de B2 durante el run,
    asi que al terminar ya no es un `file://`. Orden de preferencia:
      1. `file://` -> tal cual.
      2. El fichero que `FFmpegCompositor` dejo en su `output_dir`, que se
         llama `{step_id}.mp4` — evita una descarga de varios MB.
      3. Descarga FIRMADA desde B2 (el bucket es privado: un GET pelado da 401).
    """
    parsed = urllib.parse.urlparse(asset.url)
    if parsed.scheme == "file":
        return Path(urllib.parse.unquote(parsed.path))
    if step is not None:
        cand = Path(workdir) / f"{step.step_id}.mp4"
        if cand.is_file():
            return cand
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(M.fetch_bytes(asset.url))
    return dest


def concat_scenes(paths: list[str | Path], dest: str | Path) -> Path:
    """Master del job. `-c copy`: los parametros ya son identicos (scenes.py)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    listing = dest.parent / f"{dest.stem}.concat.txt"
    listing.write_text("".join(f"file '{Path(p).resolve()}'\n" for p in paths))
    subprocess.run(
        [P.ffmpeg_bin(), "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(dest)],
        capture_output=True, text=True, check=True)
    listing.unlink(missing_ok=True)
    return dest


def sample_frames(paths: list[str | Path], dest_dir: str | Path,
                  prefix: str = "") -> list[str]:
    """Un fotograma del centro de cada escena, en jpg. Para mirarlas con los ojos.

    Revisar un job en modo free abriendo 3 mp4 es lento; esto deja las 3 imagenes
    en un directorio y se ven de un vistazo. Se coge el centro y no el primer
    fotograma a proposito: en el centro el movimiento de Ken Burns ya se nota.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    for i, path in enumerate(paths, start=1):
        path = Path(path)
        try:
            dur = float(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, check=True).stdout.strip() or 0.0)
        except (subprocess.CalledProcessError, ValueError):
            dur = 0.0
        dest = dest_dir / f"{prefix}scene-{i}.jpg"
        subprocess.run(
            [P.ffmpeg_bin(), "-loglevel", "error", "-y", "-ss", f"{max(dur / 2, 0):.2f}",
             "-i", str(path), "-frames:v", "1", "-q:v", "2", str(dest)],
            capture_output=True, text=True, check=True)
        out.append(str(dest))
    return out


def _emit(on_event, kind: str, **payload) -> None:
    if on_event is None:
        return
    try:
        on_event({"type": kind, "ts": time.time(), **payload})
    except Exception as exc:  # noqa: BLE001 - un consumidor roto no tumba el job
        logger.warning("on_event(%s) fallo: %s", kind, exc)


# --- Job ---------------------------------------------------------------------

def run_job(
    job_id: str,
    brief: str,
    on_scene: Callable[[str], None] | None = None,
    *,
    scenes: list[Scene] | None = None,
    n_scenes: int = DEFAULT_SCENES,
    seconds: float = DEFAULT_SECONDS,
    mock: bool | None = None,
    use_b2: bool | None = None,
    out_dir: str | Path = "runs",
    cache_dir: str | Path | None = ".cache/steps",
    max_iterations: int = 2,
    threshold: float | None = None,
    llm_plan: bool = False,
    concat: bool = True,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> JobResult:
    """Genera el job entero, escena a escena, avisando tras cada una.

    `on_scene(path)` recibe la ruta LOCAL del mp4 de escena ya normalizado a
    los parametros canonicos, en cuanto esta lista. El backend la mete en el
    assembler; el player empieza a reproducir mientras el resto se genera.
    """
    t0 = time.monotonic()
    out_dir = Path(out_dir)
    job_dir = out_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    if use_b2 is None:
        use_b2 = M.b2_enabled()
    if cache_dir:
        # Namespace por job. La StepCache indexa por (modelo, prompt, params) y
        # devuelve el Step ENTERO, assets incluidos: sin namespace, dos jobs con
        # la misma escena se pasarian rutas file:// del otro job (ya borradas
        # por la limpieza de /tmp) y el sink fallaria al subirlas.
        cache_dir = Path(cache_dir) / job_id

    plan = scenes
    if plan is None and llm_plan:
        plan = plan_scenes_llm(brief, n_scenes, seconds=seconds)
    if plan is None:
        plan = plan_scenes(brief, n_scenes, seconds=seconds)

    result = JobResult(job_id=job_id, brief=brief)
    dead = chaos.dead()
    logger.info("job %s: %d escenas, gen=%s, mock=%s, b2=%s, chaos=%s",
                job_id, len(plan), gen_mode(), mock if mock is not None else "auto",
                use_b2, dead or "-")
    _emit(on_event, "job_started", job_id=job_id, scenes=len(plan),
          brief=brief, chaos=dead)

    records: list[M.SceneRecord] = []
    parent_result = None      # PipelineResult de la escena anterior (lineage)
    for scene in plan:
        scene_t0 = time.monotonic()
        workdir = media_workdir(job_id, scene.slug)
        _emit(on_event, "scene_started", job_id=job_id, scene=scene.n,
              title=scene.title)

        ps = resolve_providers(scene, workdir, mock=mock)
        result.provider_mode = ps.mode
        for note in ps.notes:
            logger.info("escena %d: %s", scene.n, note)

        loop, fe = build_scene_agent(
            scene, job_id, brief,
            workdir=workdir,
            parent=parent_result,
            mock=mock, providers=ps, threshold=threshold,
            max_iterations=max_iterations, cache_dir=cache_dir,
        )

        # Sink NUEVO por escena (es de un solo uso) y close() en finally.
        # _owns_sink=False es OBLIGATORIO aqui: AgentLoop pasa los MISMOS
        # run_kwargs a cada iteracion, y con el default el sink quedaria
        # cerrado tras la iteracion 1 y la 2 escribiria sobre un pool muerto.
        sink = M.scene_sink(job_id, scene.n) if use_b2 else None
        try:
            kwargs: dict[str, Any] = {"raise_on_failure": True}
            if sink is not None:
                kwargs.update(sink=sink, _owns_sink=False)
            agent_result = loop.run(**kwargs)
        finally:
            if sink is not None:
                sink.close()

        parent_result = agent_result.final

        asset = composite_asset(agent_result.final)
        if asset is None:
            raise RuntimeError(f"la escena {scene.n} no produjo mp4")
        comp_step = next((s for s in reversed(agent_result.final.run.steps)
                          if s.metadata.get("role") == "composite"), None)
        raw = _local_media(asset, workdir, workdir / "composite.mp4", step=comp_step)
        scene_path = normalize_scene(raw, job_dir / f"{scene.slug}.mp4",
                                     label=DRAFT_WATERMARK)

        verdict = fe.last()
        elapsed = int((time.monotonic() - scene_t0) * 1000)
        record = M.scene_record_from_result(
            scene.n, scene.title, agent_result,
            judge=verdict.as_dict() if verdict else {},
            local_path=str(scene_path), elapsed_ms=elapsed,
        )
        record.duration_sec = scene.seconds
        record.spec = scene_to_spec(scene)
        record.chain_parent_run_id = records[-1].run_id if records else None
        records.append(record)
        result.scene_paths.append(str(scene_path))
        if not result.first_scene_ms:
            result.first_scene_ms = int((time.monotonic() - t0) * 1000)

        for fb in record.fallbacks:
            logger.warning("FAILOVER escena %d: %s %s -> %s",
                           scene.n, fb["role"], fb["from"], fb["to"])
            _emit(on_event, "provider_failover", job_id=job_id, scene=scene.n, **fb)
        if record.iterations > 1:
            _emit(on_event, "scene_refined", job_id=job_id, scene=scene.n,
                  iterations=record.iterations, judge=record.judge)

        logger.info("escena %d/%d lista en %d ms -> %s (iteraciones=%d, juez=%.2f)",
                    scene.n, len(plan), elapsed, scene_path, record.iterations,
                    (verdict.score if verdict else -1.0))
        _emit(on_event, "scene_ready", job_id=job_id, scene=scene.n,
              path=str(scene_path), elapsed_ms=elapsed,
              iterations=record.iterations, judge=record.judge)

        # EL CALLBACK. Antes de seguir con la siguiente escena.
        if on_scene is not None:
            try:
                _call_on_scene(on_scene, scene.n, str(scene_path), {
                    "title": scene.title, "ms": elapsed,
                    "iterations": record.iterations, "judge": record.judge,
                    "fallbacks": record.fallbacks, "run_id": record.run_id,
                })
            except Exception as exc:  # noqa: BLE001
                logger.exception("on_scene fallo para la escena %d: %s", scene.n, exc)

    result.scenes = records
    result.elapsed_ms = int((time.monotonic() - t0) * 1000)

    if concat and result.scene_paths:
        result.final_mp4 = str(concat_scenes(result.scene_paths, job_dir / "final.mp4"))

    doc = M.build_aggregate(job_id, brief, records, extra={
        "provider_mode": result.provider_mode,
        "chaos_at_start": dead,
        "elapsed_ms": result.elapsed_ms,
        "first_scene_ms": result.first_scene_ms,
        "final_mp4": result.final_mp4,
        "scene_paths": result.scene_paths,
        "video_params": video_params(result.scene_paths[0]) if result.scene_paths else None,
    })
    result.aggregate = doc
    result.aggregate_written = M.write_aggregate(job_id, doc, local_dir=out_dir,
                                                use_b2=use_b2)

    logger.info("job %s terminado en %d ms (primera escena a los %d ms); "
                "agregado -> %s", job_id, result.elapsed_ms, result.first_scene_ms,
                result.aggregate_written.get("b2_url") or result.aggregate_written["local"])
    _emit(on_event, "job_complete", job_id=job_id, elapsed_ms=result.elapsed_ms,
          first_scene_ms=result.first_scene_ms, final_mp4=result.final_mp4,
          manifest=result.aggregate_written.get("b2_url"),
          failovers=doc.get("failovers"))
    return result


# --- Refinado de UNA escena (reject en caliente) ------------------------------

def refine_scene(job_id: str, n: int, *, note: str | None = None,
                 out_dir: str | Path = "runs", mock: bool | None = None,
                 use_b2: bool | None = None, max_iterations: int = 2,
                 threshold: float | None = None, take: int | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None) -> str:
    """Relanza UNA escena con la nota del revisor y devuelve la ruta de la toma.

    Es el camino de "rechazo en caliente" del guion: Ana rechaza la escena 2 en
    el segundo 15 con "logo ilegible" y la nota entra en el prompt del keyframe
    de la nueva pasada del `AgentLoop`. El run nuevo cuelga por `parent_run_id`
    del run de la toma rechazada, asi que el manifest deja la cadena
    toma-mala -> toma-buena.

    Reconstruye la escena desde el manifest agregado del job: por eso
    `SceneRecord.spec` guarda los tres prompts.
    """
    out_dir = Path(out_dir)
    if use_b2 is None:
        use_b2 = M.b2_enabled()
    doc = M.read_aggregate(job_id, local_dir=out_dir, use_b2=use_b2)
    entry = next((sc for sc in doc.get("scenes", []) if int(sc["n"]) == int(n)), None)
    if entry is None:
        raise KeyError(f"el job {job_id} no tiene escena {n}")
    spec = entry.get("spec") or {}
    if not spec:
        raise KeyError(f"la escena {n} de {job_id} no guardo su spec "
                       f"(job generado con una version anterior del runner)")

    scene = scene_from_spec(spec)
    if note:
        # La nota del revisor manda por encima del juez: va literal al prompt.
        scene = Scene(n=scene.n, title=scene.title,
                      keyframe_prompt=(f"{scene.keyframe_prompt}. Reviewer rejected the "
                                       f"previous take: {note.strip()}. Fix exactly that."),
                      voiceover=scene.voiceover, clip_prompt=scene.clip_prompt,
                      seconds=scene.seconds)

    brief = doc.get("brief", "")
    job_dir = out_dir / job_id
    if take is None:
        take = 1 + len(list(job_dir.glob(f"scene-{n}-take*.mp4")))
    workdir = media_workdir(job_id, f"{scene.slug}-take{take}")

    logger.info("refinando %s escena %d (toma %d) con nota=%r", job_id, n, take, note)
    _emit(on_event, "scene_refine_started", job_id=job_id, scene=n, take=take, note=note)

    loop, fe = build_scene_agent(scene, job_id, brief, workdir=workdir, mock=mock,
                                 threshold=threshold, max_iterations=max_iterations,
                                 cache_dir=None)   # sin cache: queremos otra toma
    sink = M.scene_sink(job_id, n) if use_b2 else None
    try:
        kwargs: dict[str, Any] = {"raise_on_failure": True}
        if sink is not None:
            kwargs.update(sink=sink, _owns_sink=False)
        agent_result = loop.run(**kwargs)
    finally:
        if sink is not None:
            sink.close()

    asset = composite_asset(agent_result.final)
    if asset is None:
        raise RuntimeError(f"el refinado de la escena {n} no produjo mp4")
    comp_step = next((st for st in reversed(agent_result.final.run.steps)
                      if st.metadata.get("role") == "composite"), None)
    raw = _local_media(asset, workdir, workdir / "composite.mp4", step=comp_step)
    dest = normalize_scene(raw, job_dir / f"scene-{n}-take{take}.mp4",
                           label=DRAFT_WATERMARK)

    verdict = fe.last()
    record = M.scene_record_from_result(
        n, scene.title, agent_result,
        judge=verdict.as_dict() if verdict else {}, local_path=str(dest))
    record.spec = scene_to_spec(scene)
    record.chain_parent_run_id = entry.get("run_id")

    # El agregado registra la toma: la evidencia del loop de refinado.
    doc.setdefault("refinements", []).append({
        "scene": n, "take": take, "note": note,
        "run_id": record.run_id, "rejected_run_id": entry.get("run_id"),
        "iterations": record.iterations, "judge": record.judge,
        "path": str(dest), "at": datetime.now(timezone.utc).isoformat(),
    })
    # La escena pasa a ser la toma nueva; la anterior queda en `refinements`.
    for i, sc in enumerate(doc["scenes"]):
        if int(sc["n"]) == int(n):
            doc["scenes"][i] = record.as_dict()
    doc["refined_scenes"] = sorted({*doc.get("refined_scenes", []), n})
    M.write_aggregate(job_id, doc, local_dir=out_dir, use_b2=use_b2)

    logger.info("escena %d refinada -> %s (juez=%.2f)", n, dest,
                verdict.score if verdict else -1.0)
    _emit(on_event, "scene_refined", job_id=job_id, scene=n, take=take,
          path=str(dest), iterations=record.iterations, judge=record.judge)
    return str(dest)


# --- Corpus pregenerado para la demo -----------------------------------------

# Briefs de ejemplo del corpus. Tres categorias visuales distintas a proposito
# (cosmetica / alimentacion / deporte) y con anclas de estilo distintas, para
# que el video de la demo no enseñe tres veces el mismo anuncio.
PREGEN_JOBS: list[tuple[str, str]] = [
    ("demo-serum",
     "un frasco de serum facial de una marca DTC, sobre marmol blanco, luz de manana"),
    ("demo-cafe",
     "una bolsa de cafe de especialidad de tueste artesanal, sobre madera oscura"),
    ("demo-sneaker",
     "una zapatilla de running ligera de una marca nueva, sobre asfalto mojado"),
]


def pregenerate(jobs: list[tuple[str, str]] | None = None, *,
                n_scenes: int = DEFAULT_SCENES, seconds: float = DEFAULT_SECONDS,
                out_dir: str | Path = "runs", use_b2: bool | None = False,
                cache_dir: str | Path | None = ".cache/steps",
                frames_dir: str | Path | None = None) -> list[JobResult]:
    """Genera y CACHEA los jobs de ejemplo en modo free.

    A ~45 s por imagen, un job de 3 escenas son ~2:20 de reloj. Nadie va a
    esperar eso delante de la URL en vivo ni en mitad del video. Esto se lanza
    antes (una vez), llena `KeyframeCorpus` y deja los mp4 en `runs/`, asi que
    reproducir cualquiera de estos briefs despues es instantaneo: el corpus
    acierta por prompt+seed aunque cambie el job_id.

    Es idempotente: relanzarlo no regenera nada que ya este en el corpus.
    """
    os.environ["GEN_MODE"] = "free"     # el corpus solo tiene sentido en free
    jobs = jobs or PREGEN_JOBS
    results: list[JobResult] = []
    corpus = KeyframeCorpus()
    logger.info("pregeneracion: %d jobs x %d escenas en modo free; corpus %s "
                "(%d imagenes ya dentro)", len(jobs), n_scenes, corpus.dir, len(corpus))

    for job_id, brief in jobs:
        t0 = time.monotonic()
        try:
            res = run_job(job_id, brief, n_scenes=n_scenes, seconds=seconds,
                          mock=None, use_b2=use_b2, out_dir=out_dir,
                          cache_dir=cache_dir, max_iterations=1, threshold=0.0)
        except Exception as exc:  # noqa: BLE001 - un brief no puede tumbar el resto
            logger.exception("pregeneracion de %s fallo: %s", job_id, exc)
            continue
        results.append(res)
        if frames_dir:
            sample_frames(res.scene_paths, frames_dir, prefix=f"{job_id}-")
        logger.info("pregenerado %s en %.0f s -> %s", job_id,
                    time.monotonic() - t0, res.final_mp4)

    logger.info("corpus: %d imagenes en %s", len(KeyframeCorpus()), corpus.dir)
    return results


# --- CLI ---------------------------------------------------------------------

def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # El failover del SDK se loguea aqui ("Falling back from X to Y").
    logging.getLogger("genblaze").setLevel(logging.INFO)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.runner",
        description="Genera un job de FirstFrame escena a escena.")
    ap.add_argument("--job", default="demo1", help="id del job")
    ap.add_argument("--brief", default="un frasco de serum facial de una marca DTC, "
                                       "sobre marmol blanco, luz de manana")
    ap.add_argument("--scenes", type=int, default=DEFAULT_SCENES)
    ap.add_argument("--seconds", type=float, default=DEFAULT_SECONDS)
    ap.add_argument("--mock", action="store_true", help="fuerza providers mock")
    ap.add_argument("--real", action="store_true", help="intenta providers reales")
    ap.add_argument("--free", action="store_true",
                    help="generacion REAL gratis: Pollinations + Ken Burns "
                         "(equivale a GEN_MODE=free)")
    ap.add_argument("--pregenerate", action="store_true",
                    help="genera y cachea los briefs de ejemplo en modo free "
                         "para que la demo no espere 45 s por imagen")
    ap.add_argument("--frames", metavar="DIR", nargs="?", const="/tmp/real-scenes",
                    help="guarda un fotograma del centro de cada escena en DIR "
                         "(default /tmp/real-scenes)")
    ap.add_argument("--chaos", metavar="PROVIDER", nargs="?", const=CHAOS_KEY,
                    help=f"mata un proveedor antes de arrancar (default {CHAOS_KEY})")
    ap.add_argument("--no-chaos", action="store_true", help="resucita todo y sale")
    ap.add_argument("--no-b2", action="store_true", help="no subir nada a B2")
    ap.add_argument("--out", default="runs")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--max-iterations", type=int, default=2)
    ap.add_argument("--threshold", type=float, default=None,
                    help="umbral del juez (default JUDGE_THRESHOLD o 0.7)")
    ap.add_argument("--no-judge", action="store_true",
                    help="umbral 0: el AgentLoop pasa a la primera (sin red)")
    ap.add_argument("--llm-plan", action="store_true",
                    help="plan de escenas con NIM chat en vez de la plantilla")
    ap.add_argument("--approve", action="store_true",
                    help="al terminar: embeber manifest + subir con Object Lock")
    ap.add_argument("--refine", type=int, metavar="N",
                    help="relanza SOLO la escena N de un job ya generado")
    ap.add_argument("--note", default=None,
                    help="nota del revisor para --refine (entra en el prompt)")
    ap.add_argument("--verify", action="store_true",
                    help="al terminar: verify --fetch sobre el master aprobado")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="corre el demo() de los 6 modulos y sale")
    args = ap.parse_args(argv)

    _setup_logging(args.verbose)

    if args.selftest:
        return selftest()

    if args.free:
        # El modo lo lee `scenes.gen_mode()` del entorno, que es tambien como lo
        # activa el despliegue; la bandera solo es azucar para la linea de comandos.
        os.environ["GEN_MODE"] = "free"

    if args.pregenerate:
        results = pregenerate(n_scenes=args.scenes, seconds=args.seconds,
                              out_dir=args.out,
                              use_b2=False if args.no_b2 else None,
                              cache_dir=None if args.no_cache else ".cache/steps",
                              frames_dir=args.frames)
        print()
        for res in results:
            print(f"  {res.job_id:<14} {len(res.scenes)} escenas en "
                  f"{res.elapsed_ms / 1000:6.1f}s -> {res.final_mp4}")
        corpus = KeyframeCorpus()
        print(f"  corpus: {len(corpus)} imagenes en {corpus.dir}")
        print(f"  jobs cacheados: {len(results)}/{len(PREGEN_JOBS)}")
        return 0 if len(results) == len(PREGEN_JOBS) else 1

    if args.no_chaos:
        chaos.reset()
        print("chaos: todos los proveedores vivos")
        return 0
    if args.chaos:
        chaos.kill(args.chaos)
        logger.warning("CHAOS ARMADO: '%s' devolvera MODEL_ERROR en este run", args.chaos)

    mock = True if args.mock else (False if args.real else None)
    threshold = 0.0 if args.no_judge else args.threshold

    if args.refine:
        path = refine_scene(args.job, args.refine, note=args.note, out_dir=args.out,
                            mock=mock, use_b2=False if args.no_b2 else None,
                            max_iterations=args.max_iterations, threshold=threshold)
        print(f"escena {args.refine} refinada -> {path}")
        return 0

    try:
        result = run_job(
            args.job, args.brief,
            on_scene=lambda p: logger.info("on_scene -> %s", p),
            n_scenes=args.scenes, seconds=args.seconds, mock=mock,
            use_b2=False if args.no_b2 else None, out_dir=args.out,
            cache_dir=None if args.no_cache else ".cache/steps",
            max_iterations=args.max_iterations, threshold=threshold,
            llm_plan=args.llm_plan,
        )
    finally:
        if args.chaos:
            chaos.revive(args.chaos)

    print()
    print(f"job {result.job_id}: {len(result.scenes)} escenas en {result.elapsed_ms} ms "
          f"(primera a los {result.first_scene_ms} ms), modo={result.provider_mode}")
    for rec in result.scenes:
        judge = rec.judge.get("score")
        print(f"  escena {rec.n} {rec.title:<10} {rec.local_path}  "
              f"iteraciones={rec.iterations} juez={judge if judge is not None else '-'} "
              f"run={rec.run_id[:8]} parent={(rec.parent_run_id or '-')[:8]}")
    if result.scene_paths:
        sigs = {video_params(p) for p in result.scene_paths}
        print(f"  parametros de video: {'IDENTICOS' if len(sigs) == 1 else 'DISTINTOS!'} "
              f"-> {next(iter(sigs))}")
    if result.failovers:
        for fb in result.failovers:
            print(f"  FAILOVER escena {fb['scene']}: {fb['from']} -> {fb['to']}")
    else:
        print("  failovers: ninguno (usa --chaos gmicloud para provocarlo)")
    print(f"  master: {result.final_mp4}")
    print(f"  agregado: {result.aggregate_written.get('b2_url') or result.aggregate_written['local']}")
    if args.frames and result.scene_paths:
        for shot in sample_frames(result.scene_paths, args.frames, prefix=f"{args.job}-"):
            print(f"  frame de muestra: {shot}")

    if args.approve and result.final_mp4:
        info = M.approve(args.job, result.final_mp4, doc=result.aggregate,
                         local_dir=args.out, use_b2=not args.no_b2)
        print(f"  aprobado: {info['keys'].get('master') or info['local_master']} "
              f"lock={info['lock']} embed={info['embed_method']} "
              f"verificado={info['embed_verified']}")
    if args.verify:
        report = M.verify(args.job, fetch=True, local_dir=args.out)
        ok = "OK" if report["ok"] else "FALLO"
        print(f"  verify --fetch ({report.get('method')}): {ok} "
              f"({len(report['checks'])} comprobaciones)")
        for c in report["checks"]:
            print(f"    [{'ok' if c.get('ok') else 'XX'}] {c['check']} "
                  f"{c.get('error', '')}")
        if not report["ok"]:
            return 1
    return 0


def selftest() -> int:
    """Corre el demo() de los 6 modulos del pipeline. Un comando, todo verde."""
    import importlib

    mods = ["pipeline.chaos", "pipeline.providers", "pipeline.judge",
            "pipeline.scenes", "pipeline.manifest", "pipeline.runner"]
    failures = 0
    for name in mods:
        fn = demo if name == "pipeline.runner" else importlib.import_module(name).demo
        print(f"\n=== {name}.demo() ===")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FALLO en {name}: {type(exc).__name__}: {exc}")
    print(f"\nselftest: {len(mods) - failures}/{len(mods)} modulos en verde")
    return 1 if failures else 0


def demo() -> None:
    """Autocomprobacion: un job de 2 escenas en mock, sin B2 y sin red."""
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="runner-demo-"))
    prev = {k: os.environ.get(k) for k in ("CHAOS_FILE", "JUDGE_THRESHOLD")}
    os.environ["CHAOS_FILE"] = str(tmp / "chaos.json")
    os.environ["JUDGE_THRESHOLD"] = "0.0"   # sin red: el juez degrada a 0.5
    seen: list[str] = []
    try:
        brief = "un frasco de serum facial sobre marmol"
        plan = plan_scenes(brief, 2, seconds=1.0)
        assert [s.n for s in plan] == [1, 2]
        assert all(s.keyframe_prompt and s.voiceover and s.clip_prompt for s in plan)
        assert plan_scenes("x", 99)[0].n == 1 and len(plan_scenes("x", 99)) <= len(_BEATS)

        # --- el plan cuenta un mini arco y comparte ancla de estilo -----------
        three = plan_scenes(brief, 3)
        assert [s.title for s in three] == ["apertura", "detalle", "cierre"], \
            [s.title for s in three]
        anchor = style_anchor(brief)
        assert all(anchor in s.keyframe_prompt for s in three), \
            "las 3 escenas tienen que compartir el ancla de estilo"
        assert style_anchor(brief) == style_anchor(brief.upper() + "  ")
        assert len({style_anchor(b) for b in
                    ("cafe de especialidad", "zapatilla de running",
                     "serum facial", "silla de diseno")}) > 1, \
            "el ancla no varia entre briefs: los spots saldrian clonados"
        # Ni un plano con personas: el generador real hace caras de plastico.
        # Por palabra completa: "surface" contiene "face" y es legitimo.
        banned = re.compile(r"\b(face|faces|portrait|person|people|hands|model)\b")
        for beat in _BEATS:
            shot = beat[1].lower().replace("no people", "")   # la exclusion vale
            assert not banned.search(shot), beat
        # El plan del LLM acepta las dos formas de respuesta.
        items, style = _parse_plan('ruido {"style": "S", "scenes": [{"title": "t"}]} fin')
        assert items == [{"title": "t"}] and style == "S", (items, style)
        items, style = _parse_plan('[{"title": "t"}]')
        assert items == [{"title": "t"}] and style is None, (items, style)

        res = run_job("demojob", "un frasco de serum facial sobre marmol",
                      on_scene=seen.append, scenes=plan, mock=True, use_b2=False,
                      out_dir=tmp, cache_dir=None, max_iterations=1)

        # El callback se llamo por escena, en orden, ANTES de terminar el job.
        assert len(seen) == 2, seen
        assert seen == res.scene_paths, (seen, res.scene_paths)
        assert all(Path(p).is_file() and Path(p).stat().st_size > 0 for p in seen)

        # Parametros identicos -> concatenable con -c copy.
        assert len({video_params(p) for p in seen}) == 1
        assert res.final_mp4 and Path(res.final_mp4).is_file()

        # Frames de muestra: es como se revisa un job en modo free sin abrir mp4s.
        shots = sample_frames(res.scene_paths, tmp / "frames", prefix="demojob-")
        assert len(shots) == 2, shots
        assert all(Path(s).is_file() and Path(s).stat().st_size > 1024 for s in shots)

        # Lineage y agregado.
        assert res.scenes[1].parent_run_id == res.scenes[0].run_id, \
            (res.scenes[0].run_id, res.scenes[1].parent_run_id)
        doc = json.loads(Path(res.aggregate_written["local"]).read_text())
        assert doc["schema"] == M.SCHEMA and doc["scene_count"] == 2
        assert doc["lineage"][1]["parent_run_id"] == res.scenes[0].run_id
        assert doc["failovers"] == []
        assert len(doc["scenes"][0]["steps"]) == 4
        assert [s["role"] for s in doc["scenes"][0]["steps"]] == \
            ["keyframe", "voiceover", "clip", "composite"]

        # --- mismo job con chaos: failover registrado en el agregado --------
        chaos.kill(CHAOS_KEY)
        res2 = run_job("demojob-chaos", "un frasco de serum", scenes=plan[:1],
                       mock=True, use_b2=False, out_dir=tmp, cache_dir=None,
                       max_iterations=1)
        chaos.revive(CHAOS_KEY)
        fb = res2.failovers
        assert fb and fb[0]["from"] == "pixverse-v5.6" and fb[0]["to"] == "seedance-2-0", fb
        assert res2.scenes[0].steps[2]["model"] == "seedance-2-0"

        # --- rechazo en caliente: refinado de UNA escena --------------------
        take = refine_scene("demojob", 2, note="el logo no se lee",
                            out_dir=tmp, mock=True, use_b2=False, max_iterations=1)
        assert Path(take).is_file() and take.endswith("scene-2-take1.mp4"), take
        assert video_params(take) == video_params(seen[0]), \
            "la toma refinada tiene que ser concatenable con el resto"
        doc2 = json.loads(Path(res.aggregate_written["local"]).read_text())
        ref = doc2["refinements"][0]
        assert ref["scene"] == 2 and ref["note"] == "el logo no se lee"
        assert ref["rejected_run_id"] == res.scenes[1].run_id, ref
        assert doc2["refined_scenes"] == [2]
        # La escena 2 del agregado ya apunta a la toma nueva.
        assert doc2["scenes"][1]["local_path"] == take

        # on_scene con la firma de 3 argumentos del backend.
        got: list[tuple] = []
        run_job("demojob-arity", "brief", on_scene=lambda n, p, m: got.append((n, p, m)),
                scenes=plan[:1], mock=True, use_b2=False, out_dir=tmp,
                cache_dir=None, max_iterations=1)
        assert len(got) == 1 and got[0][0] == 1 and got[0][2]["title"] == "apertura", got
    finally:
        for k, v in prev.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)

    print("demo OK: on_scene se llama por escena, parametros identicos, "
          "lineage por parent_run_id, agregado correcto y failover con chaos")


if __name__ == "__main__":
    if os.environ.get("RUNNER_DEMO"):
        demo()
    else:
        raise SystemExit(main())
