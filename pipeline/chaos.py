"""Chaos switch: un flag persistente por proveedor.

Es el boton que mata un proveedor EN DIRECTO durante la demo. El backend lo
escribe via `POST /api/chaos {provider}`; el pipeline lo lee en cada step a
traves de `providers.ChaosWrapper`.

Por que un fichero JSON y no la DB: el pipeline corre en un thread/proceso
aparte del backend y no queremos acoplar `pipeline/` a sqlite ni al server.
Un fichero de 40 bytes con escritura atomica (`os.replace`) es suficiente,
no necesita locking (una sola escritura gana, y perder una carrera solo
significa que el operador pulsa la tecla otra vez).

Uso:
    from pipeline import chaos
    chaos.kill("gmicloud")      # el proveedor empieza a dar MODEL_ERROR
    chaos.is_dead("gmicloud")   # True
    chaos.revive("gmicloud")
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

DEFAULT_PATH = "data/chaos.json"


def path() -> Path:
    """Fichero de flags. Configurable con `CHAOS_FILE` (util en tests)."""
    return Path(os.environ.get("CHAOS_FILE", DEFAULT_PATH))


def _read() -> dict[str, bool]:
    p = path()
    try:
        raw = json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): bool(v) for k, v in raw.items()}


def _write(state: dict[str, bool]) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Escritura atomica: nadie lee un JSON a medias.
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".chaos-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def is_dead(name: str) -> bool:
    """True si `name` esta marcado como muerto ahora mismo."""
    return _read().get(name, False)


def dead() -> list[str]:
    """Nombres marcados como muertos, ordenados."""
    return sorted(k for k, v in _read().items() if v)


def set_dead(name: str, value: bool = True) -> dict[str, bool]:
    state = _read()
    state[name] = bool(value)
    _write(state)
    return state


def kill(name: str) -> dict[str, bool]:
    """Mata un proveedor. Idempotente."""
    return set_dead(name, True)


def revive(name: str) -> dict[str, bool]:
    """Resucita un proveedor. Idempotente."""
    return set_dead(name, False)


def reset() -> None:
    """Todos vivos. Lo llama el arranque del backend y el seed de la demo."""
    _write({})


def demo() -> None:
    """Autocomprobacion: kill/revive persisten entre lecturas y son atomicos."""
    import shutil

    tmpdir = tempfile.mkdtemp(prefix="chaos-demo-")
    prev = os.environ.get("CHAOS_FILE")
    os.environ["CHAOS_FILE"] = str(Path(tmpdir) / "nested" / "chaos.json")
    try:
        assert not path().exists(), "arranca sin fichero"
        assert is_dead("gmicloud") is False, "sin fichero, nadie esta muerto"
        assert dead() == []

        kill("gmicloud")
        assert is_dead("gmicloud") is True
        assert is_dead("openai") is False, "matar uno no mata a los demas"
        assert dead() == ["gmicloud"]

        kill("gmicloud")  # idempotente
        kill("openai")
        assert dead() == ["gmicloud", "openai"]

        revive("gmicloud")
        assert is_dead("gmicloud") is False
        assert dead() == ["openai"]

        reset()
        assert dead() == []

        # JSON corrupto -> nadie muerto, no revienta.
        path().write_text("{ esto no es json")
        assert is_dead("openai") is False
        assert dead() == []
    finally:
        if prev is None:
            os.environ.pop("CHAOS_FILE", None)
        else:
            os.environ["CHAOS_FILE"] = prev
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("demo OK: chaos flags persisten, aislan por proveedor y toleran JSON roto")


if __name__ == "__main__":
    demo()
