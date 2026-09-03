"""Transcription engines, chosen by config or by what this host can run.

`create()` honours an explicit choice, otherwise takes the backend's preferred
engine and falls back to the other if it is not installed -- so a Mac without
mlx-whisper still transcribes on faster-whisper's CPU path rather than refusing
to start.
"""

from __future__ import annotations

from .base import Engine, EngineError, Result, Segment

_ORDER = ("faster_whisper", "mlx_whisper")


def _build(name: str, device: str, compute_type: str):
    if name == "faster_whisper":
        from .faster_whisper_engine import FasterWhisperEngine

        return FasterWhisperEngine(device, compute_type)
    if name == "mlx_whisper":
        from .mlx_whisper_engine import MlxWhisperEngine

        return MlxWhisperEngine(device, compute_type)
    raise EngineError(f"unknown engine {name!r} (known: {', '.join(_ORDER)})")


def create(name: str = "auto", device: str = "auto", compute_type: str = "auto"):
    """The engine to use, already constructed.

    `name` of "auto" asks the platform backend what it prefers.
    """
    if name != "auto":
        engine = _build(name, device, compute_type)
        if not engine.available():
            raise EngineError(
                f"engine {name!r} is configured but not installed on this host"
            )
        return engine

    from .. import backends

    preferred = backends.DEFAULT_ENGINE
    candidates = [preferred] + [n for n in _ORDER if n != preferred]
    tried = []
    for candidate in candidates:
        engine = _build(candidate, device, compute_type)
        if engine.available():
            return engine
        tried.append(candidate)
    raise EngineError(
        "no transcription engine installed (tried: " + ", ".join(tried) + ")"
    )


def available_models(engine) -> tuple:
    return getattr(engine, "MODELS", ())


__all__ = ["create", "available_models", "Engine", "EngineError", "Result", "Segment"]
