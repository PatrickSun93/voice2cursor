"""Deliver transcribed text to whatever window has focus."""
from __future__ import annotations

import threading
import time

import pyperclip
from pynput.keyboard import Controller, Key

_kb = Controller()

# Give the OS a moment to register the clipboard write before Ctrl+V, and let
# the target app read it before the old contents go back.
_PASTE_DELAY = 0.06
_RESTORE_DELAY = 1.0


def deliver(text: str, mode: str = "paste", restore_clipboard: bool = True) -> None:
    """Send `text` to the focused window.

    paste     - clipboard + Ctrl+V; fast and preserves unicode
    type      - synthesise keystrokes; leaves the clipboard untouched
    clipboard - copy only, user pastes manually
    """
    if not text:
        return
    if mode == "type":
        _kb.type(text)
        return

    previous = _read_clipboard() if restore_clipboard else None
    pyperclip.copy(text)
    if mode == "clipboard":
        return

    time.sleep(_PASTE_DELAY)
    with _kb.pressed(Key.ctrl):
        _kb.press("v")
        _kb.release("v")

    if previous is not None:
        threading.Timer(_RESTORE_DELAY, _restore, args=(previous, text)).start()


def _read_clipboard() -> str:
    try:
        return pyperclip.paste()
    except Exception:
        return ""


def _restore(previous: str, ours: str) -> None:
    """Put the old clipboard back, unless the user copied something meanwhile."""
    try:
        if pyperclip.paste() == ours:
            pyperclip.copy(previous)
    except Exception:
        pass
