"""Datos fake para STUB=1. Shape identico al real (ver server/API.md).

Existe para que W3 (frontend) pueda desarrollar contra algo desde el minuto uno,
sin B2, sin pipeline y sin sqlite.
"""
from __future__ import annotations

import itertools
import time

_T0 = 1785000000000


def _scene(n, status, ms=None, title=None):
    return {
        "n": n,
        "status": status,
        "ms": ms,
        "title": title or f"Escena {n}",
        "path": f"runs/j_stub/scene-{n}/composite.mp4",
    }


def jobs():
    now = int(time.time() * 1000)
    return [
        {
            "id": "j_live01",
            "title": "Spot 15s zapatilla Aeron",
            "brief": "spot de 15s para una zapatilla de running, luz de amanecer, ciudad vacia",
            "status": "rendering",
            "scenes": [
                _scene(1, "ready", 21400, "Plano general de la ciudad al amanecer"),
                _scene(2, "ready", 19800, "Detalle de la suela"),
                _scene(3, "ready", 23100, "Corredor arrancando"),
                _scene(4, "rendering", None, "Zapatilla girando sobre fondo negro"),
                _scene(5, "pending", None, "Logo sobre asfalto mojado"),
                _scene(6, "pending", None, "Cierre con claim"),
            ],
            "scene_count": 6,
            "created_at": now - 64000,
            "created_at_iso": "2026-08-03T09:20:00Z",
            "first_frame_ms": 7120,
            "total_render_ms": None,
            "stream_url": "/stream/j_live01/index.m3u8",
            "manifest_url": None,
            "lock": None,
            "error": None,
        },
        {
            "id": "j_rev02",
            "title": "Teaser auriculares Nova",
            "brief": "teaser de 12s, producto flotando, fondo degradado azul",
            "status": "in_review",
            "scenes": [_scene(n, "ready", 18000 + n * 900) for n in range(1, 7)],
            "scene_count": 6,
            "created_at": _T0 - 600000,
            "created_at_iso": "2026-08-03T09:05:00Z",
            "first_frame_ms": 6840,
            "total_render_ms": 128400,
            "stream_url": "/stream/j_rev02/index.m3u8",
            "manifest_url": "/api/jobs/j_rev02/manifest",
            "lock": None,
            "error": None,
        },
        {
            "id": "j_apr03",
            "title": "Spot crema solar Helia",
            "brief": "spot 20s, playa, textura de crema en macro",
            "status": "approved",
            "scenes": [_scene(n, "ready", 17000 + n * 700) for n in range(1, 7)],
            "scene_count": 6,
            "created_at": _T0 - 3600000,
            "created_at_iso": "2026-08-03T08:20:00Z",
            "first_frame_ms": 7390,
            "total_render_ms": 151200,
            "stream_url": "/stream/j_apr03/index.m3u8",
            "manifest_url": "/api/jobs/j_apr03/manifest",
            "lock": {"mode": "GOVERNANCE", "retain_until": "2026-09-02T08:23:00Z"},
            "error": None,
        },
    ]


def job(job_id):
    for j in jobs():
        if j["id"] == job_id:
            return j
    return None


def provider_events(job_id):
    now = int(time.time() * 1000)
    return [
        {"at": now - 60000, "scene": 1, "kind": "provider_call", "provider": "nvidia-nim",
         "model": "black-forest-labs/flux.1-schnell", "detail": "keyframe ok"},
        {"at": now - 58000, "scene": 1, "kind": "judge_score", "provider": "nvidia-nim",
         "model": "meta/llama-3.2-90b-vision-instruct", "score": 0.42,
         "detail": "logo ilegible, se refina"},
        {"at": now - 55000, "scene": 1, "kind": "judge_score", "provider": "nvidia-nim",
         "model": "meta/llama-3.2-90b-vision-instruct", "score": 0.81, "detail": "ok"},
        {"at": now - 40000, "scene": 3, "kind": "provider_failover", "provider": "gmicloud",
         "model": "pixverse-v5.6", "fallback_model": "seedance-2-0",
         "detail": "MODEL_ERROR: chaos injected"},
    ]


def manifest(job_id):
    return {
        "job_id": job_id,
        "created_at": "2026-08-03T09:05:00Z",
        "pipeline": "firstframe/scene",
        "scenes": [
            {
                "n": n,
                "run_id": f"run_{job_id}_{n}",
                "parent_run_id": f"run_{job_id}_{n - 1}" if n > 1 else None,
                "steps": [
                    {"name": "keyframe", "provider": "nvidia-nim", "model": "black-forest-labs/flux.1-schnell"},
                    {"name": "voiceover", "provider": "openai", "model": "tts-1"},
                    {"name": "clip", "provider": "gmicloud", "model": "pixverse-v5.6"},
                    {"name": "composite", "provider": "ffmpeg", "model": "FFmpegCompositor"},
                ],
                "assets": [{"key": f"runs/{job_id}/scene-{n}/composite.mp4",
                            "sha256": f"{n:064x}"}],
            }
            for n in range(1, 7)
        ],
    }


_seq = itertools.count(1)


def next_event():
    """Un evento SSE sintetico, para que la UI se mueva sola en modo stub."""
    n = next(_seq)
    now = int(time.time() * 1000)
    kinds = [
        ("segment_landed", {"job_id": "j_live01", "at": now, "seq": n,
                            "key": f"incoming/j_live01/seg/{n:05d}.ts", "duration": 4.0}),
        ("scene_ready", {"job_id": "j_live01", "at": now, "scene": (n % 6) + 1,
                         "ms": 20000 + n * 100, "job": job("j_live01")}),
        ("provider_failover", {"job_id": "j_live01", "at": now, "scene": 3,
                               "provider": "gmicloud", "model": "pixverse-v5.6",
                               "fallback_model": "seedance-2-0"}),
        ("judge_score", {"job_id": "j_live01", "at": now, "scene": 2,
                         "score": 0.4 + (n % 5) / 10, "iteration": 1}),
    ]
    return kinds[n % len(kinds)]
