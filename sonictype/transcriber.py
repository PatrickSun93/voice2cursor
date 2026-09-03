"""faster-whisper wrapper with hot model swapping.

Loading a model takes seconds, so the active model is kept resident and a swap
loads the replacement on a worker thread while the old one stays usable.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import numpy as np

from . import vocabulary
from .config import Config
from .cuda_setup import register_cuda_dlls


@dataclass
class Transcript:
    text: str
    language: str
    language_probability: float
    duration: float
    elapsed: float

    @property
    def speedup(self) -> float:
        return self.duration / self.elapsed if self.elapsed else 0.0


class CudaWedged(RuntimeError):
    """CUDA inference failed, so the context can no longer be trusted.

    Retrying a CUDA load in the same process after this has been observed to
    hang indefinitely at 0% CPU rather than raise, so the only safe response is
    to stop using CUDA for the life of the process.
    """


class Transcriber:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._model = None
        self._loaded_key: tuple[str, str, str] | None = None
        self._lock = threading.Lock()      # guards _model / _loaded_key
        self._infer_lock = threading.Lock()  # one transcription at a time
        self.device = "cpu"
        self.compute_type = "int8"
        self._cuda_failed = False

    # ---------- model lifecycle ----------

    def _resolve_device(self) -> str:
        if self._cuda_failed:
            return "cpu"
        if self.cfg.device != "auto":
            return self.cfg.device
        register_cuda_dlls()
        try:
            import ctranslate2

            return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            return "cpu"

    @staticmethod
    def _cpu_threads() -> int:
        """CTranslate2 defaults to a single CPU thread, which makes the CPU
        fallback far slower than the hardware allows."""
        return max(1, min(8, (os.cpu_count() or 2) // 2))

    def ensure_loaded(self, size: str | None = None) -> None:
        """Load the requested model if it is not already resident.

        Falls back to CPU if a CUDA load fails, so a broken cuDNN install
        degrades performance instead of breaking the app.
        """
        size = size or self.cfg.model_size
        device = self._resolve_device()
        compute = self.cfg.resolved_compute_type(device)
        key = (size, device, compute)

        with self._lock:
            if key == self._loaded_key:
                return

        register_cuda_dlls()
        from faster_whisper import WhisperModel

        def build(dev: str, ctype: str):
            kwargs = {"device": dev, "compute_type": ctype}
            if dev == "cpu":
                kwargs["cpu_threads"] = self._cpu_threads()
            return WhisperModel(size, **kwargs)

        try:
            model = build(device, compute)
        except Exception:
            if device == "cpu":
                raise
            # A failed CUDA *load* has not run any kernels, so the context is
            # still clean and falling back here is safe.
            device, compute = "cpu", "int8"
            model = build(device, compute)
            key = (size, device, compute)

        with self._lock:
            self._model = model
            self._loaded_key = key
            self.device, self.compute_type = device, compute

    def warmup(self) -> None:
        """Run one throwaway inference so the first real clip is not slow.

        The first GPU transcription in a fresh process pays a one-off ~10 s for
        cuBLAS/cuDNN kernel selection. Paying it at startup instead means the
        user's first dictation does not look like a hang.
        """
        self.ensure_loaded()
        silence = np.zeros(16_000, dtype=np.float32)
        with self._infer_lock:
            segments, _ = self._model.transcribe(silence, beam_size=1, vad_filter=False)
            list(segments)  # generator: nothing runs until it is consumed

    @property
    def loaded_size(self) -> str | None:
        return self._loaded_key[0] if self._loaded_key else None

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def describe(self) -> str:
        if not self._loaded_key:
            return "no model loaded"
        size, device, compute = self._loaded_key
        return f"{size} on {device} ({compute})"

    # ---------- inference ----------

    def _join_confident(self, segments) -> str:
        """Concatenate segments, dropping the ones Whisper is unsure about.

        Fed near-silence, Whisper reliably hallucinates short stock phrases
        ("Thank you.", "Mahala"). Both signals it exposes per segment are
        needed: `no_speech_prob` catches silence, `avg_logprob` catches
        low-confidence garbage in audio that is not silent.
        """
        kept = []
        for seg in segments:
            no_speech = getattr(seg, "no_speech_prob", 0.0) or 0.0
            avg_logprob = getattr(seg, "avg_logprob", 0.0) or 0.0
            if no_speech > self.cfg.max_no_speech:
                continue
            if avg_logprob < self.cfg.min_avg_logprob:
                continue
            kept.append(seg.text)
        return "".join(kept).strip()

    def _apply_vocabulary(self, text: str) -> str:
        """Snap known jargon to its correct spelling.

        Applied here rather than in the caller so every consumer - the paste,
        the review window, and the text handed to Ollama - sees the corrected
        transcript.
        """
        if not text or not self.cfg.vocabulary:
            return text
        vocab = vocabulary.load_cached(self.cfg.resolved_vocabulary_path())
        return vocab.apply(text) if vocab else text

    def transcribe(self, audio: np.ndarray) -> Transcript:
        import time

        if audio.size == 0:
            return Transcript("", "", 0.0, 0.0, 0.0)

        self.ensure_loaded()
        duration = audio.size / 16_000
        started = time.perf_counter()
        with self._infer_lock:
            try:
                segments, info = self._model.transcribe(
                    audio,
                    beam_size=self.cfg.beam_size,
                    language=self.cfg.language or None,
                    vad_filter=self.cfg.vad_filter,
                    initial_prompt=self.cfg.initial_prompt or None,
                    condition_on_previous_text=False,  # avoids runaway repetition on short clips
                )
                text = self._join_confident(segments)
            except Exception as exc:
                # A CUDA failure here (typically a cuBLAS/cuDNN DLL that loaded
                # but cannot run) leaves the context unusable. Latch it off
                # rather than reloading, which hangs instead of raising.
                if self.device == "cuda":
                    self._cuda_failed = True
                    raise CudaWedged(
                        f"CUDA inference failed ({type(exc).__name__}: {exc}). "
                        "Restart SonicType with --device cpu."
                    ) from exc
                raise
        return Transcript(
            text=self._apply_vocabulary(text),
            language=info.language or "",
            language_probability=float(info.language_probability or 0.0),
            duration=duration,
            elapsed=time.perf_counter() - started,
        )
