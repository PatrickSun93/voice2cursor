"""Persistent settings, in the state directory this platform's backend names.

One dataclass serves both hosts. Where a default has to differ -- the record
hotkey, the engine -- it comes from the backend rather than from a branch here.

Settings written by either of the two pre-merge builds are migrated on first
run: the Windows package stored its config under %APPDATA%\\SonicType, and the
macOS script kept a flat config.json beside its own source with a different set
of key names. Both are read once and rewritten in this shape.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field, fields

from . import backends

APP_NAME = "voice2cursor"
CONFIG_DIR = backends.STATE_DIR
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_DIR = os.path.join(CONFIG_DIR, "logs")

# Kept for callers that want a menu without an engine to hand; the authoritative
# list is whatever the active engine reports (Transcriber.models).
MODEL_SIZES = ("tiny", "base", "small", "medium", "large-v3", "distil-large-v3", "turbo")

DEFAULT_POLISH_PROMPT = (
    "You are a transcript editor. Rewrite the text below so it reads as clean prose:\n"
    "fix punctuation and capitalisation, drop filler words (um, uh, you know, like),\n"
    "remove false starts and stutters, and break it into paragraphs.\n"
    "Do not summarise, reword, translate, or add anything that is not already said.\n"
    "Reply with the corrected text only - no preamble, no explanation, no quote marks.\n\n"
    "TEXT:\n{text}"
)


def _default_hotkey() -> str:
    return backends.DEFAULT_RECORD_HOTKEY


def _default_keep_mic_open() -> bool:
    return backends.DEFAULT_KEEP_MIC_OPEN


@dataclass
class Config:
    # --- engine ---
    # auto -> this host's preferred engine, falling back to the other if the
    # preferred one is not installed. See engines/__init__.py.
    engine: str = "auto"

    # --- transcription ---
    # A size token ("small", "large-v3") or, for mlx, a Hugging Face repo id.
    # Size tokens work on both engines; each maps them to its own naming.
    model_size: str = "small"
    device: str = "auto"          # auto | cuda | cpu   (ignored by mlx)
    compute_type: str = "auto"    # auto -> float16 on cuda, int8 on cpu
    language: str | None = None   # None -> autodetect
    beam_size: int = 5
    vad_filter: bool = True       # drop silence; big win on push-to-talk clips
    initial_prompt: str = ""      # seed vocabulary, e.g. names or jargon

    # Whisper invents words when fed near-silence - a stray "Mahala" or "Thank
    # you." pasting into your editor is worse than nothing. Segments are dropped
    # when the model is this unsure. Raise max_no_speech toward 1.0, or lower
    # min_avg_logprob toward -2.0, if real speech is being discarded; None turns
    # that check off.
    max_no_speech: float | None = 0.6     # drop segment if no_speech_prob exceeds this
    min_avg_logprob: float | None = -1.0  # drop segment if avg_logprob falls below this

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
    # Hold the stream open between clips: pre-roll, and no per-clip open delay.
    # Off by default on macOS, where an open Bluetooth mic drags the headset's
    # playback down to telephone quality. See audio.py.
    keep_mic_open: bool = field(default_factory=_default_keep_mic_open)
    min_seconds: float = 0.35     # shorter clips are a mis-press, not speech
    max_seconds: float = 300.0    # a stuck key should not fill the disk

    # --- hotkeys ---
    record_hotkey: str = field(default_factory=_default_hotkey)
    polish_hotkey: str = "ctrl+alt+p"
    review_hotkey: str = "ctrl+alt+r"
    hotkey_mode: str = "hold"     # hold (push-to-talk) | toggle

    # --- output ---
    output_mode: str = "paste"    # paste (clipboard + modifier+V) | type | clipboard
    restore_clipboard: bool = True
    auto_polish: bool = False     # run Ollama before pasting
    open_review_window: bool = False
    sounds: bool = True           # audible start / done / error cues

    # --- recording archive ---
    # Every clip kept as a WAV with its transcript beside it under the same
    # name, so a wrong transcription can be listened back to instead of guessed
    # at. Off by default: it grows without bound.
    save_recordings: bool = False
    recordings_dir: str = ""      # empty -> recordings/ in the state directory

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
        raw = _read(CONFIG_PATH)
        if raw is None:
            raw = _migrate()
        if raw is None:
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2, ensure_ascii=False)
        os.replace(tmp, CONFIG_PATH)

    def resolved_vocabulary_path(self) -> str:
        return self.vocabulary_path or os.path.join(CONFIG_DIR, "vocabulary.json")

    def resolved_recordings_dir(self) -> str:
        return (os.path.expanduser(self.recordings_dir)
                or os.path.join(CONFIG_DIR, "recordings"))

    def resolved_compute_type(self, device: str) -> str:
        if self.compute_type != "auto":
            return self.compute_type
        return "float16" if device == "cuda" else "int8"


def _read(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# The macOS script's key names, and what they became. Only the ones that
# actually changed are listed; everything else already matches.
_LEGACY_KEYS = {
    "hotkey": "record_hotkey",
    "model": "model_size",
}


def _from_legacy_mac(raw: dict) -> dict:
    """Translate the macOS script's flat config.json into this shape."""
    out = {_LEGACY_KEYS.get(k, k): v for k, v in raw.items() if k != "ollama"}

    # auto_paste was a boolean; output_mode says the same thing with room for
    # the "type" case the boolean could not express.
    if "auto_paste" in out:
        out["output_mode"] = "paste" if out.pop("auto_paste") else "clipboard"

    ollama = raw.get("ollama") or {}
    if ollama:
        out["auto_polish"] = bool(ollama.get("enabled", False))
        if ollama.get("model"):
            out["ollama_model"] = ollama["model"]
        url = ollama.get("url") or ""
        if url:
            # The script pointed at the /api/generate endpoint; the client here
            # builds its own paths from the server root.
            out["ollama_url"] = url.split("/api/")[0].rstrip("/")

    # The engines pin this to False themselves -- it causes runaway repetition
    # on push-to-talk-length clips.
    out.pop("condition_on_previous_text", None)
    out.pop("preload_model", None)

    # The script never dropped segments, and switching the confidence filter on
    # underneath it is not safe: replaying one user's archive through
    # mlx-whisper, Mandarin that was spoken and transcribed correctly scored an
    # avg_logprob of -2.7 to -4.2, far below the -1.0 default. Opt in by hand.
    out.setdefault("max_no_speech", None)
    out.setdefault("min_avg_logprob", None)
    return out


