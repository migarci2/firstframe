"""Ken Burns: convierte un keyframe FIJO en un plano de video de verdad.

Por que existe
--------------
En modo `free` el keyframe lo genera Pollinations (imagen real, gratis), pero
una imagen fija no es un video: un carrusel de fotos se ve en camara como lo
que es. Este modulo es el paso 2 de esa cadena — el mismo hueco que en modo
`real` ocuparia pixverse/seedance (imagen -> video) — y lo resuelve con el
recurso que SI tenemos gratis: ffmpeg `zoompan`, o sea el efecto Ken Burns.

Lo que importa visualmente (todo verificado renderizando, no leido):

1. **Supersampling antes del zoompan.** `zoompan` recorta y reescala por
   fotograma; aplicado directo sobre 1024x576 (lo que devuelve el tier anonimo
   de Pollinations) el resultado tiembla y se ve blando. Se preescala a 3x el
   canonico (3840x2160) con lanczos y el zoompan trabaja ahi: el movimiento
   queda subpixel y la salida a 1280x720 sale nitida. Coste: ~1 s por clip de
   4 s en un portatil. Es gratis comparado con los 45 s de la imagen.

2. **Solo `on`, nunca `zoom`.** La expresion `z='min(zoom+0.0015,1.5)'` que
   circula por todas partes es acumulativa: depende del fotograma anterior, asi
   que el movimiento cambia si cambia `d` y no es reproducible. Aqui todas las
   expresiones se escriben en funcion de `on` (indice de fotograma de salida),
   asi que el plano es identico en cada re-render.

3. **Easing.** Interpolacion lineal = arranque y frenada bruscos, delata la
   automatizacion. Se aplica un smoothstep t*t*(3-2t) sobre el progreso: el
   plano entra y sale suave, como un dolly de verdad.

4. **Direccion distinta por escena.** Tres push-in seguidos se leen como un
   template. `move_for(n)` reparte push-in / paneo / pull-out / ascenso por
   indice de escena, asi que un spot de 3 escenas nunca repite movimiento.

El clip sale MUDO y con los parametros canonicos de `providers.CANONICAL_VIDEO_ARGS`
(1280x720, h264, 24 fps, GOP 48). La voz se mezcla despues en el step 3
(`FFmpegCompositor`), igual que en el camino mock y en el real.

Uso
---
    from pipeline.kenburns import KenBurnsProvider, KEN_BURNS_MODEL

    pipe.step(KenBurnsProvider(output_dir=wd, scene=1), model=KEN_BURNS_MODEL,
              modality=Modality.VIDEO, input_from=[0], duration=4.0)

Autocomprobacion (no necesita red):

    .venv/bin/python -m pipeline.kenburns
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from genblaze import Asset, VideoMetadata
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.enums import Modality, ProviderErrorCode
from genblaze_core.models.step import Step
from genblaze_core.providers.base import ProviderCapabilities, SyncProvider
from genblaze_core.runnable.config import RunnableConfig

from pipeline import providers as P

logger = logging.getLogger("firstframe.kenburns")

# Modelo nominal del step "clip" en modo free. No es un modelo de red: es el
# nombre con el que este movimiento aparece en el manifest, y es honesto —
# el manifest no dice "pixverse" para algo que hace ffmpeg.
KEN_BURNS_MODEL = "kenburns-2.5d"
# Respaldo: mismo provider, movimiento minimo. Existe para que `fallback_models`
# tenga a donde saltar cuando el ChaosWrapper mata al primario en camara.
KEN_BURNS_FALLBACK = "kenburns-static"

# Factor de supersampling (ver docstring §1). 3x el canonico = 3840x2160.
SUPERSAMPLE = 3


@dataclass(frozen=True)
class Move:
    """Un movimiento de camara. `x`/`y` son fracciones 0..1 del recorrido util.

    x=0 es el borde izquierdo del encuadre disponible, x=1 el derecho; 0.5 es
    centrado. El zoom es multiplicador: 1.0 = plano completo.
    """

    name: str
    z0: float
    z1: float
    x0: float = 0.5
    x1: float = 0.5
    y0: float = 0.5
    y1: float = 0.5

    @property
    def label(self) -> str:
        return self.name.replace("_", " ")


# Repertorio. El orden importa: `move_for()` lo recorre por indice de escena,
# asi que las 3 primeras escenas de un spot salen con movimientos claramente
# distintos (acercarse / paneo lateral / alejarse).
MOVES: tuple[Move, ...] = (
    Move("push_in",   1.00, 1.16),                              # dolly in centrado
    Move("pan_right", 1.12, 1.16, x0=0.28, x1=0.72),            # paneo lateral
    Move("pull_out",  1.18, 1.02, y0=0.44, y1=0.56),            # revelado
    Move("rise",      1.06, 1.20, y0=0.74, y1=0.30),            # grua ascendente
    Move("pan_left",  1.16, 1.12, x0=0.72, x1=0.28),
    Move("drift_in",  1.02, 1.18, x0=0.36, x1=0.62, y0=0.34, y1=0.66),
)
STATIC_MOVE = Move("static", 1.03, 1.06)   # el respaldo: casi quieto, pero vivo

MOVES_BY_NAME: dict[str, Move] = {m.name: m for m in (*MOVES, STATIC_MOVE)}

# Modelo -> movimiento. None = "el que toque por indice de escena".
MODELS: dict[str, Move | None] = {
    KEN_BURNS_MODEL: None,
    KEN_BURNS_FALLBACK: STATIC_MOVE,
}


def move_for(scene_index: int) -> Move:
    """Movimiento de la escena N. Deterministico y distinto entre escenas."""
    return MOVES[int(scene_index) % len(MOVES)]


def _expr(a: float, b: float, ease: str) -> str:
    """Interpolacion a->b con easing, como expresion de ffmpeg."""
    if a == b:
        return f"{a:g}"
    return f"({a:g}+({b - a:g})*{ease})"


def build_filter(move: Move, frames: int, *, width: int = P.WIDTH,
                 height: int = P.HEIGHT, supersample: int = SUPERSAMPLE) -> str:
    """Cadena -vf completa: preescalado + zoompan + pixel format.

    `frames` es el numero de fotogramas de SALIDA; `d` del zoompan tiene que
    valer exactamente eso porque la entrada es una unica imagen.
    """
    frames = max(2, int(frames))
    big_w, big_h = width * supersample, height * supersample
    # Progreso 0..1 con smoothstep (ver docstring §3).
    t = f"(on/{frames - 1})"
    ease = f"({t}*{t}*(3-2*{t}))"

    z = _expr(move.z0, move.z1, ease)
    # x/y del zoompan van en coordenadas de la ENTRADA (la imagen preescalada) y
    # el recorrido util es (iw - iw/zoom).
    x = f"(iw-iw/zoom)*{_expr(move.x0, move.x1, ease)}"
    y = f"(ih-ih/zoom)*{_expr(move.y0, move.y1, ease)}"

    return (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={big_w}:{big_h},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={P.FPS},"
        f"format=yuv420p"
    )


def ken_burns_clip(src: str | Path, dest: str | Path, *, seconds: float = 4.0,
                   move: Move | str | None = None, scene_index: int = 0,
                   width: int = P.WIDTH, height: int = P.HEIGHT,
                   supersample: int = SUPERSAMPLE) -> Path:
    """Imagen fija -> mp4 mudo con movimiento, a los parametros canonicos de §2."""
    src, dest = Path(src), Path(dest)
    if not src.is_file():
        raise FileNotFoundError(f"no existe el keyframe {src}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if move is None:
        move = move_for(scene_index)
    elif isinstance(move, str):
        try:
            move = MOVES_BY_NAME[move]
        except KeyError:
            raise ValueError(f"movimiento desconocido {move!r}; "
                             f"conocidos: {', '.join(sorted(MOVES_BY_NAME))}") from None

    frames = max(2, round(float(seconds) * P.FPS))
    args = [P.ffmpeg_bin(), "-loglevel", "error", "-y", "-i", str(src),
            "-vf", build_filter(move, frames, width=width, height=height,
                                supersample=supersample),
            "-frames:v", str(frames), "-an",
            *P.CANONICAL_VIDEO_ARGS, str(dest)]
    P._run(args)
    logger.info("ken burns %s (%s) %.1fs -> %s", src.name, move.name,
                frames / P.FPS, dest.name)
    return dest


class KenBurnsProvider(SyncProvider):
    """`SyncProvider` imagen -> video. Ocupa el hueco del step 2 en modo free.

    Consume el asset de imagen de `step.inputs` (o sea `input_from=[0]`, el
    keyframe) y devuelve un mp4 mudo con movimiento. La duracion sale de
    `step.params["duration"]` (lo que escribe `pipe.step(..., duration=...)`)
    y si no del `seconds` del constructor.

    Args:
        output_dir: donde escribir el mp4. Tiene que estar bajo tmp si el run
            lleva `ObjectStorageSink` (ver `runner.media_workdir`).
        seconds: duracion por defecto del clip.
        scene: indice de escena; decide el movimiento cuando el modelo no lo
            fija. Escenas distintas -> movimientos distintos.
        move: fuerza un movimiento concreto (nombre o `Move`). Para tests.
        image_dirs: directorios donde buscar el fichero por sha256 si el asset
            de entrada ya no es un `file://` (el sink reescribe las URLs al
            subirlas a B2).
    """

    name = "kenburns"

    def __init__(
        self,
        *,
        output_dir: str | Path | None = None,
        seconds: float = 4.0,
        scene: int = 0,
        move: Move | str | None = None,
        image_dirs: list[str | Path] | None = None,
        **kwargs: Any,
    ) -> None:
        # super() OBLIGATORIO y nada de @dataclass: sin esto BaseProvider.__init__
        # no corre y el run muere luego con AttributeError: '_poll_cache_max_age'.
        super().__init__(**kwargs)
        self.output_dir = Path(output_dir or Path(tempfile.gettempdir()) / "firstframe-kenburns")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.seconds = float(seconds)
        self.scene = int(scene)
        self.move = move
        self.image_dirs = [Path(d) for d in (image_dirs or [])]

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            supported_modalities=[Modality.VIDEO],
            supported_inputs=["image"],
            accepts_chain_input=True,
            output_formats=["video/mp4"],
            models=sorted(MODELS),
        )

    # --- resolucion de la imagen de entrada ---------------------------------

    def _resolve_image(self, step: Step) -> Path:
        assets = [a for a in (step.inputs or [])
                  if (a.media_type or "").startswith("image/")]
        if not assets:
            raise ProviderError(
                f"{self.name} necesita un asset de imagen en step.inputs "
                f"(usa input_from=[<step del keyframe>]); recibi "
                f"{[a.media_type for a in (step.inputs or [])]}",
                error_code=ProviderErrorCode.INVALID_INPUT,
            )
        asset = assets[0]
        parsed = urlparse(asset.url or "")

        if parsed.scheme == "file":
            path = Path(unquote(parsed.path))
            if path.is_file():
                return path
        elif not parsed.scheme and asset.url:
            path = Path(asset.url)
            if path.is_file():
                return path

        # El sink reescribe asset.url al objeto de B2. Los keyframes se guardan
        # con nombre content-addressed (sha[:16].jpg), asi que el fichero local
        # sigue estando aqui aunque la URL ya no lo diga.
        if asset.sha256:
            for d in self.image_dirs:
                for cand in sorted(Path(d).glob(f"{asset.sha256[:16]}.*")):
                    if cand.is_file():
                        return cand

        if parsed.scheme in ("http", "https"):
            return self._download(asset)

        raise ProviderError(
            f"{self.name}: no se puede leer el keyframe {asset.url!r} "
            f"(ni file:// existente, ni copia local por sha, ni http)",
            error_code=ProviderErrorCode.INVALID_INPUT,
        )

    def _download(self, asset: Asset) -> Path:
        import httpx

        ext = mimetypes.guess_extension(asset.media_type or "") or ".jpg"
        stem = (asset.sha256 or hashlib.sha256(asset.url.encode()).hexdigest())[:16]
        dest = self.output_dir / f"input-{stem}{ext}"
        if dest.is_file():
            return dest
        logger.info("%s: descargando el keyframe desde %s", self.name, asset.url[:80])
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(asset.url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        return dest

    # --- SyncProvider --------------------------------------------------------

    def generate(self, step: Step, config: RunnableConfig | None = None) -> Step:
        model = (step.model or KEN_BURNS_MODEL).strip()
        if model not in MODELS:
            # MODEL_ERROR a proposito: es el UNICO codigo ante el que
            # `fallback_models=` prueba el siguiente modelo.
            raise ProviderError(
                f"modelo '{model}' no disponible en {self.name}; "
                f"conocidos: {', '.join(sorted(MODELS))}",
                error_code=ProviderErrorCode.MODEL_ERROR,
            )

        src = self._resolve_image(step)
        params = dict(step.params or {})
        seconds = float(params.get("duration") or params.get("seconds") or self.seconds)
        move = MODELS[model] or self.move or move_for(self.scene)
        if isinstance(move, str):
            move = MOVES_BY_NAME[move]

        dest = self.output_dir / f"kenburns-{step.step_id[:8]}-{move.name}.mp4"
        ken_burns_clip(src, dest, seconds=seconds, move=move)

        asset = P.local_asset(dest, width=P.WIDTH, height=P.HEIGHT, duration=seconds)
        asset.video = VideoMetadata(codec="h264", has_audio=False,
                                    frame_rate=float(P.FPS),
                                    resolution=f"{P.WIDTH}x{P.HEIGHT}")
        asset.metadata = {
            "provider": self.name,
            "model": model,
            "motion": move.name,
            "source_image": src.name,
            "source_sha256": (step.inputs[0].sha256 if step.inputs else None),
            "frames": max(2, round(seconds * P.FPS)),
            "supersample": SUPERSAMPLE,
        }
        step.assets.append(asset)
        return step


# --- Autocomprobacion --------------------------------------------------------


def demo() -> None:
    """Rinde un clip de verdad y comprueba parametros canonicos y movimiento."""
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="kenburns-demo-"))
    try:
        # Keyframe de prueba (ffmpeg local, sin red).
        png = P.make_keyframe(tmp / "kf.png", label="ken burns")

        # 1. Movimientos distintos por escena, deterministicos.
        names = [move_for(i).name for i in range(3)]
        assert len(set(names)) == 3, f"las 3 primeras escenas repiten movimiento: {names}"
        assert move_for(0).name == move_for(len(MOVES)).name, "move_for no cicla"

        # 2. Las expresiones no dependen del fotograma anterior (reproducible).
        vf = build_filter(MOVES[0], 48)
        assert "zoom+" not in vf, f"expresion acumulativa en {vf}"
        assert "d=48" in vf and f"s={P.WIDTH}x{P.HEIGHT}" in vf, vf

        # 3. Clip real a los parametros canonicos.
        clip = ken_burns_clip(png, tmp / "a.mp4", seconds=1.0, scene_index=0)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,codec_name,r_frame_rate,nb_frames",
             "-of", "csv=p=0", str(clip)],
            capture_output=True, text=True, check=True).stdout.strip()
        assert probe.startswith(f"h264,{P.WIDTH},{P.HEIGHT}"), probe
        assert "24/1" in probe, probe
        assert probe.endswith(f",{P.FPS}"), f"esperaba {P.FPS} fotogramas: {probe}"

        # 4. Se MUEVE de verdad: primer y ultimo fotograma distintos.
        for n, out in ((0, tmp / "f0.png"), (P.FPS - 1, tmp / "fN.png")):
            P._run([P.ffmpeg_bin(), "-loglevel", "error", "-y", "-i", str(clip),
                    "-vf", f"select=eq(n\\,{n})", "-frames:v", "1", str(out)])
        a, b = (tmp / "f0.png").read_bytes(), (tmp / "fN.png").read_bytes()
        assert a != b, "el clip no se mueve: primer y ultimo fotograma identicos"

        # 5. El provider, con el asset de entrada por step.inputs.
        prov = KenBurnsProvider(output_dir=tmp, scene=1)
        caps = prov.get_capabilities()
        assert Modality.VIDEO in (caps.supported_modalities or []), caps
        assert caps.accepts_chain_input, caps

        step = Step(step_id="kb123456", run_id="t", provider=prov.name,
                    model=KEN_BURNS_MODEL, step_index=2, modality=Modality.VIDEO,
                    inputs=[P.local_asset(png, width=P.WIDTH, height=P.HEIGHT)],
                    params={"duration": 1.0})
        step = prov.generate(step)
        assert len(step.assets) == 1, step.assets
        out = step.assets[0]
        assert out.media_type == "video/mp4", out.media_type
        assert len(out.sha256 or "") == 64, out.sha256
        assert out.metadata["motion"] == move_for(1).name, out.metadata
        assert Path(unquote(urlparse(out.url).path)).is_file(), out.url

        # 6. El respaldo usa el movimiento estatico (lo que ve el failover).
        step2 = Step(step_id="kb765432", run_id="t", provider=prov.name,
                     model=KEN_BURNS_FALLBACK, step_index=2, modality=Modality.VIDEO,
                     inputs=[P.local_asset(png)], params={"duration": 1.0})
        assert prov.generate(step2).assets[0].metadata["motion"] == "static"

        # 7. Modelo desconocido -> MODEL_ERROR (lo unico que dispara fallback_models).
        bad = Step(step_id="kbbad", run_id="t", provider=prov.name, model="no-existe",
                   step_index=2, modality=Modality.VIDEO, inputs=[P.local_asset(png)])
        try:
            prov.generate(bad)
            raise AssertionError("un modelo desconocido deberia haber fallado")
        except ProviderError as exc:
            assert exc.error_code is ProviderErrorCode.MODEL_ERROR, exc.error_code

        # 8. Sin imagen de entrada -> INVALID_INPUT, no un ffmpeg ilegible.
        empty = Step(step_id="kbempty", run_id="t", provider=prov.name,
                     model=KEN_BURNS_MODEL, step_index=2, modality=Modality.VIDEO)
        try:
            prov.generate(empty)
            raise AssertionError("sin inputs deberia haber fallado")
        except ProviderError as exc:
            assert exc.error_code is ProviderErrorCode.INVALID_INPUT, exc.error_code

        # 9. Recuperacion por sha cuando el sink ya reescribio la URL a B2.
        stash = tmp / "stash"
        stash.mkdir()
        sha = P.local_asset(png).sha256
        shutil.copy2(png, stash / f"{sha[:16]}.png")
        prov2 = KenBurnsProvider(output_dir=tmp, scene=0, image_dirs=[stash])
        remote = Asset(url="https://s3.example.com/runs/x/keyframe.png",
                       media_type="image/png", sha256=sha, size_bytes=png.stat().st_size)
        step3 = Step(step_id="kbremote", run_id="t", provider=prov.name,
                     model=KEN_BURNS_MODEL, step_index=2, modality=Modality.VIDEO,
                     inputs=[remote], params={"duration": 1.0})
        assert prov2.generate(step3).assets, "no recupero el keyframe por sha256"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"demo OK: {len(MOVES)} movimientos deterministicos sin acumulacion, clip "
          f"{P.WIDTH}x{P.HEIGHT}@{P.FPS} canonico y en movimiento, provider "
          f"imagen->video con MODEL_ERROR para el failover y rescate por sha256")


if __name__ == "__main__":
    demo()
