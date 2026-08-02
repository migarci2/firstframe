"""Providers propios.

PassthroughProvider existe porque Genblaze no tiene `Pipeline.input(fichero)`:
el step 0 SIEMPRE tiene que ser un provider generador. Para arrancar un pipeline
desde un asset que ya existe (una imagen de referencia, un clip ya renderizado)
hay que envolverlo en un provider de usar y tirar. Lo sufrieron 8 de 10 sample-apps
oficiales; el plan manda escribirlo el primer dia, no el de la demo.

ChaosWrapper existe porque `fallback_models` SOLO salta ante
`ProviderErrorCode.MODEL_ERROR` (verificado en
`genblaze_core/pipeline/pipeline.py:_try_fallback_models`): un timeout de
transporte NO dispara el failover. Para ensenar failover en camara hay que
provocar un MODEL_ERROR de verdad, y eso es exactamente lo que hace.

Las fabricas `mock_*_provider()` devuelven los MockProvider REALES del SDK
(`genblaze_core.MockProvider`, nunca `genblaze_core.testing` — importa pytest
a nivel de modulo) pero con un `assets=` callable que sintetiza media LOCAL
de verdad con ffmpeg. Es lo que hace que `FFmpegCompositor` y `FFmpegTransform`
funcionen en modo mock: sus assets por defecto son URLs https://mock.test/...
que ffmpeg no puede abrir.

OJO: nada de @dataclass sobre una subclase de SyncProvider. Sobrescribe __init__,
se salta BaseProvider.__init__ y revienta mucho despues con
AttributeError: '_poll_cache_max_age'.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import shutil
import subprocess
import tempfile
from pathlib import Path

from genblaze import (
    Asset,
    AudioMetadata,
    MockAudioProvider,
    MockProvider,
    MockVideoProvider,
    ProviderError,
    ProviderErrorCode,
    VideoMetadata,
)
from genblaze_core.models import Step
from genblaze_core.models.enums import StepStatus
from genblaze_core.providers.base import BaseProvider, SyncProvider

from pipeline import chaos

logger = logging.getLogger("firstframe.providers")

# --- Parametros de encode comunes (PLAN §2) ---------------------------------
# TODAS las escenas salen con exactamente estos parametros. Es lo que permite
# concatenar/fragmentar sin re-encodear en server/assembler.py.
WIDTH, HEIGHT, FPS = 1280, 720, 24

CANONICAL_VIDEO_ARGS: list[str] = [
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-profile:v", "high",
    "-pix_fmt", "yuv420p",
    "-r", str(FPS),
    "-g", "48",
    "-keyint_min", "48",
    "-sc_threshold", "0",
    "-b:v", "6M", "-maxrate", "6M", "-bufsize", "12M",
    "-s", f"{WIDTH}x{HEIGHT}",
]
CANONICAL_AUDIO_ARGS: list[str] = [
    "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
]
# Solo para los fragmentos que sirve el player; el master de escena no lo usa.
FRAGMENTED_MP4_ARGS: list[str] = [
    "-movflags", "empty_moov+default_base_moof+frag_keyframe",
]


def ffmpeg_bin() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg no esta en el PATH (lo necesita todo el pipeline)")
    return exe


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise RuntimeError(f"ffmpeg fallo ({proc.returncode}):\n{tail}")


def local_asset(path: str | Path, **extra) -> Asset:
    """Asset con sha256 + size reales a partir de un fichero local.

    sha256 no es cosmetico: `ObjectStorageSink` usa content-addressing y
    `Manifest.verify()` exige que cada output declare un sha256 de 64 hex.
    """
    p = Path(path).resolve()
    data = p.read_bytes()
    media_type, _ = mimetypes.guess_type(p.name)
    return Asset(
        url=p.as_uri(),
        media_type=media_type or "application/octet-stream",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        **extra,
    )


# --- Sintesis local con ffmpeg (modo mock) ----------------------------------

def make_keyframe(out: str | Path, *, seed: int = 0, label: str = "") -> Path:
    """PNG 1280x720 determinista. Sustituye a flux.1-schnell en modo mock."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    vf = f"drawtext=text='{_esc(label)}':fontsize=48:x=40:y=40:fontcolor=white" if label else None
    args = [ffmpeg_bin(), "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate=1:duration=1",
            "-frames:v", "1"]
    if vf:
        args += ["-vf", vf]
    args += [str(out)]
    _run(args)
    return out


