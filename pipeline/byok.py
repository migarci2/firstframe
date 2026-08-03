"""Bring-your-own-key: la clave de GMI Cloud del propio usuario, y nada mas.

Este fichero es el contrato entero de BYOK, y es corto a proposito: si alguien
quiere comprobar que su clave no acaba en ningun sitio raro, esto es lo unico
que tiene que leer. La UI enlaza aqui.

Donde vive la clave
-------------------
  navegador   `localStorage['firstframe.gmi_key']`. La escribe y la borra el
              usuario desde el propio formulario; nadie mas la toca.
  red         viaja en la cabecera `X-GMI-Key` de las peticiones que lanzan
              generacion (crear spot, regenerar, pedir cambios). Nada mas.
  servidor    `threading.local()`. El thread del job la coge al empezar y la
              suelta en un `finally` al terminar. NO hay diccionario global, NO
              se escribe en la base de datos, NO se escribe en disco, NO entra
              en el manifest ni en ningun evento SSE.
  proveedor   el unico destino: `Authorization: Bearer …` contra la API de
              GMI Cloud, que la hace el conector `genblaze_gmicloud`.

Que NO hace nadie con ella
--------------------------
  * no se registra: no hay ni un `print`/`logger` en todo el repo que reciba la
    clave. Para que un descuido futuro tampoco pueda, `scrub()` la tacha de
    cualquier texto que vaya a salir por un log o por la API, y se aplica en
    los dos sitios donde un error de proveedor se convierte en texto
    (`pipeline/scenes.py:GuardedImageProvider` y `server/jobs.py:_run_job_safe`).
  * no se persiste: `set()` guarda en un `threading.local`, que muere con el
    thread del job.
  * no sale hacia B2: lo unico que sube a B2 son los mp4 y el manifest, y el
    manifest se arma en `server/jobs.py:_write_manifest` a partir de la base de
    datos, donde la clave nunca ha estado.

Es opcional: sin clave, `key()` devuelve None y `resolve_providers()` decide
exactamente lo que decidia antes (`GEN_MODE`).
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

HEADER = "X-GMI-Key"
STORAGE_KEY = "firstframe.gmi_key"
REDACTED = "[gmi key redacted]"

_local = threading.local()


def set(key: str | None) -> None:  # noqa: A001 - el nombre corto es el bueno aqui
    """Ata una clave al thread actual. `None` o vacio = sin clave."""
    key = (key or "").strip()
    _local.key = key or None


def clear() -> None:
    _local.key = None


def key() -> str | None:
    """La clave del thread actual, si el usuario mando una."""
    return getattr(_local, "key", None)


def active() -> bool:
    return key() is not None


@contextmanager
def using(value: str | None):
    """`with byok.using(k):` — la suelta pase lo que pase."""
    previous = key()
    set(value)
    try:
        yield
    finally:
        set(previous)


def scrub(text: object) -> str:
    """Tacha la clave del thread en cualquier texto antes de que salga.

    Un stacktrace o un mensaje de error de httpx que arrastrara la cabecera
    filtraria la clave en el log. Esto lo hace imposible sin depender de que
    nadie se equivoque nunca.
    """
    out = str(text)
    k = key()
    if k and k in out:
        out = out.replace(k, REDACTED)
    return out


def demo() -> None:
    """Autocomprobacion: aislamiento por thread, scrub y limpieza."""
    assert key() is None and not active()
    set("sk-secreta-1234")
    assert active() and key() == "sk-secreta-1234"
    assert scrub("HTTP 401 con sk-secreta-1234 en la cabecera") == \
        f"HTTP 401 con {REDACTED} en la cabecera"

    seen: list[str | None] = []
    t = threading.Thread(target=lambda: seen.append(key()))
    t.start()
    t.join()
    assert seen == [None], f"la clave se escapo a otro thread: {seen}"

    with using("otra"):
        assert key() == "otra"
    assert key() == "sk-secreta-1234", "using() no restauro la clave anterior"

    clear()
    assert key() is None and scrub("nada que tachar") == "nada que tachar"
    print("byok demo OK: clave por thread, sin fugas a otros threads, scrub y clear")


if __name__ == "__main__":
    demo()
