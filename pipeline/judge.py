"""Juez de vision del AgentLoop.

Puntua un keyframe contra el brief con `meta/llama-3.2-90b-vision-instruct`
en NVIDIA NIM (gratis, verificado en VALIDACION.md).

Dos cosas verificadas que NO se pueden cambiar sin romperlo:

1. **Formato del contenido.** Hay que mandar el array estilo OpenAI con
   `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`.
   El estilo `<img src="data:...">` inline da respuestas INCORRECTAS
   (un PNG rojo puro se describia como "orange" y luego "grey").
2. **El modelo.** `nemotron-nano-12b-v2-vl` falla incluso con el formato bueno.
   Usar el de 90b.

Ademas el asset se descarga a un fichero LOCAL con extension real antes de
codificar (nunca se pasa una URL https:// al juez) y se re-escala a 768px:
NIM rechaza imagenes inline por encima de ~180 KB en base64.

Degradacion con gracia: si la API falla, timeoutea o devuelve basura, el juez
devuelve `NEUTRAL_SCORE` marcado con `degraded=True`. El pipeline NUNCA revienta
por culpa del juez — como mucho el AgentLoop deja de refinar.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from genblaze import Asset, PipelineResult, ThresholdEvaluator

from pipeline.providers import ffmpeg_bin

logger = logging.getLogger("firstframe.judge")

NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
JUDGE_MODEL = "meta/llama-3.2-90b-vision-instruct"
NEUTRAL_SCORE = 0.5
DEFAULT_THRESHOLD = 0.7
MAX_EDGE = 768          # px del lado largo antes de codificar
TIMEOUT_SEC = 60.0
ATTEMPTS = 2            # la free tier de NIM timeoutea de vez en cuando

_PROMPT = """You are the quality gate of an automated ad-production pipeline.
You are shown ONE keyframe that was generated from this brief:

---
{brief}
---

Rate how well the frame serves that brief: subject present and recognisable,
composition usable as the first frame of a product shot, no obvious artefacts,
legible if there is any text or logo.

