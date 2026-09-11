"""Microphone capture for push-to-talk recording.

The stream is held open continuously and audio lands in a ring buffer, rather
than opening a stream when the hotkey is pressed. Measured on this hardware,
`InputStream` takes 0.02 s to open on a USB array but up to 0.93 s on a
Bluetooth headset - long enough to swallow the first word of every clip. A ring
buffer also gives us a short pre-roll, so audio from just *before* the keypress
survives for users who start talking as they press.

That is `keep_open`, the Windows default. macOS defaults the other way: an open
Bluetooth microphone pins the headset to its hands-free profile, so everything
the user listens to plays at telephone quality for as long as the stream is
up. There the stream opens on the keypress and closes on release, and there is
no pre-roll.

Recording at the device's native rate and resampling to the 16 kHz mono float32
Whisper wants means faster-whisper can be handed a numpy array directly, so
there is no temp file and no ffmpeg dependency.
"""
from __future__ import annotations

import threading
from collections import deque
from math import gcd

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

TARGET_RATE = 16_000

# Retained ahead of the keypress, so a slightly early speaker is not clipped.
PREROLL_SECONDS = 0.25
# Safety stop, in case a hotkey release event is ever missed.
MAX_SECONDS = 600.0


class Recorder:
    """Capture with mark/collect semantics.

    `open()` starts the stream. `start()` marks the point to collect from;
    `stop()` returns everything since that mark, resampled to 16 kHz mono.
    With `keep_open` the stream then keeps running, and while idle only the
    pre-roll is retained, so it costs a few kilobytes rather than growing
    without bound. Without it, `start()` opens the stream and `stop()` closes it.
    """

    def __init__(self, device: str | None = None, keep_open: bool = True):
        self._device = device          # device NAME, or None for system default
        self._resolved: int | None = None  # index, recomputed on every open()
        self._stream: sd.InputStream | None = None
        self._rate = TARGET_RATE
        self.keep_open = keep_open

        self._lock = threading.Lock()
        self._blocks: deque[np.ndarray] = deque()
        self._buffered = 0   # samples currently in _blocks
        self._dropped = 0    # samples evicted from the front, ever
        self._mark: int | None = None  # absolute index to collect from
        self.overflows = 0

    # ---------------------------------------------------------------- lifecycle

    @property
    def device(self) -> str | None:
        return self._device

    @property
    def resolved_index(self) -> int | None:
        """The PortAudio index actually in use, or None for the system default."""
        return self._resolved

    @device.setter
    def device(self, value: str | None) -> None:
        """Switching devices needs a new stream, so reopen if one is running."""
        if value == self._device:
            return
        self._device = value
        if self._stream is not None:
            self.close()
            self.open()

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    @property
    def capturing(self) -> bool:
        return self._mark is not None

    def open(self) -> None:
        """Start the stream. Safe to call when already open."""
        if self._stream is not None:
            return
        try:
            self._open_stream()
        except sd.PortAudioError:
            # PortAudio enumerates devices once, when it initialises. After a
            # Bluetooth headset connects or drops, or the Mac wakes from sleep,
            # that list is stale and every open fails (-9986) until PortAudio is
            # re-initialised, which also refreshes what resolve_input_device sees.
            sd._terminate()
            sd._initialize()
            self._open_stream()

    def _open_stream(self) -> None:
        self._resolved = resolve_input_device(self._device)
        self._rate = self._pick_rate()
        with self._lock:
            self._blocks.clear()
            self._buffered = self._dropped = 0
            self._mark = None
        stream = sd.InputStream(
            samplerate=self._rate,
            channels=1,
            dtype="float32",
            device=self._resolved,
            callback=self._callback,
        )
        stream.start()
        self._stream = stream

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass  # device may already be gone
        with self._lock:
            self._blocks.clear()
            self._buffered = self._dropped = 0
            self._mark = None

    def reset(self) -> None:
        """Drop the stream after the device misbehaved. Reopened here only when
        it is meant to stay open; otherwise the next start() opens a fresh one."""
        self.close()
        if self.keep_open:
            self.open()

    def _pick_rate(self) -> int:
        """Prefer 16 kHz so no resampling is needed; fall back to the device default."""
        try:
            sd.check_input_settings(
                device=self._resolved, samplerate=TARGET_RATE, channels=1, dtype="float32"
            )
            return TARGET_RATE
        except Exception:
            info = sd.query_devices(self._resolved, "input")
            return int(info["default_samplerate"])

    # ------------------------------------------------------------------ capture

    def _capacity(self) -> int:
        """Samples to retain: the full clip while capturing, else just pre-roll."""
        seconds = MAX_SECONDS if self._mark is not None else PREROLL_SECONDS
        return max(1, int(seconds * self._rate))

    def _callback(self, indata, _frames, _time, status) -> None:
        if status.input_overflow:
            self.overflows += 1
        block = indata.reshape(-1).copy()
        with self._lock:
            self._blocks.append(block)
            self._buffered += block.size
            capacity = self._capacity()
            while self._buffered > capacity and len(self._blocks) > 1:
                gone = self._blocks.popleft()
                self._buffered -= gone.size
                self._dropped += gone.size

    def start(self) -> None:
        """Mark the start of a clip, including PREROLL_SECONDS of prior audio."""
        if self._stream is None:
            self.open()
        with self._lock:
            total = self._dropped + self._buffered
            preroll = int(PREROLL_SECONDS * self._rate)
            # Never mark earlier than what is still buffered.
            self._mark = max(self._dropped, total - preroll)

    def stop(self) -> np.ndarray:
        """Return audio since `start()` as 16 kHz mono float32 (empty if none).

        Without `keep_open` this also closes the stream -- after collecting,
        because close() discards the buffer.
        """
        try:
            return self._collect()
        finally:
            if not self.keep_open:
                self.close()

    def _collect(self) -> np.ndarray:
        with self._lock:
            mark, self._mark = self._mark, None
            if mark is None or not self._blocks:
                self._trim_locked()
                return np.zeros(0, dtype=np.float32)
            joined = np.concatenate(self._blocks)
            offset = max(0, mark - self._dropped)
            clip = joined[offset:].copy()
            self._trim_locked()

        if clip.size == 0:
            return np.zeros(0, dtype=np.float32)
        clip = clip[: int(MAX_SECONDS * self._rate)]
        if self._rate != TARGET_RATE:
            clip = _resample(clip, self._rate, TARGET_RATE)
        return clip.astype(np.float32, copy=False)

    def _trim_locked(self) -> None:
        """Drop back to pre-roll size once a clip is collected. Caller holds _lock."""
        capacity = self._capacity()
        while self._buffered > capacity and len(self._blocks) > 1:
            gone = self._blocks.popleft()
            self._buffered -= gone.size
            self._dropped += gone.size

    def cancel(self) -> None:
        """Discard the in-progress clip without returning it."""
        if not self.keep_open:
            self.close()
            return
        with self._lock:
            self._mark = None
            self._trim_locked()


