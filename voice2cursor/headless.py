"""No tray, no windows: hotkey in, text out, logs to a file.

This is how the macOS launchd agent runs, and how anyone on either host can
run it from a terminal. It shares every part that does real work -- the same
recorder, transcriber, vocabulary, Ollama client and delivery path the tray
app uses -- and simply leaves out the UI.

The tray app is not merely this plus an icon: pystray wants its own loop and
Tk wants the main thread, which is a different program shape. Rather than bend
one into the other, both drive the same components. That is also why macOS
does not get a tray by default -- a menu-bar item wants an .app bundle, and a
launchd agent has no session to draw one in.
"""

from __future__ import annotations

import threading
import time

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


class Headless:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.transcriber = Transcriber(cfg)
        self.ollama = OllamaClient(cfg)
        self.recorder = audio.Recorder(cfg.input_device)
        self.hotkeys = HotkeyManager()
        self._busy = threading.Lock()
        self._held_since: float | None = None
        self._recording = False
        self._stop = threading.Event()

    # ---------- hotkey edges ----------

    def on_press(self) -> None:
        if self._recording:
            return
        try:
            self.recorder.device = self.cfg.input_device
            self.recorder.start()
        except Exception as exc:
            log.error("cannot open microphone: %s", exc)
            backends.play_sound("error", self.cfg.sounds)
            return
        self._recording = True
        self._held_since = time.monotonic()
        backends.play_sound("start", self.cfg.sounds)
        log.info("recording")

    def on_release(self) -> None:
        if not self._recording:
            return
        self._recording = False
        try:
            clip = self.recorder.stop()
        except Exception as exc:
            log.error("recording failed: %s", exc)
            backends.play_sound("error", self.cfg.sounds)
            return

        held = time.monotonic() - (self._held_since or time.monotonic())
        self._held_since = None
        seconds = clip.size / audio.TARGET_RATE

        if clip.size == 0:
            if held >= _DEAD_MIC_SECONDS:
                # Held long enough to mean it, but the device produced nothing:
                # usually a Bluetooth mic that dropped, or the default device
                # changing underneath. Reopen so the next press has a chance.
                log.warning("microphone produced no audio; reopening")
                self._reopen()
                backends.play_sound("error", self.cfg.sounds)
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
            self.recorder.close()
            self.recorder.open()
        except Exception as exc:
            log.error("stream reopen failed: %s", exc)

    # ---------- the pipeline ----------

    def _work(self, clip) -> None:
        if not self._busy.acquire(blocking=False):
            log.warning("still working on the previous clip; dropped this one")
            return

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
            log.info("%.1fs -> %d chars in %.1fs (%.1fx)",
                     result.duration, len(text), result.elapsed, result.speedup)
        except Exception:
            log.exception("transcription failed")
            backends.play_sound("error", self.cfg.sounds)
        finally:
            self._busy.release()

    # ---------- lifecycle ----------

    def run(self) -> int:
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
            log.error("model load failed: %s: %s", type(exc).__name__, exc)
            return 1
        log.info("ready - %s", self.transcriber.describe())

        self.hotkeys.register("record", self.cfg.record_hotkey,
                              self.on_press, self.on_release)
        self.hotkeys.start()

        mode = "hold" if self.cfg.hotkey_mode == "hold" else "press"
        log.info("%s %s to dictate; config at %s",
                 mode, self.cfg.record_hotkey, CONFIG_PATH)

        try:
            while not self._stop.is_set():
                self._stop.wait(3600)
        except KeyboardInterrupt:
            pass
        finally:
            self.hotkeys.stop()
            self.recorder.close()
            log.info("stopped")
        return 0

    def stop(self) -> None:
        self._stop.set()