def make_clip(out: str | Path, *, seconds: float = 4.0, label: str = "") -> Path:
    """MP4 mudo con los parametros canonicos de §2. Sustituye a pixverse."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    filters = [f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FPS}:duration={seconds}"]
    args = [ffmpeg_bin(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", filters[0]]
    if label:
        args += ["-vf", f"drawtext=text='{_esc(label)}':fontsize=40:x=40:y=h-90:fontcolor=white"]
    args += CANONICAL_VIDEO_ARGS + ["-an", "-t", str(seconds), str(out)]
    _run(args)
    return out


def make_voiceover(out: str | Path, *, seconds: float = 4.0, tone: int = 320) -> Path:
    """M4A/AAC. Sustituye a tts-1. AAC porque el compositor hace `-c copy`."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _run([ffmpeg_bin(), "-loglevel", "error", "-y",
          "-f", "lavfi", "-i", f"sine=frequency={tone}:duration={seconds}",
          *CANONICAL_AUDIO_ARGS, "-t", str(seconds), str(out)])
    return out


def _esc(text: str) -> str:
    """Escape para drawtext. Sin esto, un ':' en el brief rompe el filtro."""
    return (text.replace("\\", "\\\\").replace(":", "\\:")
                .replace("'", "").replace("%", "").replace(",", " "))


# --- Providers ---------------------------------------------------------------

class PassthroughProvider(SyncProvider):
    """Devuelve ficheros locales como si los hubiera generado.

    El modelo del step se ignora; los ficheros se fijan en el constructor.
    """

    name = "passthrough"

    def __init__(self, paths: list[str | Path]):
        super().__init__()
        self.paths = [Path(p).resolve() for p in paths]
        missing = [str(p) for p in self.paths if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"no existen: {missing}")

    def generate(self, step: Step, config=None) -> Step:
        step.assets = [self._asset(p) for p in self.paths]
        step.status = "completed"
        return step

    @staticmethod
    def _asset(path: Path) -> Asset:
        return local_asset(path)