def _resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Band-limited resample; resample_poly wants the ratio in lowest terms."""
    g = gcd(src, dst)
    return resample_poly(audio, dst // g, src // g).astype(np.float32)


def list_input_devices() -> list[tuple[int, str]]:
    """(index, name) for every device that can capture, deduplicated by name."""
    seen: dict[str, int] = {}
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and dev["name"] not in seen:
            seen[dev["name"]] = idx
    return [(idx, name) for name, idx in seen.items()]


def resolve_input_device(name: str | None) -> int | None:
    """Map a saved device name to a current PortAudio index.

    Indices are positional and shift as devices come and go, so the name is
    what gets persisted. Returns None (system default) when the named device is
    not present, which is the right behaviour for an unplugged microphone.

    The prefix match matters because the same device is reported under
    different names per host API - MME truncates to 31 characters, so
    "Microphone Array on SoundWire Device (2- Cirrus Logic XU)" appears as
    "Microphone Array on SoundWire D".
    """
    if not name:
        return None
    devices = list_input_devices()
    for idx, dev_name in devices:
        if dev_name == name:
            return idx
    for idx, dev_name in devices:
        if dev_name.startswith(name) or name.startswith(dev_name):
            return idx
    return None


def default_input_name() -> str:
    try:
        return str(sd.query_devices(kind="input")["name"])
    except Exception:
        return "unknown"
