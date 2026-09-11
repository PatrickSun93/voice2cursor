"""macOS menu-bar item: the pipeline's state at a glance, and the common settings.

Drawn with AppKit directly rather than through pystray, which on a Mac wants
the main thread for itself and cannot share it with Tk; a status item is only a
handful of AppKit calls. No .app bundle is needed -- a LaunchAgent runs inside
the user's GUI session, so the launchd agent can show one. PyObjC is already
installed as a pynput dependency.

Threading: AppKit is only touched on the main thread, which runs the event
loop. The pipeline (headless.py) calls `on_change` from its listener and worker
threads, and that only schedules a redraw on the main thread.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import unicodedata

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSColor,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject
from PyObjCTools import AppHelper

from . import backends
from .config import CONFIG_PATH
from .logging_setup import LOG_PATH, get_logger

log = get_logger()

_HOTKEYS = {
    "alt_r": "Right Option",
    "cmd_r": "Right Command",
    "ctrl_r": "Right Control",
    "shift_r": "Right Shift",
}
_LANGUAGES = ((None, "Auto-detect"), ("zh", "Chinese"), ("en", "English"))
_OUTPUTS = (
    ("paste", "Paste at cursor"),
    ("type", "Type it out keystroke by keystroke"),
    ("clipboard", "Copy to clipboard only"),
)
_TOGGLES = (
    ("sounds", "Sounds"),
    ("restore_clipboard", "Restore clipboard after pasting"),
    ("save_recordings", "Keep recordings"),
    ("auto_polish", "Polish with Ollama"),
)
# Display columns, CJK counted as two, so one long dictation cannot stretch the menu.
_TEXT_WIDTH = 48


def _hotkey_label(spec: str) -> str:
    return _HOTKEYS.get(spec, spec)


def _shorten(text: str) -> str:
    text = " ".join(text.split())
    used = 0
    for i, ch in enumerate(text):
        used += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used > _TEXT_WIDTH:
            return text[:i] + "…"
    return text


def _describe(runner) -> tuple[str, bool, str]:
    """(SF Symbol, tinted red, status line), highest-priority state first."""
    if runner.loading:
        return "hourglass", False, "Loading model..."
    if runner.paused:
        return "mic.slash", False, "Paused - the hotkey is ignored"
    if runner.recording:
        return "mic.fill", True, "Recording - release to transcribe"
    if runner.busy:
        return "waveform", False, "Transcribing..."
    if runner.last_error:
        return "exclamationmark.triangle", False, "Last attempt failed"
    return "mic", False, f"Ready - hold {_hotkey_label(runner.cfg.record_hotkey)} to dictate"


class MenuBar(NSObject):
    @objc.python_method
    def attach(self, runner) -> None:
        self.runner = runner
        self.actions = []
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength)
        menu = NSMenu.alloc().init()
        menu.setDelegate_(self)
        self.status_item.setMenu_(menu)
        # Hooked up before the first draw, so no state change can fall between.
        runner.on_change = lambda: AppHelper.callAfter(self.redraw)
        self.redraw()

    @objc.python_method
    def redraw(self) -> None:
        symbol, red, text = _describe(self.runner)
        button = self.status_item.button()
        button.setImage_(
            NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, text))
        button.setContentTintColor_(NSColor.systemRedColor() if red else None)
        button.setToolTip_(f"voice2cursor - {text}")

    # NSMenuDelegate: rebuilt every time it opens, so it is never stale.
    @objc.typedSelector(b"v@:@")
    def menuNeedsUpdate_(self, menu) -> None:
        try:
            self.build(menu)
        except Exception:
            log.exception("menu build failed")

    @objc.IBAction
    def onItem_(self, sender) -> None:
        try:
            self.actions[sender.tag()]()
        except Exception:
            log.exception("menu action failed: %s", sender.title())

    @objc.python_method
    def add(self, menu, title, action=None, checked=None) -> None:
        """An item with no action is a greyed-out line of text."""
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, "onItem:" if action else None, "")
        if action:
            item.setTarget_(self)
            item.setTag_(len(self.actions))
            self.actions.append(action)
        if checked is not None:
            item.setState_(NSControlStateValueOn if checked else NSControlStateValueOff)
        menu.addItem_(item)

    @objc.python_method
    def submenu(self, menu, title):
        sub = NSMenu.alloc().initWithTitle_(title)
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setSubmenu_(sub)
        menu.addItem_(item)
        return sub

    @objc.python_method
    def build(self, menu) -> None:
        runner, cfg = self.runner, self.runner.cfg
        menu.removeAllItems()
        self.actions = []

        self.add(menu, _describe(runner)[2])
        if runner.last_error:
            when, message = runner.last_error
            self.add(menu, f"{when:%H:%M}  {_shorten(message)}")
        history = list(runner.history)
        if history:
            when, text = history[-1]
            self.add(menu, f"Last {when:%H:%M}: {_shorten(text)}")
            recent = self.submenu(menu, "Recent transcripts (click to copy)")
            for when, text in reversed(history):
                self.add(recent, f"{when:%H:%M}  {_shorten(text)}",
                         lambda t=text: backends.clipboard_set(t))

        menu.addItem_(NSMenuItem.separatorItem())
        self.add(menu, "Pause dictation", self.toggle_pause, checked=runner.paused)

        menu.addItem_(NSMenuItem.separatorItem())
        for name, title in _TOGGLES:
            on = bool(getattr(cfg, name))
            self.add(menu, title, lambda n=name, v=not on: self.set_option(n, v), checked=on)
        sub = self.submenu(menu, "Output")
        for mode, title in _OUTPUTS:
            self.add(sub, title, lambda m=mode: self.set_option("output_mode", m),
                     checked=cfg.output_mode == mode)
        sub = self.submenu(menu, "Language")
        language = cfg.language or None
        for code, title in _LANGUAGES:
            self.add(sub, title, lambda c=code: self.set_option("language", c),
                     checked=code == language)
        current = cfg.record_hotkey
        sub = self.submenu(menu, f"Hotkey: {_hotkey_label(current)}")
        for spec in list(_HOTKEYS) + ([] if current in _HOTKEYS else [current]):
            self.add(sub, _hotkey_label(spec),
                     lambda s=spec: self.set_option("record_hotkey", s),
                     checked=spec == current)

        menu.addItem_(NSMenuItem.separatorItem())
        self.add(menu, "Open recordings folder", self.open_recordings)
        self.add(menu, "Show log", lambda: backends.open_text_file(LOG_PATH))
        self.add(menu, "Edit config file (then Restart)", self.edit_config)

        menu.addItem_(NSMenuItem.separatorItem())
        self.add(menu, "Restart voice2cursor", self.restart)
        self.add(menu, "Quit voice2cursor", self.quit)

    # ---------- actions ----------

    @objc.python_method
    def toggle_pause(self) -> None:
        self.runner.paused = not self.runner.paused
        log.info("dictation %s", "paused" if self.runner.paused else "resumed")
        self.redraw()

    @objc.python_method
    def set_option(self, name, value) -> None:
        """Apply now and persist. The pipeline reads each of these at the point
        of use, except the hotkey, which needs the listener rebuilt."""
        cfg = self.runner.cfg
        setattr(cfg, name, value)
        cfg.save()
        if name == "record_hotkey":
            self.runner.listen()
        log.info("menu: %s = %r", name, value)
        self.redraw()

    @objc.python_method
    def open_recordings(self) -> None:
        path = self.runner.cfg.resolved_recordings_dir()
        os.makedirs(path, exist_ok=True)
        backends.open_folder(path)

    @objc.python_method
    def edit_config(self) -> None:
        if not os.path.exists(CONFIG_PATH):
            self.runner.cfg.save()
        backends.open_text_file(CONFIG_PATH)

    @objc.python_method
    def restart(self) -> None:
        """exec in place: launchd still sees the same process, and a terminal
        run comes back exactly as it was started."""
        log.info("restarting from the menu")
        self.runner.shutdown()
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])

    @objc.python_method
    def quit(self) -> None:
        """A clean exit (status 0). The LaunchAgent relaunches only after a
        crash, so this stays quit until the next login or scripts/macos/start.sh."""
        self.runner.shutdown()
        NSApplication.sharedApplication().terminate_(None)


def run_menubar(runner) -> int:
    """Run the pipeline under a menu-bar item. Blocks until quit."""
    # The AppKit loop never hands a pending KeyboardInterrupt back to Python, so
    # let Ctrl+C in a terminal end the process outright. SIGTERM from launchd is
    # already at its default, which does the same.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # no Dock icon
    bar = MenuBar.alloc().init()
    bar.attach(runner)
    # The model takes seconds to load; the icon shows an hourglass meanwhile.
    threading.Thread(target=runner.start, daemon=True).start()
    app.run()
    return 0
