"""faster-whisper: CUDA where there is an NVIDIA card, CPU where there is not.

Behaviour here is unchanged from the Windows-only build -- the CUDA fallback,
the wedged-context latch and the CPU thread count are all load-bearing and
were arrived at the hard way.
"""

from __future__ import annotations

import os
import threading
import time

import numpy as np

from ..cuda_setup import register_cuda_dlls
from .base import EngineError, Result, Segment


class CudaWedged(EngineError):
    """CUDA inference failed, so the context can no longer be trusted.

    Retrying a CUDA load in the same process after this has been observed to
    hang indefinitely at 0% CPU rather than raise, so the only safe response is
    to stop using CUDA for the life of the process.
    """


class FasterWhisperEngine:
    name = "faster_whisper"

    # What the tray menu offers. Downloaded on first use, cached under
    # ~/.cache/huggingface.
    MODELS = ("tiny", "base", "small", "medium", "large-v3", "distil-large-v3")
    DEFAULT_MODEL = "small"

    def __init__(self, device: str = "auto", compute_type: str = "auto"):
        self._device_pref = device
        self._compute_pref = compute_type
        self._model = None
        self._key: tuple[str, str, str] | None = None
        self._lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self.device = "cpu"
        self.compute_type = "int8"
        self._cuda_failed = False

    # ---------- availability ----------

    def available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401

            return True
        except Exception:
            return False

    def _resolve_device(self) -> str:
        if self._cuda_failed:
            return "cpu"
        if self._device_pref != "auto":
            return self._device_pref
        register_cuda_dlls()
        try:
            import ctranslate2

            return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            return "cpu"

    def _resolve_compute(self, device: str) -> str:
        if self._compute_pref != "auto":
            return self._compute_pref
        return "float16" if device == "cuda" else "int8"

    @staticmethod
    def _cpu_threads() -> int:
        """CTranslate2 defaults to a single CPU thread, which makes the CPU
        fallback far slower than the hardware allows."""
        return max(1, min(8, (os.cpu_count() or 2) // 2))

    # ---------- lifecycle ----------

    def load(self, model: str) -> None:
        device = self._resolve_device()
        compute = self._resolve_compute(device)
        key = (model, device, compute)

        with self._lock:
            if key == self._key:
                return

        register_cuda_dlls()
        from faster_whisper import WhisperModel

        def build(dev: str, ctype: str):
            kwargs = {"device": dev, "compute_type": ctype}
            if dev == "cpu":
                kwargs["cpu_threads"] = self._cpu_threads()
            return WhisperModel(model, **kwargs)

        try:
            built = build(device, compute)
        except Exception:
            if device == "cpu":
                raise
            # A failed CUDA *load* has not run any kernels, so the context is
            # still clean and falling back here is safe.
            device, compute = "cpu", "int8"
            built = build(device, compute)
            key = (model, device, compute)

        with self._lock:
            self._model = built
            self._key = key
            self.device, self.compute_type = device, compute

    def warmup(self) -> None:
        """The first GPU transcription in a fresh process pays a one-off ~10 s
        for cuBLAS/cuDNN kernel selection. Pay it at startup so the user's
        first dictation does not look like a hang."""
        silence = np.zeros(16_000, dtype=np.float32)
        with self._infer_lock:
            segments, _ = self._model.transcribe(silence, beam_size=1, vad_filter=False)
            list(segments)  # generator: nothing runs until it is consumed

    @property
    def loaded_model(self) -> str | None:
        return self._key[0] if self._key else None

    def describe(self) -> str:
        if not self._key:
            return "no model loaded"
        model, device, compute = self._key
        return f"{model} on {device} ({compute})"

    # ---------- inference ----------

    def transcribe(self, audio, *, beam_size: int = 5, language: str | None = None,
                   vad_filter: bool = True, initial_prompt: str | None = None) -> Result:
        with self._infer_lock:
            try:
                segments, info = self._model.transcribe(
                    audio,
                    beam_size=beam_size,
                    language=language or None,
                    vad_filter=vad_filter,
                    initial_prompt=initial_prompt or None,
                    condition_on_previous_text=False,  # avoids runaway repetition on short clips
                )
                out = [
                    Segment(
                        text=seg.text,
                        no_speech_prob=getattr(seg, "no_speech_prob", None),
                        avg_logprob=getattr(seg, "avg_logprob", None),
                    )
                    for seg in segments
                ]
            except Exception as exc:
                # A CUDA failure here (typically a cuBLAS/cuDNN DLL that loaded
                # but cannot run) leaves the context unusable. Latch it off
                # rather than reloading, which hangs instead of raising.
                if self.device == "cuda":
                    self._cuda_failed = True
                    raise CudaWedged(
                        f"CUDA inference failed ({type(exc).__name__}: {exc}). "
                        "Restart with --device cpu."
                    ) from exc
                raise
        return Result(
            segments=out,
            language=getattr(info, "language", "") or "",
            language_probability=float(getattr(info, "language_probability", 0.0) or 0.0),
        )
