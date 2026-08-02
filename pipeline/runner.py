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
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pipeline import chaos, manifest as M
from pipeline import providers as P
from pipeline.scenes import (
    CHAOS_KEY,
    DRAFT_WATERMARK,
    Scene,
    build_scene_agent,
    composite_asset,
    normalize_scene,
    resolve_providers,
    video_params,
)

logger = logging.getLogger("firstframe.runner")

DEFAULT_SCENES = 3          # PLAN §8 recorte 4: 6 -> 3 escenas
DEFAULT_SECONDS = 4.0

# Plantilla fija del plan de escenas. Es el camino por defecto: no depende de
# ninguna API y produce siempre el mismo numero de escenas.
_BEATS: list[tuple[str, str, str, str]] = [
    ("apertura",
     "wide establishing shot of the product on a clean surface",
     "{brief_short}",
     "slow push-in towards the product"),
    ("detalle",
     "extreme close-up of the product label and texture",
     "Cada detalle cuenta.",
     "slow orbit around the product, shallow depth of field"),
    ("uso",
     "the product in use, hands in frame, natural light",
     "Hecho para el dia a dia.",
     "handheld drift, subtle parallax"),
    ("contexto",
     "the product in a lifestyle setting, soft morning light",
     "Encaja donde vivas.",
     "lateral dolly across the scene"),
    ("beneficio",
     "macro shot of the result the product delivers",
     "Resultados que se ven.",
     "slow rack focus onto the result"),
    ("cierre",
     "hero shot of the product centred against a plain backdrop",
     "Disponible ya.",
     "static hero shot, slow zoom out"),
]


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
                seconds: float = DEFAULT_SECONDS) -> list[Scene]:
    """Plan por plantilla. Deterministico, sin red, siempre disponible."""
    n = max(1, min(n, len(_BEATS)))
    short = " ".join(brief.split())[:90]
    subject = short.rstrip(".")
    out: list[Scene] = []
    for i in range(n):
        title, shot, line, motion = _BEATS[i]
        out.append(Scene(
            n=i + 1,
            title=title,
            keyframe_prompt=f"{shot}; subject: {subject}",
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
        f"Break this ad brief into exactly {n} scenes for a short product spot.\n\n"
        f"BRIEF: {brief.strip()}\n\n"
        'Reply with ONLY a JSON array, one object per scene, keys exactly: '
        '"title" (1-2 words), "keyframe" (visual description of the first frame), '
        '"voiceover" (one short spoken sentence), "motion" (camera movement). '
        "No prose, no markdown fence."
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
        start, end = raw.find("["), raw.rfind("]")
        items = json.loads(raw[start:end + 1])
        scenes = [
            Scene(n=i + 1,
                  title=str(it["title"])[:24],
                  keyframe_prompt=str(it["keyframe"]),
                  voiceover=str(it["voiceover"]),
                  clip_prompt=str(it.get("motion", "slow push-in")),
                  seconds=seconds)
            for i, it in enumerate(items[:n])
        ]
        if scenes:
            logger.info("plan de escenas generado por NIM chat (%d escenas)", len(scenes))
            return scenes
    except Exception as exc:  # noqa: BLE001 - el plan LLM es un extra
        logger.warning("plan por LLM fallo (%s); uso la plantilla", exc)
    return None


# --- Utilidades --------------------------------------------------------------

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
    logger.info("job %s: %d escenas, mock=%s, b2=%s, chaos=%s",
                job_id, len(plan), mock if mock is not None else "auto",
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
                on_scene(str(scene_path))
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
    result.aggregate_written = M.write_aggregate(job_id, doc, local_dir=out_dir)

    logger.info("job %s terminado en %d ms (primera escena a los %d ms); "
                "agregado -> %s", job_id, result.elapsed_ms, result.first_scene_ms,
                result.aggregate_written.get("b2_url") or result.aggregate_written["local"])
    _emit(on_event, "job_complete", job_id=job_id, elapsed_ms=result.elapsed_ms,
          first_scene_ms=result.first_scene_ms, final_mp4=result.final_mp4,
          manifest=result.aggregate_written.get("b2_url"),
          failovers=doc.get("failovers"))
    return result


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
    ap.add_argument("--verify", action="store_true",
                    help="al terminar: verify --fetch sobre el master aprobado")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="corre el demo() de los 6 modulos y sale")
    args = ap.parse_args(argv)

    _setup_logging(args.verbose)

    if args.selftest:
        return selftest()

    if args.no_chaos:
        chaos.reset()
        print("chaos: todos los proveedores vivos")
        return 0
    if args.chaos:
        chaos.kill(args.chaos)
        logger.warning("CHAOS ARMADO: '%s' devolvera MODEL_ERROR en este run", args.chaos)

    mock = True if args.mock else (False if args.real else None)
    threshold = 0.0 if args.no_judge else args.threshold

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

    if args.approve and result.final_mp4:
        info = M.approve(args.job, result.final_mp4, doc=result.aggregate,
                         local_dir=args.out)
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
        plan = plan_scenes("un frasco de serum facial sobre marmol", 2, seconds=1.0)
        assert [s.n for s in plan] == [1, 2]
        assert all(s.keyframe_prompt and s.voiceover and s.clip_prompt for s in plan)
        assert plan_scenes("x", 99)[0].n == 1 and len(plan_scenes("x", 99)) <= len(_BEATS)

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
