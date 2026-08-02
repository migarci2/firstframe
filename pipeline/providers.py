"""Providers propios.

PassthroughProvider existe porque Genblaze no tiene `Pipeline.input(fichero)`:
el step 0 SIEMPRE tiene que ser un provider generador. Para arrancar un pipeline
desde un asset que ya existe (una imagen de referencia, un clip ya renderizado)
hay que envolverlo en un provider de usar y tirar. Lo sufrieron 8 de 10 sample-apps
oficiales; el plan manda escribirlo el primer dia, no el de la demo.

OJO: nada de @dataclass sobre una subclase de SyncProvider. Sobrescribe __init__,
se salta BaseProvider.__init__ y revienta mucho despues con
AttributeError: '_poll_cache_max_age'.
"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path

from genblaze import Asset
from genblaze_core.models import Step
from genblaze_core.providers.base import SyncProvider


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
        data = path.read_bytes()
        media_type, _ = mimetypes.guess_type(path.name)
        return Asset(
            url=path.as_uri(),
            media_type=media_type or "application/octet-stream",
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )


def demo() -> None:
    """Autocomprobacion: un PNG real entra y sale como Asset con hash correcto."""
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "red.png"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "color=c=red:s=64x64:d=1", "-frames:v", "1", str(png)],
            check=True,
        )
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
            PassthroughProvider([Path(tmp) / "no-existe.png"])
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("deberia rechazar ficheros inexistentes")

    print("demo OK: passthrough devuelve el asset con hash y media_type correctos")


if __name__ == "__main__":
    demo()
