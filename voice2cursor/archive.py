"""Keep every clip as a WAV with its transcript beside it.

A transcription that comes out wrong is otherwise unreproducible: the audio is
gone the moment it is transcribed, so there is nothing to play back and nothing
to test a model change against. Same base name for both files, so the pair is
obvious in a directory listing sorted by time.

Standard-library `wave` on purpose -- this costs no dependency.
"""

from __future__ import annotations

import os
import wave
from datetime import datetime

import numpy as np

SAMPLE_RATE = 16_000


def save(audio: np.ndarray, directory: str, text: str | None = None) -> str | None:
    """Write clip and transcript, returning the WAV path.

    Returns None rather than raising: losing the archive copy must never cost
    the user the transcription that is already in hand.
    """
    if audio is None or audio.size == 0:
        return None
    try:
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        path = os.path.join(directory, f"{stamp}.wav")
        _write_wav(path, audio)
        if text:
            write_transcript(path, text)
        return path
    except (OSError, ValueError):
        return None


def write_transcript(wav_path: str, text: str) -> str | None:
    """Write `text` next to an already-saved WAV, under the same base name."""
    try:
        txt_path = os.path.splitext(wav_path)[0] + ".txt"
        with open(txt_path, "w", encoding="utf-8") as fh:
            fh.write(text.rstrip() + "\n")
        return txt_path
    except OSError:
        return None


def _write_wav(path: str, audio: np.ndarray) -> None:
    """float32 [-1, 1] mono -> 16 kHz 16-bit PCM."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm.tobytes())
