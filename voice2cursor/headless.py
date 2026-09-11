"""The dictation pipeline without a UI of its own: hotkey in, text out, logs to a file.

Run bare, this is `--headless`, which anyone on either host can use from a
terminal. The macOS menu-bar item (menubar.py) wraps the same object and
watches it through `on_change`, so every part that does real work -- the
recorder, transcriber, vocabulary, Ollama client and delivery path -- is
shared rather than duplicated.

The Windows tray app is a different program shape: pystray wants its own loop
and Tk wants the main thread. Rather than bend one into the other, app.py
drives the same components itself.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime

from . import archive, audio, backends
from .config import CONFIG_PATH, Config
from .hotkeys import HotkeyManager
from .logging_setup import get_logger
from .ollama_client import OllamaClient, OllamaError
from .output import deliver
from .transcriber import Transcriber

log = get_logger()

# Held at least this long with zero audio means a dead device, not a mis-tap.
_DEAD_MIC_SECONDS = 0.6
_MAX_HISTORY = 10


class Headless:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.transcriber = Transcriber(cfg)
        self.ollama = OllamaClient(cfg)
        self.recorder = audio.Recorder(cfg.input_device, keep_open=cfg.keep_mic_open)
        self.hotkeys = HotkeyManager()
        self._busy = threading.Lock()
        self._held_since: float | None = None
        self._recording = False
        self._stop = threading.Event()

        # State for a UI shell to show. `on_change` is called from whichever
        # thread changed it; getting back onto a UI thread is the shell's job.
        self.loading = True
        self.paused = False
        self.last_error: tuple[datetime, str] | None = None  # until the next success
        self.history: deque[tuple[datetime, str]] = deque(maxlen=_MAX_HISTORY)
        self.on_change = lambda: None

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def _fail(self, message: str, exc_info: bool = False) -> None:
        log.error("%s", message, exc_info=exc_info)
        backends.play_sound("error", self.cfg.sounds)
        self.last_error = (datetime.now(), message)
        self.on_change()

    # ---------- hotkey edges ----------

    def on_press(self) -> None:
        if self._recording or self.paused:
            return
        try:
            self.recorder.device = self.cfg.input_device
            self.recorder.start()
        except Exception as exc:
            self._fail(f"cannot open microphone: {exc}")
            return
        self._recording = True
        self._held_since = time.monotonic()
        backends.play_sound("start", self.cfg.sounds)
        log.info("recording")
        self.on_change()

    def on_release(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self.on_change()
        try:
            clip = self.recorder.stop()
        except Exception as exc:
            self._fail(f"recording failed: {exc}")
            return

        held = time.monotonic() - (self._held_since or time.monotonic())
        self._held_since = None
        seconds = clip.size / audio.TARGET_RATE

        if clip.size == 0:
            if held >= _DEAD_MIC_SECONDS:
                # Held long enough to mean it, but the device produced nothing:
                # usually a Bluetooth mic that dropped, or the default device
                # changing underneath. Reopen so the next press has a chance.
                self._reopen()
                self._fail("microphone produced no audio; reopened it, try again")
            return

        if seconds < self.cfg.min_seconds:
            return

        limit = self.cfg.max_seconds
        if limit and seconds > limit:
            clip = clip[: int(limit * audio.TARGET_RATE)]
            log.warning("clip truncated to %.0fs", limit)

        # Off the listener thread: a callback that blocks stops every later
        # key event from being seen.
        threading.Thread(target=self._work, args=(clip,), daemon=True).start()

    def _reopen(self) -> None:
        try:
            self.recorder.reset()
        except Exception as exc:
            log.error("stream reopen failed: %s", exc)

    # ---------- the pipeline ----------

    def _work(self, clip) -> None:
        if not self._busy.acquire(blocking=False):
            # Audible rather than only logged, or the clip vanishes without a trace.
            self._fail("still working on the previous clip; dropped this one")
            return
        self.on_change()

        wav_path = None
        if self.cfg.save_recordings:
            # Before transcription, so a crash still leaves the audio behind.
            wav_path = archive.save(clip, self.cfg.resolved_recordings_dir())
        try:
            result = self.transcriber.transcribe(clip)
            if not result.text:
                log.info("no speech detected")
                return

            text = result.text
            if self.cfg.auto_polish:
                try:
                    text = self.ollama.polish(result.text) or result.text
                except OllamaError as exc:
                    # Never lose a transcript to a failed polish.
                    log.warning("polish skipped: %s", exc)

            if wav_path:
                archive.write_transcript(wav_path, text)

            # Pasting while the hotkey's own modifiers are still held turns
            # Cmd+V into Cmd+Alt+V, which most apps discard.
            if self.cfg.output_mode != "clipboard":
                self.hotkeys.wait_for_modifiers_clear(timeout=2.0)
            deliver(text, self.cfg.output_mode, self.cfg.restore_clipboard)

            backends.play_sound("done", self.cfg.sounds)
            self.last_error = None
            self.history.append((datetime.now(), text))
            log.info("%.1fs -> %d chars in %.1fs (%.1fx)",
                     result.duration, len(text), result.elapsed, result.speedup)
        except Exception as exc:
            self._fail(f"transcription failed: {type(exc).__name__}: {exc}", exc_info=True)
        finally:
            self._busy.release()
            self.on_change()

    # ---------- lifecycle ----------

    def listen(self) -> None:
        """(Re)start the hotkey listener on the current record_hotkey.

        A HotkeyManager cannot unregister, so a changed hotkey gets a fresh one.
        """
        self.hotkeys.stop()
        self.hotkeys = HotkeyManager()
        self.hotkeys.register("record", self.cfg.record_hotkey,
                              self.on_press, self.on_release)
        self.hotkeys.start()

    def start(self) -> bool:
        """Open the mic if it is kept open, load the model, start listening.

        False when the model cannot load, which leaves nothing to listen for.
        """
        if self.recorder.keep_open:
            try:
                self.recorder.open()
            except Exception as exc:
                # Not fatal: the device may appear later, and start() retries.
                log.warning("microphone unavailable at startup: %s", exc)

        log.info("loading %s ...", self.cfg.model_size)
        try:
            self.transcriber.ensure_loaded()
            self.transcriber.warmup()
        except Exception as exc:
            self.loading = False
            self._fail(f"model load failed: {type(exc).__name__}: {exc}")
            return False
        log.info("ready - %s", self.transcriber.describe())

        self.listen()
        self.loading = False
        self.on_change()

        mode = "hold" if self.cfg.hotkey_mode == "hold" else "press"
        log.info("%s %s to dictate; config at %s",
                 mode, self.cfg.record_hotkey, CONFIG_PATH)
        return True

    def shutdown(self) -> None:
        self.hotkeys.stop()
        self.recorder.close()
        log.info("stopped")

    def run(self) -> int:
        if not self.start():
            return 1
        try:
            while not self._stop.is_set():
                self._stop.wait(3600)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def stop(self) -> None:
        self._stop.set()