def _migrate() -> dict | None:
    """Adopt settings from a pre-merge install, once.

    Returns the migrated mapping, having also written it to the new location,
    or None when there is nothing to adopt.
    """
    for legacy_dir in backends.LEGACY_STATE_DIRS:
        raw = _read(os.path.join(legacy_dir, "config.json"))
        if raw is None:
            continue
        _adopt_sidecars(legacy_dir)
        _write_raw(raw)
        return raw

    # The macOS script kept its config beside its own source rather than in a
    # state directory, so look where that source used to live.
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    raw = _read(os.path.join(here, "config.json"))
    if raw is not None:
        migrated = _from_legacy_mac(raw)
        _write_raw(migrated)
        return migrated
    return None


def _adopt_sidecars(legacy_dir: str) -> None:
    """Bring the vocabulary across too, or the migration loses it silently."""
    for name in ("vocabulary.json",):
        src, dst = os.path.join(legacy_dir, name), os.path.join(CONFIG_DIR, name)
        if os.path.exists(src) and not os.path.exists(dst):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass


def _write_raw(raw: dict) -> None:
    known = {f.name for f in fields(Config)}
    try:
        Config(**{k: v for k, v in raw.items() if k in known}).save()
    except (OSError, TypeError, ValueError):
        pass  # a failed migration must not stop the app starting
