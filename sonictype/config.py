r"""Persistent settings, stored as JSON under %APPDATA%\SonicType\config.json."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields

APP_NAME = "SonicType"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# Offered in the tray menu. Downloaded on first use, cached in ~/.cache/huggingface.
MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3", "distil-large-v3")

DEFAULT_POLISH_PROMPT = (
    "You are a transcript editor. Rewrite the text below so it reads as clean prose:\n"
    "fix punctuation and capitalisation, drop filler words (um, uh, you know, like),\n"
    "remove false starts and stutters, and break it into paragraphs.\n"
    "Do not summarise, reword, translate, or add anything that is not already said.\n"
    "Reply with the corrected text only - no preamble, no explanation, no quote marks.\n\n"
    "TEXT:\n{text}"
)


@dataclass
class Config:
    # --- transcription ---
    model_size: str = "small"
    device: str = "auto"          # auto | cuda | cpu
    compute_type: str = "auto"    # auto -> float16 on cuda, int8 on cpu
    language: str | None = None   # None -> autodetect
    beam_size: int = 5
    vad_filter: bool = True       # drop silence; big win on push-to-talk clips
    initial_prompt: str = ""      # seed vocabulary, e.g. names or jargon

    # Whisper invents words when fed near-silence - a stray "Mahala" or "Thank
    # you." pasting into your editor is worse than nothing. Segments are dropped
    # when the model is this unsure. Raise max_no_speech toward 1.0, or lower
    # min_avg_logprob toward -2.0, if real speech is being discarded.
    max_no_speech: float = 0.6    # drop segment if no_speech_prob exceeds this
    min_avg_logprob: float = -1.0  # drop segment if avg_logprob falls below this

    # --- vocabulary ---
    # Site jargon Whisper cannot know: panel names, daemon identifiers, brand
    # spellings. `initial_prompt` biases the decoder but caps out around 60
    # terms; the vocabulary file corrects the transcript afterwards and has no
    # size limit. See vocabulary.py.
    vocabulary: bool = True
    vocabulary_path: str = ""     # empty -> vocabulary.json beside config.json

    # --- audio ---
    # Device NAME, not index. PortAudio indices shift whenever a device appears
    # or disappears - connecting a Bluetooth headset renumbered everything after
    # it in testing - so a saved index silently starts pointing at a different
    # microphone. None means the system default.
    input_device: str | None = None

    # --- hotkeys ---
    record_hotkey: str = "ctrl+alt+space"
    polish_hotkey: str = "ctrl+alt+p"
    review_hotkey: str = "ctrl+alt+r"
    hotkey_mode: str = "hold"     # hold (push-to-talk) | toggle

    # --- output ---
    output_mode: str = "paste"    # paste (clipboard + Ctrl+V) | type | clipboard
    restore_clipboard: bool = True
    auto_polish: bool = False     # run Ollama before pasting
    open_review_window: bool = False

    # --- ollama ---
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""        # empty -> first model Ollama reports

    # Ollama evicts an idle model after ~5 minutes by default, so the first
    # polish after a break pays a multi-second reload while a warm one takes
    # ~0.2 s. Holding it resident costs VRAM that Whisper also wants: a 3B model
    # is ~2.6 GB, so on an 8 GB card pair it with `small` or `medium`, not
    # `large-v3`. Set "0" to evict immediately, or "24h" to pin it.
    ollama_keep_alive: str = "30m"
    polish_prompt: str = DEFAULT_POLISH_PROMPT

    @classmethod
    def load(cls) -> "Config":
        """Read config.json, ignoring unknown or malformed entries."""
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
        os.replace(tmp, CONFIG_PATH)

    def resolved_vocabulary_path(self) -> str:
        return self.vocabulary_path or os.path.join(CONFIG_DIR, "vocabulary.json")

    def resolved_compute_type(self, device: str) -> str:
        if self.compute_type != "auto":
            return self.compute_type
        return "float16" if device == "cuda" else "int8"