class ChaosWrapper(SyncProvider):
    """Envuelve a otro provider y lo mata a peticion, con MODEL_ERROR de verdad.

    Por que MODEL_ERROR y no un timeout: `Pipeline._try_fallback_models` solo
    prueba `fallback_models` cuando `step.error_code == MODEL_ERROR`. Un
    `read operation timed out` deja el step muerto sin failover (verificado).

    Semantica de `guarded_models`: el flag de chaos nombra un PROVEEDOR, pero
    matar tambien al modelo de respaldo dejaria al pipeline sin salida y no
    habria failover que ensenar. Asi que se mata solo a los modelos vigilados
    (por defecto: cualquier modelo que NO sea ya un reintento de failover,
    detectado por la metadata `fallback_model` que escribe el propio SDK).
    En la demo se pasa `guarded_models=["pixverse-v5.6"]`: preciso y honesto,
    "el modelo primario esta caido, el de respaldo no".

    Se sobreescribe `generate()` y se delega en `inner.invoke()` (no en
    `inner.generate()`) para no saltarse el ciclo submit/poll/fetch ni la
    politica de reintentos del provider real envuelto.
    """

    def __init__(
        self,
        inner: BaseProvider,
        *,
        key: str | None = None,
        guarded_models: list[str] | None = None,
    ):
        super().__init__()
        self._inner = inner
        self.key = key or getattr(inner, "name", "unknown")
        self.guarded_models = list(guarded_models) if guarded_models else None
        # El pipeline registra `step.provider` con este nombre: el manifest
        # deja constancia de que ese step paso por el interruptor de caos.
        self.name = f"chaos:{self.key}"  # type: ignore[assignment]

    @property
    def inner(self) -> BaseProvider:
        return self._inner

    def _is_guarded(self, step: Step) -> bool:
        if self.guarded_models is not None:
            return step.model in self.guarded_models
        # Sin lista explicita: todo menos un reintento de failover ya en curso.
        return not step.metadata.get("fallback_model")

    def generate(self, step: Step, config=None) -> Step:
        if chaos.is_dead(self.key) and self._is_guarded(step):
            logger.warning(
                "CHAOS: %s/%s marcado como muerto -> MODEL_ERROR (esperando fallback_models)",
                self.key, step.model,
            )
            raise ProviderError(
                f"chaos: provider '{self.key}' model '{step.model}' is down",
                error_code=ProviderErrorCode.MODEL_ERROR,
            )

        out = self._inner.invoke(step, config)
        if out.status == StepStatus.FAILED:
            # Re-lanzamos preservando el error_code del provider real: si el
            # de verdad devolvio MODEL_ERROR, el failover tiene que seguir
            # saltando aunque estemos envueltos.
            raise ProviderError(
                out.error or f"{self.key} failed",
                error_code=out.error_code or ProviderErrorCode.UNKNOWN,
            )
        return out

    def get_capabilities(self):
        return self._inner.get_capabilities()


# --- Fabricas de mocks con media local real ---------------------------------

def mock_image_provider(workdir: str | Path, *, label: str = "") -> MockProvider:
    """MockProvider del SDK que escupe PNGs reales de ffmpeg."""
    workdir = Path(workdir)

    def factory(step: Step) -> list[Asset]:
        out = make_keyframe(workdir / f"keyframe-{step.step_id[:8]}.png", label=label)
        return [local_asset(out, width=WIDTH, height=HEIGHT)]

    return MockProvider(name="mock-image", assets=factory, cost_usd=0.0)


def mock_video_provider(
    workdir: str | Path, *, seconds: float = 4.0, label: str = ""
) -> MockVideoProvider:
    """MockVideoProvider del SDK con clips reales a los parametros de §2."""
    workdir = Path(workdir)

    def factory(step: Step) -> list[Asset]:
        out = make_clip(workdir / f"clip-{step.step_id[:8]}.mp4", seconds=seconds, label=label)
        asset = local_asset(out, width=WIDTH, height=HEIGHT, duration=seconds)
        asset.video = VideoMetadata(codec="h264", has_audio=False, frame_rate=float(FPS),
                                    resolution=f"{WIDTH}x{HEIGHT}")
        return [asset]

    return MockVideoProvider(assets=factory, cost_usd=0.0)


def mock_audio_provider(workdir: str | Path, *, seconds: float = 4.0,
                        tone: int = 320) -> MockAudioProvider:
    """MockAudioProvider del SDK con AAC real (el compositor hace `-c copy`)."""
    workdir = Path(workdir)

    def factory(step: Step) -> list[Asset]:
        out = make_voiceover(workdir / f"vo-{step.step_id[:8]}.m4a", seconds=seconds, tone=tone)
        asset = local_asset(out, duration=seconds)
        asset.audio = AudioMetadata(codec="aac", channels=2, sample_rate=48000)
        return [asset]

    return MockAudioProvider(assets=factory, cost_usd=0.0)


