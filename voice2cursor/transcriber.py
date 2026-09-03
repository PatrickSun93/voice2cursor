"""Model lifecycle and the confidence filter, on top of a pluggable engine.

The engine knows how to run a model on this hardware; everything here is the
same on every host -- keeping one model resident, swapping it without dropping
the old one first, filtering out what Whisper invented, and snapping jargon to
its correct spelling.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

from . import engines, vocabulary
from .config import Config
# Re-exported: callers catch this to fall back to CPU, and it would be a poor
# trade to make them import it from an engine module they otherwise never touch.
from .engines.faster_whisper_engine import CudaWedged  # noqa: F401


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


class Transcriber:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._engine = engines.create(cfg.engine, cfg.device, cfg.compute_type)
        self._lock = threading.Lock()

    # ---------- engine passthrough ----------

    @property
    def engine_name(self) -> str:
        return self._engine.name

    @property
    def models(self) -> tuple:
        """What this engine offers, for the tray menu."""
        return engines.available_models(self._engine)

    @property
    def device(self) -> str:
        return getattr(self._engine, "device", "")

    @property
    def compute_type(self) -> str:
        return getattr(self._engine, "compute_type", "")

    @property
    def loaded_size(self) -> str | None:
        return self._engine.loaded_model

    @property
    def is_loaded(self) -> bool:
        return self._engine.loaded_model is not None

    def describe(self) -> str:
        return self._engine.describe()

    def ensure_loaded(self, size: str | None = None) -> None:
        with self._lock:
            self._engine.load(size or self.cfg.model_size)

    def warmup(self) -> None:
        self.ensure_loaded()
        self._engine.warmup()

    # ---------- inference ----------

    def _join_confident(self, segments) -> str:
        """Concatenate segments, dropping the ones Whisper is unsure about.

        Fed near-silence, Whisper reliably hallucinates short stock phrases
        ("Thank you.", "Mahala"). Both signals it exposes per segment are
        needed: `no_speech_prob` catches silence, `avg_logprob` catches
        low-confidence garbage in audio that is not silent.

        A None means the engine does not report that signal, which is not the
        same as reporting a bad score -- treat it as keep. Discarding real
        speech is the worse of the two failures.
        """
        kept = []
        for seg in segments:
            if seg.no_speech_prob is not None and seg.no_speech_prob > self.cfg.max_no_speech:
                continue
            if seg.avg_logprob is not None and seg.avg_logprob < self.cfg.min_avg_logprob:
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
        if audio.size == 0:
            return Transcript("", "", 0.0, 0.0, 0.0)

        self.ensure_loaded()
        duration = audio.size / 16_000
        started = time.perf_counter()
        result = self._engine.transcribe(
            audio,
            beam_size=self.cfg.beam_size,
            language=self.cfg.language or None,
            vad_filter=self.cfg.vad_filter,
            initial_prompt=self.cfg.initial_prompt or None,
        )
        return Transcript(
            text=self._apply_vocabulary(self._join_confident(result.segments)),
            language=result.language,
            language_probability=result.language_probability,
            duration=duration,
            elapsed=time.perf_counter() - started,
        )
