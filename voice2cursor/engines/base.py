"""The contract every transcription engine implements.

Two engines exist because the hardware genuinely differs: faster-whisper runs
on CUDA, mlx-whisper on the Apple Silicon GPU, and neither works well on the
other's host. They disagree about more than speed, so the shape here is the
narrow part they can both honestly fill.

The important disagreement is confidence. faster-whisper reports
`no_speech_prob` and `avg_logprob` per segment, which is what lets the caller
throw away the stock phrases Whisper hallucinates from near-silence. Engines
that report nothing set those to None, which means "unknown" -- not "zero".
Filtering treats unknown as keep, because dropping real speech is the worse
failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass
class Segment:
    text: str
    no_speech_prob: float | None = None
    avg_logprob: float | None = None


@dataclass
class Result:
    segments: Sequence[Segment]
    language: str = ""
    language_probability: float = 0.0


class EngineError(RuntimeError):
    """The engine could not load or run."""


class Engine(Protocol):
    name: str

    def available(self) -> bool:
        """True if this host can actually run the engine."""

    def load(self, model: str) -> None:
        """Make `model` resident. Cheap when it already is."""

    def warmup(self) -> None:
        """Run one throwaway inference so the first real clip is not slow."""

    def transcribe(self, audio, **opts) -> Result:
        """Transcribe float32 mono 16 kHz audio."""

    def describe(self) -> str:
        """One line naming the model and where it is running."""

    @property
    def loaded_model(self) -> str | None:
        ...