def demo() -> None:
    """Autocomprobacion: passthrough, sintesis ffmpeg, mocks y ChaosWrapper."""
    import os

    from genblaze_core.models.enums import Modality

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        # --- 1. PassthroughProvider (contrato original, no se toca) ----------
        png = tmp / "red.png"
        _run([ffmpeg_bin(), "-loglevel", "error", "-y", "-f", "lavfi",
              "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", str(png)])
        step = Step(step_id="t", run_id="t", provider="passthrough",
                    model="none", step_index=0)
        out = PassthroughProvider([png]).generate(step)

        assert len(out.assets) == 1, out.assets
        a = out.assets[0]
        assert a.media_type == "image/png", a.media_type
        assert a.size_bytes == png.stat().st_size
        assert a.sha256 == hashlib.sha256(png.read_bytes()).hexdigest()
        assert a.url.startswith("file://")
        assert out.status == "completed"

        try:
            PassthroughProvider([tmp / "no-existe.png"])
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("deberia rechazar ficheros inexistentes")

        # --- 2. Sintesis ffmpeg a los parametros canonicos -------------------
        clip = make_clip(tmp / "c.mp4", seconds=1.0, label="hola: mundo")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,codec_name,r_frame_rate", "-of", "csv=p=0", str(clip)],
            capture_output=True, text=True, check=True).stdout.strip()
        assert probe.startswith(f"h264,{WIDTH},{HEIGHT}"), probe
        assert "24/1" in probe, probe

        vo = make_voiceover(tmp / "v.m4a", seconds=1.0)
        assert vo.stat().st_size > 0

        # --- 3. Mocks del SDK con media local --------------------------------
        vp = mock_video_provider(tmp, seconds=1.0)
        s = Step(step_id="vid1234567", run_id="t", provider="mock-video",
                 model="mock", step_index=0, modality=Modality.VIDEO)
        s = vp.generate(s)
        assert s.assets[0].url.startswith("file://"), s.assets[0].url
        assert s.assets[0].media_type == "video/mp4"
        assert len(s.assets[0].sha256 or "") == 64

        # --- 4. ChaosWrapper: vivo delega, muerto da MODEL_ERROR -------------
        prev = os.environ.get("CHAOS_FILE")
        os.environ["CHAOS_FILE"] = str(tmp / "chaos.json")
        try:
            wrapped = ChaosWrapper(mock_video_provider(tmp, seconds=1.0),
                                   key="gmicloud", guarded_models=["pixverse-v5.6"])
            assert wrapped.name == "chaos:gmicloud"

            def fresh(model: str, **md) -> Step:
                st = Step(step_id=f"s{model}", run_id="t", provider=wrapped.name,
                          model=model, step_index=0, modality=Modality.VIDEO)
                st.metadata.update(md)
                return st

            ok = wrapped.generate(fresh("pixverse-v5.6"))
            assert ok.assets, "vivo: debe delegar en el provider envuelto"

            chaos.kill("gmicloud")
            try:
                wrapped.generate(fresh("pixverse-v5.6"))
            except ProviderError as exc:
                assert exc.error_code == ProviderErrorCode.MODEL_ERROR, exc.error_code
            else:
                raise AssertionError("muerto: deberia lanzar MODEL_ERROR")

            # El modelo de respaldo no esta vigilado -> sobrevive -> hay failover.
            ok2 = wrapped.generate(fresh("seedance-2-0"))
            assert ok2.assets, "el modelo de respaldo no debe morir"

            # Sin guarded_models: muere todo menos un reintento de failover.
            loose = ChaosWrapper(mock_video_provider(tmp, seconds=1.0), key="gmicloud")
            try:
                loose.generate(fresh("cualquiera"))
            except ProviderError as exc:
                assert exc.error_code == ProviderErrorCode.MODEL_ERROR
            else:
                raise AssertionError("sin lista: deberia matar al primario")
            assert loose.generate(fresh("seedance-2-0", fallback_model="seedance-2-0")).assets

            chaos.revive("gmicloud")
            assert wrapped.generate(fresh("pixverse-v5.6")).assets
        finally:
            if prev is None:
                os.environ.pop("CHAOS_FILE", None)
            else:
                os.environ["CHAOS_FILE"] = prev

    print("demo OK: passthrough + sintesis ffmpeg canonica + mocks locales + "
          "ChaosWrapper con MODEL_ERROR real")


if __name__ == "__main__":
    demo()
