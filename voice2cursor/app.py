"""voice2cursor orchestrator: tray icon, global hotkeys, and the dictation pipeline.

Threading model, which the rest of this file depends on:

  main thread      owns Tk. Runs `root.mainloop()`. Every widget touch must
                   arrive here via `_post()`, because Tk is not thread-safe.
  tray thread      pystray, started with `run_detached()` so it does not fight
                   Tk for the main thread. Menu callbacks land here.
  listener thread  pynput keyboard hook. Callbacks must return immediately or
                   keystrokes back up system-wide, so they only flip state and
                   hand work to a worker.
  worker threads   transcription and Ollama calls. One at a time, guarded by
                   `_busy`, so concurrent hotkey presses cannot stack two
                   models onto the GPU at once.
"""
from __future__ import annotations

import queue
import subprocess
import threading
import time
import tkinter as tk
import traceback
from typing import Callable

import pystray

from . import archive, audio, backends, icons
from .config import CONFIG_DIR, CONFIG_PATH, Config
from .logging_setup import get_logger
from .hotkeys import HotkeyManager
from .ollama_client import OllamaClient, OllamaError
from .output import deliver
from .review_window import ReviewWindow
from .transcriber import Transcriber

log = get_logger()

_MAX_HISTORY = 20
# Held at least this long with zero audio means a dead device,
# not a mis-tap.
_DEAD_MIC_SECONDS = 0.6


def _bind(fn: Callable[..., object], *args: object) -> Callable[[], None]:
    """Build a zero-argument tray callback with `args` baked in.

    pystray validates actions by `co_argcount`, which counts parameters that
    have defaults - so the usual `lambda _i, _it, x=x: ...` late-binding idiom
    is rejected with ValueError. A factory closure keeps the count at zero
    while still capturing a distinct value per loop iteration.
    """
    def action() -> None:
        fn(*args)

    return action


