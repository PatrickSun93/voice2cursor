"""Per-platform behaviour, picked once at import time.

Everything that differs between hosts lives behind this module, so no other
file in the package branches on the platform. A backend supplies:

    NAME                  human-readable host name
    STATE_DIR             config, logs, recordings and vocabulary live here
    LEGACY_STATE_DIRS     older locations to migrate a config out of
    PASTE_MODIFIER        "cmd" or "ctrl" -- the modifier that means paste
    DEFAULT_ENGINE        transcription engine to prefer on this host
    DEFAULT_UI            "tray", "menubar" or "headless" -- how this host normally runs
    DEFAULT_RECORD_HOTKEY a chord that is actually free on this host
    DEFAULT_KEEP_MIC_OPEN hold the mic stream open between clips (see audio.py)
    UI_FONT / MONO_FONT   Tk font families that exist here
    open_folder(path)     reveal a directory in the file manager
    open_text_file(path)  open a file in the default text editor
    play_sound(kind, on)  "start" | "done" | "error", best-effort
    clipboard_get/set     read and write the system clipboard
    single_instance_lock(name)  -> handle, or None if another instance holds it
    doctor_checks(rep, cfg)     host-specific diagnostics, may be a no-op

Deliberately no pynput import here: this module is imported during startup,
and on macOS importing pynput probes the accessibility API. PASTE_MODIFIER is
a string that output.py resolves to a key object only when it first pastes.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

_MODULES = {
    "darwin": "macos",
    "win32": "windows",
}


class UnsupportedPlatform(RuntimeError):
    """No backend for this host."""


if sys.platform not in _MODULES:
    raise UnsupportedPlatform(
        f"voice2cursor has no backend for {sys.platform!r} "
        f"(supported: {', '.join(sorted(_MODULES))})"
    )

backend: ModuleType = importlib.import_module(f".{_MODULES[sys.platform]}", __name__)

NAME: str = backend.NAME
STATE_DIR: str = backend.STATE_DIR
LEGACY_STATE_DIRS: tuple = backend.LEGACY_STATE_DIRS
PASTE_MODIFIER: str = backend.PASTE_MODIFIER
DEFAULT_ENGINE: str = backend.DEFAULT_ENGINE
DEFAULT_RECORD_HOTKEY: str = backend.DEFAULT_RECORD_HOTKEY
DEFAULT_UI: str = backend.DEFAULT_UI
DEFAULT_KEEP_MIC_OPEN: bool = backend.DEFAULT_KEEP_MIC_OPEN
UI_FONT: tuple = backend.UI_FONT
MONO_FONT: tuple = backend.MONO_FONT

open_folder = backend.open_folder
open_text_file = backend.open_text_file
play_sound = backend.play_sound
clipboard_get = backend.clipboard_get
clipboard_set = backend.clipboard_set
single_instance_lock = backend.single_instance_lock
doctor_checks = backend.doctor_checks

__all__ = [
    "NAME", "STATE_DIR", "LEGACY_STATE_DIRS", "PASTE_MODIFIER", "DEFAULT_ENGINE",
    "DEFAULT_RECORD_HOTKEY", "DEFAULT_UI", "DEFAULT_KEEP_MIC_OPEN", "UI_FONT",
    "MONO_FONT", "open_folder", "open_text_file", "play_sound", "clipboard_get",
    "clipboard_set", "single_instance_lock", "doctor_checks",
    "UnsupportedPlatform", "backend",
]
