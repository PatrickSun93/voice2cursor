"""mlx-whisper: the Apple Silicon GPU.

faster-whisper runs on this hardware but only on the CPU, several times slower
for the same model, so on a Mac this is the engine worth having.

Model naming differs from faster-whisper's: mlx takes a Hugging Face repo id,
not a size token. Size tokens are accepted anyway and mapped to the
mlx-community conversions, so one `model` setting means the same thing in both
config files.
"""

from __future__ import annotations

import threading

import numpy as np

from .base import EngineError, Result, Segment

# Size token -> the mlx-community conversion of that model. Anything with a
# "/" in it is passed straight through as a repo id.
_SIZE_TO_REPO = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    # No distil conversion upstream; turbo is the fast large, which is what
    # anyone reaching for distil-large-v3 actually wants.
    "distil-large-v3": "mlx-community/whisper-large-v3-turbo",
    "turbo": "mlx-community/whisper-large-v3-turbo",
}


def resolve_repo(model: str) -> str:
    if "/" in model:
        return model
    return _SIZE_TO_REPO.get(model, _SIZE_TO_REPO["large-v3"])


class MlxWhisperEngine:
    name = "mlx_whisper"

    MODELS = tuple(_SIZE_TO_REPO)
    DEFAULT_MODEL = "turbo"

    def __init__(self, device: str = "auto", compute_type: str = "auto"):
        # mlx picks the GPU on its own and has no compute-type knob; both are
        # accepted so the engines are constructed identically.
        self._mlx = None
        self._repo: str | None = None
        self._infer_lock = threading.Lock()
        self.device = "mlx"
        self.compute_type = "float16"

    def available(self) -> bool:
        try:
            import mlx_whisper  # noqa: F401

            return True
        except Exception:
            return False

    def load(self, model: str) -> None:
        """mlx-whisper has no separate load step -- it caches by repo id on
        first transcribe -- so this only resolves and remembers the name."""
        if self._mlx is None:
            try:
                import mlx_whisper
            except Exception as exc:
                raise EngineError(f"mlx-whisper is not installed: {exc}") from exc
            self._mlx = mlx_whisper
        self._repo = resolve_repo(model)

    def warmup(self) -> None:
        """Transcribe one second of silence, which downloads and resides the
        weights so the first real clip is not several seconds slower."""
        with self._infer_lock:
            self._mlx.transcribe(np.zeros(16_000, dtype=np.float32),
                                 path_or_hf_repo=self._repo)

    @property
    def loaded_model(self) -> str | None:
        return self._repo

    def describe(self) -> str:
        return f"{self._repo} on mlx" if self._repo else "no model loaded"

    def transcribe(self, audio, *, beam_size: int = 5, language: str | None = None,
                   vad_filter: bool = True, initial_prompt: str | None = None) -> Result:
        # beam_size and vad_filter have no mlx equivalent and are accepted only
        # to keep one call signature across engines.
        opts: dict = {"condition_on_previous_text": False}
        if language:
            opts["language"] = language
        if initial_prompt:
            opts["initial_prompt"] = initial_prompt

        with self._infer_lock:
            result = self._mlx.transcribe(audio, path_or_hf_repo=self._repo, **opts)

        raw = result.get("segments") or []
        if raw:
            segments = [
                Segment(
                    text=seg.get("text", ""),
                    no_speech_prob=seg.get("no_speech_prob"),
                    avg_logprob=seg.get("avg_logprob"),
                )
                for seg in raw
            ]
        else:
            # Older builds return only the joined text. One segment with
            # unknown confidence is honest; zeros would claim certainty.
            segments = [Segment(text=result.get("text", ""))]

        return Result(
            segments=segments,
            language=result.get("language") or "",
            language_probability=0.0,  # mlx does not report one
        )