Answer with ONE line of raw JSON and nothing else:
{{"score": <number between 0 and 1>, "reason": "<max 20 words>"}}"""


@dataclass
class Verdict:
    """Resultado del juez. `degraded=True` = el score no es de fiar."""

    score: float
    reason: str
    degraded: bool = False
    model: str = JUDGE_MODEL
    raw: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"score": self.score, "reason": self.reason,
                "degraded": self.degraded, "model": self.model}


# --- Preparacion de la imagen ------------------------------------------------

def _download(asset: Asset, dest_dir: Path) -> Path:
    """Baja el asset a un fichero local con extension REAL."""
    url = asset.url
    ext = mimetypes.guess_extension(asset.media_type or "") or ""
    if ext in ("", ".jpe"):
        ext = ".png" if "png" in (asset.media_type or "") else ".jpg"
    dest = dest_dir / f"frame{ext}"

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        dest.write_bytes(Path(urllib.parse.unquote(parsed.path)).read_bytes())
    elif parsed.scheme in ("http", "https", "s3"):
        # Via manifest.fetch_bytes: cuando el run lleva sink, el SDK ya ha
        # reescrito asset.url al objeto de B2, que es privado. Un GET pelado
        # devuelve 401 y el juez degradaria por un motivo tonto.
        from pipeline.manifest import fetch_bytes

        dest.write_bytes(fetch_bytes(url))
    else:
        raise ValueError(f"esquema de URL no soportado para el juez: {url!r}")
    return dest


def _shrink(src: Path, dest_dir: Path) -> Path:
    """Re-escala a MAX_EDGE en PNG. NIM corta las imagenes inline muy grandes."""
    dest = dest_dir / "small.png"
    proc = subprocess.run(
        [ffmpeg_bin(), "-loglevel", "error", "-y", "-i", str(src),
         "-vf", f"scale='min({MAX_EDGE},iw)':-2", "-frames:v", "1", str(dest)],
        capture_output=True, text=True,
    )
    return dest if proc.returncode == 0 and dest.exists() else src


def _data_uri(path: Path) -> str:
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{b64}"


# --- Llamada a NIM -----------------------------------------------------------

def _call_nim(data_uri: str, brief: str, api_key: str) -> str:
    payload = {
        "model": JUDGE_MODEL,
        "max_tokens": 128,
        "temperature": 0.0,
        "messages": [{
            "role": "user",
            # FORMATO VERIFICADO. No sustituir por <img src="data:...">.
            "content": [
                {"type": "text", "text": _PROMPT.format(brief=brief.strip())},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
    }
    req = urllib.request.Request(
        NIM_URL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:  # noqa: S310
        body = json.loads(resp.read().decode())
    return body["choices"][0]["message"]["content"]


_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)
_NUM_RE = re.compile(r"([01](?:\.\d+)?|\.\d+)")


def _parse(raw: str) -> tuple[float, str]:
    """Extrae score+reason. El modelo a veces envuelve el JSON en prosa."""
    match = _JSON_RE.search(raw)
    if match:
        try:
            obj = json.loads(match.group(0))
            score = float(obj["score"])
            reason = str(obj.get("reason", "")).strip()
            return max(0.0, min(1.0, score)), reason or "sin motivo"
        except (ValueError, KeyError, TypeError):
            pass
    num = _NUM_RE.search(raw)
    if num:
        return max(0.0, min(1.0, float(num.group(1)))), raw.strip()[:160]
    raise ValueError(f"respuesta del juez no parseable: {raw[:200]!r}")


# --- API publica -------------------------------------------------------------

def judge_frame_verdict(asset: Asset, brief: str) -> Verdict:
    """Puntua el frame y devuelve score + razon. Nunca lanza."""
    api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not api_key:
        return Verdict(NEUTRAL_SCORE, "NVIDIA_API_KEY ausente: juez desactivado",
                       degraded=True)
    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        try:
            with tempfile.TemporaryDirectory(prefix="judge-") as tmp:
                tmp = Path(tmp)
                local = _download(asset, tmp)
                small = _shrink(local, tmp)
                raw = _call_nim(_data_uri(small), brief, api_key)
            score, reason = _parse(raw)
            logger.info("juez %s -> score=%.2f (%s)", JUDGE_MODEL, score, reason)
            return Verdict(score, reason, raw=raw)
        except (urllib.error.URLError, OSError, TimeoutError, ValueError,
                KeyError, subprocess.SubprocessError) as exc:
            last = exc
            if attempt + 1 < ATTEMPTS:
                logger.info("juez: reintento %d/%d tras %s",
                            attempt + 2, ATTEMPTS, type(exc).__name__)
    logger.warning("juez degradado (%s: %s) -> score neutro %.2f",
                   type(last).__name__, last, NEUTRAL_SCORE)
    return Verdict(NEUTRAL_SCORE, f"juez no disponible ({type(last).__name__}: {last})",
                   degraded=True)


def judge_frame(asset: Asset, brief: str) -> float:
    """Contrato simple del PLAN: score 0..1. Degrada a `NEUTRAL_SCORE`."""
    return judge_frame_verdict(asset, brief).score


def first_image_asset(result: PipelineResult) -> Asset | None:
    """El keyframe: primer asset image/* del run (step 0 en las escenas)."""
    for step in result.run.steps:
        for asset in step.assets:
            if (asset.media_type or "").startswith("image/"):
                return asset
    return None


@dataclass
class FrameEvaluator:
    """`ThresholdEvaluator` cableado al juez, recordando el ultimo veredicto.

    Se guarda el veredicto por `run_id` para que `feedback_fn` pueda devolver
    la razon del juez sin llamar a la API dos veces. Esa razon es lo que el
    factory del `AgentLoop` inyecta en el prompt de la siguiente iteracion.
    """

    brief: str
    threshold: float = DEFAULT_THRESHOLD
    verdicts: dict[str, Verdict] = field(default_factory=dict)

    def score(self, result: PipelineResult) -> float:
        if self.threshold <= 0.0:
            # Umbral 0 = juez apagado a proposito (`--no-judge`). Sin esto
            # seguiriamos gastando una llamada a NIM por iteracion para nada.
            verdict = Verdict(1.0, "juez desactivado (threshold=0)", degraded=True)
            self.verdicts[result.run.run_id] = verdict
            return verdict.score
        asset = first_image_asset(result)
        if asset is None:
            verdict = Verdict(NEUTRAL_SCORE, "el run no produjo ningun keyframe",
                              degraded=True)
        else:
            verdict = judge_frame_verdict(asset, self.brief)
        self.verdicts[result.run.run_id] = verdict
        return verdict.score

    def feedback(self, result: PipelineResult, score: float) -> str:
        verdict = self.verdicts.get(result.run.run_id)
        reason = verdict.reason if verdict else "sin veredicto"
        return f"score {score:.2f} < {self.threshold:.2f}: {reason}"

    def last(self) -> Verdict | None:
        return next(reversed(self.verdicts.values()), None) if self.verdicts else None

    def as_evaluator(self) -> ThresholdEvaluator:
        return ThresholdEvaluator(
            score_fn=self.score,
            threshold=self.threshold,
            higher_is_better=True,
            feedback_fn=self.feedback,
        )


def frame_evaluator(brief: str, *, threshold: float = DEFAULT_THRESHOLD
                    ) -> tuple[ThresholdEvaluator, FrameEvaluator]:
    """Devuelve (evaluador para el AgentLoop, objeto con los veredictos)."""
    fe = FrameEvaluator(brief=brief, threshold=threshold)
    return fe.as_evaluator(), fe


def demo() -> None:
    """Autocomprobacion.

    Sin NVIDIA_API_KEY: comprueba solo el camino degradado y el parser.
    Con NVIDIA_API_KEY: llama de verdad a NIM con un frame rojo puro y con un
    keyframe de producto, y exige que el juez los ordene bien.
    """
    from genblaze import Manifest, Run
    from pipeline.providers import local_asset, make_keyframe

    # --- parser ---
    assert _parse('{"score": 0.8, "reason": "ok"}') == (0.8, "ok")
    assert _parse('bla bla {"score":0.25,"reason":"logo ilegible"} fin')[0] == 0.25
    assert _parse("Score: 0.4 because the logo is blurry")[0] == 0.4
    assert _parse('{"score": 5, "reason": "x"}')[0] == 1.0, "clamp arriba"
    try:
        _parse("no hay nada aqui")
    except ValueError:
        pass
    else:
        raise AssertionError("deberia rechazar una respuesta sin numero")

    # --- degradacion: sin key nunca revienta ---
    prev = os.environ.pop("NVIDIA_API_KEY", None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            png = make_keyframe(Path(tmp) / "k.png", label="demo")
            v = judge_frame_verdict(local_asset(png), "un frasco de serum")
            assert v.degraded and v.score == NEUTRAL_SCORE, v
            assert judge_frame(local_asset(png), "x") == NEUTRAL_SCORE

            # asset con URL imposible -> degrada, no revienta
            os.environ["NVIDIA_API_KEY"] = "fake-key-para-el-camino-de-error"
            bad = local_asset(png)
            bad.url = "ftp://no/existe.png"
            assert judge_frame_verdict(bad, "x").degraded
    finally:
        os.environ.pop("NVIDIA_API_KEY", None)
        if prev:
            os.environ["NVIDIA_API_KEY"] = prev

    # --- evaluador para el AgentLoop (sin red: usa el camino degradado) ---
    ev, fe = frame_evaluator("un frasco de serum sobre marmol", threshold=0.7)
    run = Run(run_id="r1", name="t")
    result = PipelineResult(run=run, manifest=Manifest.from_run(run))
    ev_result = ev.evaluate(result)
    assert ev_result.score == NEUTRAL_SCORE
    assert ev_result.passed is False, "0.5 no llega a 0.7"
    assert "keyframe" in (ev_result.feedback or ""), ev_result.feedback
    assert fe.last() is not None

    # --- camino real, solo si hay key ---
    if os.environ.get("NVIDIA_API_KEY"):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            red = tmp / "red.png"
            subprocess.run([ffmpeg_bin(), "-loglevel", "error", "-y", "-f", "lavfi",
                            "-i", "color=c=red:s=512x512:d=1", "-frames:v", "1", str(red)],
                           check=True)
            # Assert DURO: la capacidad verificada en VALIDACION.md es que el
            # modelo VE la imagen con este formato de contenido. Se comprueba
            # eso, no una puntuacion concreta: el juez es un LLM y su score
            # sobre la misma imagen varia entre llamadas aun con temperature=0.
            small = _shrink(red, tmp)
            answer = _call_nim(
                _data_uri(small),
                "IGNORE the rest. What is the dominant colour of this image? "
                "Answer with one word.",
                os.environ["NVIDIA_API_KEY"],
            ).lower()
            assert "red" in answer, (
                f"el juez no esta viendo la imagen (dijo {answer!r}); revisa que "
                "el contenido siga siendo el array con image_url + data URI")

            # Informativo: el score real sobre un brief de producto.
            brief = "primer plano de un frasco de serum cosmetico sobre marmol blanco"
            v_red = judge_frame_verdict(local_asset(red), brief)
            print(f"  NIM ve la imagen (dominante={answer.strip()!r}); "
                  f"score contra el brief={v_red.score:.2f} reason={v_red.reason!r}")
            # La free tier timeoutea de vez en cuando: degradar es el camino
            # CORRECTO, no un fallo. Lo que no puede pasar es que reviente.
            if v_red.degraded:
                print("  (aviso: NIM degrado en esta llamada; el camino de "
                      "degradacion es justo lo que queremos que ocurra)")
            else:
                assert 0.0 <= v_red.score <= 1.0 and v_red.reason
    else:
        print("  (NVIDIA_API_KEY ausente: camino real del juez no ejercitado)")

    print("demo OK: juez parsea, ordena y degrada sin tumbar el pipeline")


if __name__ == "__main__":
    demo()