def _check(fn: Callable[..., bool], *args: object) -> Callable[[object], bool]:
    """Same idea for `checked`, which pystray calls with the menu item."""
    def state(_item: object) -> bool:
        return bool(fn(*args))

    return state


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.transcriber = Transcriber(cfg)
        self.ollama = OllamaClient(cfg)
        self.recorder = audio.Recorder(cfg.input_device)
        self.hotkeys = HotkeyManager()

        self.state = "idle"  # idle | recording | transcribing | polishing | error
        self.last_raw = ""
        self.last_polished = ""
        self.history: list[str] = []
        self.status_detail = "starting..."

        self._busy = threading.Lock()
        self._ollama_models: list[str] = []
        self._icon: pystray.Icon | None = None
        self._root: tk.Tk | None = None
        self._review: ReviewWindow | None = None
        self._ui_calls: queue.Queue[Callable[[], None]] = queue.Queue()
        self._held_since: float | None = None

    # ------------------------------------------------------------- ui plumbing

    def _post(self, fn: Callable[[], None]) -> None:
        """Run `fn` on the Tk main thread."""
        if self._root is None:
            return
        self._ui_calls.put(fn)
        try:
            self._root.after(0, self._drain_ui_calls)
        except RuntimeError:
            pass  # interpreter shutting down

    def _drain_ui_calls(self) -> None:
        while True:
            try:
                fn = self._ui_calls.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                traceback.print_exc()

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        if detail:
            self.status_detail = detail
        if self._icon is not None:
            icon_state = state if state in icons.STATE_COLOURS else "idle"
            self._icon.icon = icons.make_icon(icon_state)
            self._icon.title = f"voice2cursor - {self.status_detail}"
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def _notify(self, message: str, title: str = "voice2cursor") -> None:
        if self._icon is None:
            return
        try:
            self._icon.notify(message[:220], title)
        except Exception:
            pass  # balloon notifications are best-effort

    def _cue(self, kind: str) -> None:
        backends.play_sound(kind, self.cfg.sounds)

    def _fail(self, message: str) -> None:
        log.error("%s", message)
        self._cue("error")
        self._set_state("error", message)
        self._notify(message, "voice2cursor - error")
        threading.Timer(4.0, lambda: self._set_state("idle", self._idle_detail())).start()

    def _idle_detail(self) -> str:
        return f"ready - {self.transcriber.describe()}"

    def _deliver(self, text: str) -> None:
        """Send text to the focused window once the hotkey modifiers are released.

        Ctrl+V while the user still holds the record hotkey's Ctrl+Alt lands as
        Ctrl+Alt+V, which most apps discard - the transcript would vanish.
        """
        if self.cfg.output_mode != "clipboard":
            self.hotkeys.wait_for_modifiers_clear(timeout=2.0)
        deliver(text, self.cfg.output_mode, self.cfg.restore_clipboard)

    # --------------------------------------------------------------- recording

    def on_record_pressed(self) -> None:
        """Hotkey engaged. In hold mode this starts capture; in toggle mode it flips."""
        if self.cfg.hotkey_mode == "toggle":
            if self.state == "recording":
                self._stop_and_transcribe()
            else:
                self._start_recording()
        else:
            self._start_recording()

    def on_record_released(self) -> None:
        if self.cfg.hotkey_mode == "hold" and self.state == "recording":
            self._stop_and_transcribe()

    def _start_recording(self) -> None:
        if self.state in ("recording", "transcribing", "polishing"):
            return
        try:
            self.recorder.device = self.cfg.input_device
            self.recorder.start()
        except Exception as exc:
            self._fail(f"Cannot open microphone: {exc}")
            return
        self._held_since = time.monotonic()
        self._cue("start")
        self._set_state("recording", "recording...")

    def _recover_stream(self) -> None:
        """Close and reopen the capture stream after the device misbehaved."""
        try:
            self.recorder.close()
            self.recorder.device = self.cfg.input_device
            self.recorder.open()
        except Exception as exc:
            print(f"[voice2cursor] stream reopen failed: {exc}")

    def _stop_and_transcribe(self) -> None:
        try:
            clip = self.recorder.stop()
        except Exception as exc:
            self._fail(f"Recording failed: {exc}")
            return

        seconds = clip.size / audio.TARGET_RATE
        held = time.monotonic() - (self._held_since or time.monotonic())
        self._held_since = None

        if clip.size == 0 and held >= _DEAD_MIC_SECONDS:
            # Held long enough to mean it, but the device produced nothing. Most
            # often a Bluetooth mic that dropped, or the default device changing
            # under us. Reopen so the next attempt has a chance, and say so -
            # silently returning to idle looks like the hotkey is broken.
            self._recover_stream()
            self._fail("Microphone produced no audio - reconnecting. Try again.")
            return

        # Shorter than this is almost always a mis-tap on the hotkey.
        if seconds < self.cfg.min_seconds:
            self._set_state("idle", self._idle_detail())
            return

        # A key that sticks should not hand Whisper an hour of room tone. Keep
        # the start, which is what was actually said, and say that it was cut.
        limit = self.cfg.max_seconds
        if limit and seconds > limit:
            clip = clip[: int(limit * audio.TARGET_RATE)]
            seconds = limit
            self._notify(f"Clip truncated to {limit:.0f}s")

        self._set_state("transcribing", f"transcribing {seconds:.1f}s...")
        threading.Thread(target=self._transcribe_worker, args=(clip,), daemon=True).start()

    def _transcribe_worker(self, clip) -> None:
        if not self._busy.acquire(blocking=False):
            self._fail("Still working on the previous clip")
            return
        # Written before transcription, so a crash still leaves the audio.
        wav_path = None
        if self.cfg.save_recordings:
            wav_path = archive.save(clip, self.cfg.resolved_recordings_dir())
        try:
            result = self.transcriber.transcribe(clip)
            if not result.text:
                self._set_state("idle", "no speech detected")
                self._notify("No speech detected")
                return

            self.last_raw = result.text
            self.last_polished = ""
            self._remember(result.text)

            text = result.text
            if self.cfg.auto_polish:
                self._set_state("polishing", "polishing...")
                try:
                    self.last_polished = self.ollama.polish(result.text)
                    text = self.last_polished or result.text
                except OllamaError as exc:
                    # Never lose a transcript to a failed polish; paste the raw text.
                    self._notify(f"Polish skipped: {exc}")

            if wav_path:
                archive.write_transcript(wav_path, text)

            self._deliver(text)
            self._cue("done")
            log.info("%.1fs -> %d chars in %.1fs (%.1fx)",
                     result.duration, len(text), result.elapsed, result.speedup)
            self._set_state(
                "idle",
                f"{result.duration:.1f}s in {result.elapsed:.1f}s ({result.speedup:.1f}x)",
            )
            if self.cfg.open_review_window:
                self._post(self._show_review)
        except Exception as exc:
            log.exception("transcription failed")
            traceback.print_exc()
            self._fail(f"Transcription failed: {type(exc).__name__}: {exc}")
        finally:
            self._busy.release()

    def _remember(self, text: str) -> None:
        self.history.append(text)
        del self.history[:-_MAX_HISTORY]

    # ------------------------------------------------------------------ polish

    def on_polish_hotkey(self) -> None:
        if not self.last_raw:
            self._notify("Nothing to polish yet")
            return
        threading.Thread(
            target=self._polish_worker, args=(self.last_raw, True), daemon=True
        ).start()

    def _polish_worker(self, text: str, paste: bool) -> None:
        if not self._busy.acquire(blocking=False):
            self._notify("Busy - try again in a moment")
            return
        try:
            self._set_state("polishing", "polishing...")
            polished = self.ollama.polish(text)
            self.last_polished = polished
            self._post(lambda: self._update_review(polished))
            if paste:
                self._deliver(polished)
            self._set_state("idle", self._idle_detail())
        except OllamaError as exc:
            message = str(exc)
            self._post(lambda: self._update_review_status(message))
            self._fail(message)
        except Exception as exc:
            traceback.print_exc()
            self._fail(f"Polish failed: {type(exc).__name__}: {exc}")
        finally:
            self._busy.release()

    # ----------------------------------------------------------- review window

    def _show_review(self) -> None:
        if self._review is None:
            return
        self._review.show(
            raw=self.last_raw,
            polished=self.last_polished,
            status=self.transcriber.describe(),
        )

    def _update_review(self, polished: str) -> None:
        if self._review is not None:
            self._review.set_polished(polished, "polished")

    def _update_review_status(self, text: str) -> None:
        if self._review is not None:
            self._review.set_status(text)

    def _review_polish_request(self, text: str) -> None:
        """Called from the review window's Polish button, on the main thread."""
        threading.Thread(target=self._polish_worker, args=(text, False), daemon=True).start()

    def _review_paste_request(self, text: str) -> None:
        # Delay so the window has finished hiding and focus is back on the target app.
        threading.Timer(0.3, self._deliver, args=(text,)).start()

    # ------------------------------------------------------------- tray actions

    def _select_model(self, size: str) -> None:
        if size == self.cfg.model_size and self.transcriber.loaded_size == size:
            return
        self.cfg.model_size = size
        self.cfg.save()
        threading.Thread(target=self._load_model_worker, args=(size,), daemon=True).start()

    def _load_model_worker(self, size: str) -> None:
        # Takes _busy so a swap cannot free the model out from under an
        # in-flight transcription, which crashes CTranslate2 rather than erroring.
        if not self._busy.acquire(timeout=30):
            self._notify("Busy - model not switched, try again")
            return
        try:
            self._set_state("busy", f"loading {size}...")
            self.transcriber.ensure_loaded(size)
            self._set_state("idle", self._idle_detail())
            self._notify(f"Loaded {self.transcriber.describe()}")
        except Exception as exc:
            self._fail(f"Could not load {size}: {type(exc).__name__}: {exc}")
        finally:
            self._busy.release()

    def _select_input(self, name: str | None) -> None:
        self.cfg.input_device = name
        self.cfg.save()
        self.recorder.device = name  # setter reopens the stream
        self._notify(f"Microphone: {name or 'system default'}")

    def _select_ollama_model(self, name: str) -> None:
        self.cfg.ollama_model = name
        self.cfg.save()
        self._notify(f"Ollama model: {name}")

    def _set_hotkey_mode(self, mode: str) -> None:
        self.cfg.hotkey_mode = mode
        self.cfg.save()

    def _set_output_mode(self, mode: str) -> None:
        self.cfg.output_mode = mode
        self.cfg.save()

    def _toggle_auto_polish(self) -> None:
        self.cfg.auto_polish = not self.cfg.auto_polish
        self.cfg.save()

    def _toggle_review_window(self) -> None:
        self.cfg.open_review_window = not self.cfg.open_review_window
        self.cfg.save()

    def _refresh_ollama(self) -> None:
        """Re-poll Ollama, so models pulled after startup show up in the menu."""
        def work() -> None:
            self._ollama_models = self.ollama.list_models()
            self._rebuild_menu()
            if self._ollama_models:
                self._notify(f"Ollama: {len(self._ollama_models)} model(s) available")
            else:
                self._notify("Ollama not reachable - transcription still works")

        threading.Thread(target=work, daemon=True).start()

    def _open_config_folder(self) -> None:
        self.cfg.save()  # make sure the file exists before opening the folder
        backends.open_folder(CONFIG_DIR)

    def _reload_config(self) -> None:
        fresh = Config.load()
        self.cfg = fresh
        self.transcriber.cfg = fresh
        self.ollama.cfg = fresh
        self.recorder.device = fresh.input_device
        self._rebind_hotkeys()
        self._rebuild_menu()
        self._notify("Config reloaded")

    def _run_doctor(self) -> None:
        def work() -> None:
            import io
            import os
            from contextlib import redirect_stdout

            buf = io.StringIO()
            try:
                from .doctor import run_doctor

                with redirect_stdout(buf):
                    run_doctor(self.cfg)
            except Exception:
                buf.write(traceback.format_exc())
            path = os.path.join(CONFIG_DIR, "doctor.txt")
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(buf.getvalue())
            backends.open_text_file(path)

        threading.Thread(target=work, daemon=True).start()

    def _quit(self) -> None:
        self.hotkeys.stop()
        self.recorder.close()
        if self._icon is not None:
            self._icon.visible = False
            self._icon.stop()
        self._post(self._teardown_tk)

    def _teardown_tk(self) -> None:
        if self._root is not None:
            self._root.quit()
            self._root.destroy()

    # ---------------------------------------------------------------- tray menu

    def _rebuild_menu(self) -> None:
        if self._icon is None:
            return
        self._icon.menu = self._build_menu()
        try:
            self._icon.update_menu()
        except Exception:
            pass

    def _build_menu(self) -> pystray.Menu:
        Item, Menu = pystray.MenuItem, pystray.Menu

        model_items = [
            Item(
                size,
                _bind(self._select_model, size),
                checked=_check(lambda s: self.cfg.model_size == s, size),
                radio=True,
            )
            for size in self.transcriber.models
        ]

        device_items = [
            Item(
                "System default",
                _bind(self._select_input, None),
                checked=_check(lambda: self.cfg.input_device is None),
                radio=True,
            )
        ]
        for _idx, name in audio.list_input_devices():
            device_items.append(
                Item(
                    name[:44],
                    _bind(self._select_input, name),
                    checked=_check(lambda n: self.cfg.input_device == n, name),
                    radio=True,
                )
            )

        if self._ollama_models:
            ollama_items = [
                Item(
                    name,
                    _bind(self._select_ollama_model, name),
                    checked=_check(lambda n: self.cfg.ollama_model == n, name),
                    radio=True,
                )
                for name in self._ollama_models
            ]
        else:
            ollama_items = [Item("(none found - is ollama serve running?)", None, enabled=False)]
        ollama_items += [
            Menu.SEPARATOR,
            Item("Refresh model list", _bind(self._refresh_ollama)),
            Item(
                "Auto-polish every transcript",
                _bind(self._toggle_auto_polish),
                checked=_check(lambda: self.cfg.auto_polish),
            ),
        ]

        mode_items = [
            Item(
                label,
                _bind(self._set_hotkey_mode, mode),
                checked=_check(lambda m: self.cfg.hotkey_mode == m, mode),
                radio=True,
            )
            for mode, label in (
                ("hold", "Hold to talk (release to transcribe)"),
                ("toggle", "Tap to start, tap to stop"),
            )
        ]

        output_items = [
            Item(
                label,
                _bind(self._set_output_mode, mode),
                checked=_check(lambda m: self.cfg.output_mode == m, mode),
                radio=True,
            )
            for mode, label in (
                ("paste", "Paste at cursor (Ctrl+V)"),
                ("type", "Type it out keystroke by keystroke"),
                ("clipboard", "Copy to clipboard only"),
            )
        ]

        return Menu(
            # Callable text so the status line refreshes on update_menu().
            Item(lambda _item: self.status_detail, None, enabled=False),
            Item(lambda _item: f"Record: {self.cfg.record_hotkey}", None, enabled=False),
            Menu.SEPARATOR,
            Item("Show last transcript", _bind(self._post, self._show_review)),
            Item(
                "Open transcript window after each recording",
                _bind(self._toggle_review_window),
                checked=_check(lambda: self.cfg.open_review_window),
            ),
            Menu.SEPARATOR,
            # A Menu passed as the action is how pystray expresses a submenu.
            Item("Whisper model", Menu(*model_items)),
            Item("Microphone", Menu(*device_items)),
            Item("Ollama polish", Menu(*ollama_items)),
            Item("Hotkey mode", Menu(*mode_items)),
            Item("Output", Menu(*output_items)),
            Menu.SEPARATOR,
            Item("Run diagnostics", _bind(self._run_doctor)),
            Item("Open config folder", _bind(self._open_config_folder)),
            Item("Reload config file", _bind(self._reload_config)),
            Menu.SEPARATOR,
            Item("Quit", _bind(self._quit)),
        )


    # ------------------------------------------------------------------ startup

    def _rebind_hotkeys(self) -> None:
        self.hotkeys.stop()
        self.hotkeys = HotkeyManager()
        self.hotkeys.register(
            "record", self.cfg.record_hotkey, self.on_record_pressed, self.on_record_released
        )
        self.hotkeys.register("polish", self.cfg.polish_hotkey, self.on_polish_hotkey)
        self.hotkeys.register("review", self.cfg.review_hotkey, lambda: self._post(self._show_review))
        self.hotkeys.start()

    def run(self) -> None:
        self._root = tk.Tk()
        self._root.withdraw()  # tray-only app; the root window is never shown
        self._review = ReviewWindow(
            self._root, self._review_polish_request, self._review_paste_request
        )

        self._icon = pystray.Icon(
            "voice2cursor", icons.make_icon("idle"), "voice2cursor - starting...", self._build_menu()
        )
        # Detached so Tk keeps the main thread; pystray runs its own message loop.
        self._icon.run_detached()

        self._rebind_hotkeys()

        # Warm up off-thread: loading a model takes seconds and would otherwise
        # be paid on the user's first recording.
        threading.Thread(target=self._startup_worker, daemon=True).start()

        try:
            self._root.mainloop()
        except KeyboardInterrupt:
            self._quit()

    def _startup_worker(self) -> None:
        # Open the mic stream up front: on Bluetooth devices the first stream
        # takes most of a second to deliver data, which would otherwise eat the
        # start of the very first clip.
        try:
            self.recorder.open()
        except Exception as exc:
            self._notify(f"Microphone unavailable: {exc}")

        self._set_state("busy", f"loading {self.cfg.model_size}...")
        try:
            self.transcriber.ensure_loaded()
            # The first GPU inference in a fresh process pays a one-off ~10 s for
            # kernel selection. Spend it here, not on the user's first dictation.
            self._set_state("busy", "warming up...")
            self.transcriber.warmup()
            self._set_state("idle", self._idle_detail())
        except Exception as exc:
            self._fail(f"Model load failed: {type(exc).__name__}: {exc}")
        self._ollama_models = self.ollama.list_models()
        self._rebuild_menu()
        print(f"[voice2cursor] {self.transcriber.describe()}")
        print(f"[voice2cursor] hold {self.cfg.record_hotkey} to dictate; config at {CONFIG_PATH}")
