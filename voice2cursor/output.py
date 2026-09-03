"""Deliver transcribed text to whatever window has focus."""

from __future__ import annotations

import threading
import time

from pynput.keyboard import Controller, Key

from . import backends

_kb = Controller()

# Cmd+V on macOS, Ctrl+V on Windows. Resolved once, from the backend, so this
# module never asks which platform it is on.
_PASTE_MODIFIER = getattr(Key, backends.PASTE_MODIFIER)

# Give the OS a moment to register the clipboard write before the paste, and
# let the target app read it before the old contents go back.
_PASTE_DELAY = 0.06
_RESTORE_DELAY = 1.0


def deliver(text: str, mode: str = "paste", restore_clipboard: bool = True) -> None:
    """Send `text` to the focused window.

    paste     - clipboard + the platform paste chord; preserves unicode
    type      - synthesise keystrokes; leaves the clipboard untouched
    clipboard - copy only, the user pastes manually
    """
    if not text:
        return
    if mode == "type":
        _kb.type(text)
        return

    previous = _read_clipboard() if restore_clipboard else None
    backends.clipboard_set(text)
    if mode == "clipboard":
        return

    time.sleep(_PASTE_DELAY)
    with _kb.pressed(_PASTE_MODIFIER):
        _kb.press("v")
        _kb.release("v")

    if previous is not None:
        threading.Timer(_RESTORE_DELAY, _restore, args=(previous, text)).start()


def _read_clipboard() -> str:
    try:
        return backends.clipboard_get()
    except Exception:
        return ""


def _restore(previous: str, ours: str) -> None:
    """Put the old clipboard back, unless the user copied something meanwhile."""
    try:
        if backends.clipboard_get() == ours:
            backends.clipboard_set(previous)
    except Exception:
        pass
