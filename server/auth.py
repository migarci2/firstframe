"""Muro de acceso de la demo.

Esto NO es un sistema de identidad: no hay usuarios, ni contrasenas, ni registro.
Es un unico codigo compartido (`DEMO_ACCESS_CODE`, por defecto `FIRSTFRAME`) que
abre la instancia publica, y una cookie firmada que evita repetirlo en cada carga.

Por que la cookie no guarda el codigo: si el valor fuese el propio codigo, cualquier
extension o log que lea cookies se lo lleva en claro. Se guarda un HMAC derivado del
codigo, asi que ademas *rotar el codigo invalida todas las sesiones* de golpe, que es
justo lo que se quiere despues de un hackathon.

Autocomprobacion:  .venv/bin/python -m server.auth
"""
from __future__ import annotations

import hashlib
import hmac
import os

COOKIE = "ff_access"
MAX_AGE = 30 * 24 * 3600          # 30 dias: un juez no deberia volver a escribirlo
_MSG = b"firstframe-demo-access-v1"


def code() -> str:
    """El codigo vigente. Cadena vacia = muro desactivado."""
    raw = os.getenv("DEMO_ACCESS_CODE")
    return (raw if raw is not None else "FIRSTFRAME").strip()


def enabled() -> bool:
    """`DEMO_ACCESS_CODE=` (vacio) deja la instancia abierta — util en local."""
    return code() != ""


def token() -> str:
    return hmac.new(code().encode("utf-8"), _MSG, hashlib.sha256).hexdigest()


def check_code(given: str | None) -> bool:
    # Comparacion en tiempo constante y sin distinguir mayusculas: el codigo se
    # teclea desde un movil con autocapitalizacion.
    a = (given or "").strip().upper()
    b = code().upper()
    return bool(b) and hmac.compare_digest(a, b)


def check_cookie(value: str | None) -> bool:
    return bool(value) and hmac.compare_digest(value, token())


def demo() -> None:
    os.environ["DEMO_ACCESS_CODE"] = "SECRETO"
    assert enabled() is True
    assert check_code("secreto") is True
    assert check_code(" Secreto ") is True
    assert check_code("otro") is False
    assert check_cookie(token()) is True
    assert check_cookie("x" * 64) is False
    # rotar el codigo invalida la cookie anterior
    viejo = token()
    os.environ["DEMO_ACCESS_CODE"] = "OTRO"
    assert check_cookie(viejo) is False
    os.environ["DEMO_ACCESS_CODE"] = ""
    assert enabled() is False
    assert check_code("") is False
    del os.environ["DEMO_ACCESS_CODE"]
    assert code() == "FIRSTFRAME"
    print("auth.demo OK")


if __name__ == "__main__":
    demo()
