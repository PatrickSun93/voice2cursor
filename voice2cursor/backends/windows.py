"""Windows backend.

Paste is Ctrl+V, sounds are the system event sounds, and state lives under
%APPDATA%. Config written by the SonicType-era builds is migrated on first run.
"""

from __future__ import annotations

import ctypes
import os
import subprocess

NAME = "Windows"

_APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~")
STATE_DIR = os.path.join(_APPDATA, "voice2cursor")

# The package shipped as "SonicType" before the two platforms were merged.
# config.py copies out of here once so an existing install keeps its settings.
LEGACY_STATE_DIRS = (os.path.join(_APPDATA, "SonicType"),)

PASTE_MODIFIER = "ctrl"

# faster-whisper with CUDA where there is an NVIDIA card, and its own CPU
# fallback where there is not.
DEFAULT_ENGINE = "faster_whisper"

# Not ctrl+alt+space, which the Claude desktop app already registers --
# a chord another app owns never reaches us, and nothing reports an error.
DEFAULT_RECORD_HOTKEY = "ctrl+shift+space"

# The tray icon is how this is used on Windows: no console, launched hidden
# from the Startup folder, everything reachable from the menu.
DEFAULT_UI = "tray"

UI_FONT = ("Segoe UI", 10)
MONO_FONT = ("Consolas", 10)

# MB_* constants: the system event sounds, which respect the user's sound
# scheme instead of hardcoding a file that a theme may have replaced.
_SOUNDS = {"start": 0x00000040, "done": 0x00000000, "error": 0x00000010}


def open_folder(path: str) -> None:
    subprocess.Popen(["explorer", path])


def open_text_file(path: str) -> None:
    # `start` picks whatever the user associated with .json / .txt; notepad
    # would override a deliberate choice of editor.
    subprocess.Popen(["cmd", "/c", "start", "", path], shell=False)


def play_sound(kind: str, enabled: bool = True) -> None:
    flag = _SOUNDS.get(kind)
    if not enabled or flag is None:
        return
    try:
        import winsound

        winsound.MessageBeep(flag)
    except Exception:
        pass


def clipboard_set(text: str) -> None:
    import pyperclip

    pyperclip.copy(text)


def clipboard_get() -> str:
    import pyperclip

    return pyperclip.paste()


def single_instance_lock(name: str = "lock"):
    """A named mutex, or None if another instance already holds it.

    Windows has no flock. A named kernel object is the equivalent: the second
    process's CreateMutexW succeeds but reports ERROR_ALREADY_EXISTS, which is
    how it learns to bow out.
    """
    ERROR_ALREADY_EXISTS = 183
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, f"Global\\voice2cursor.{name}")
    if not handle:
        return None
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return handle  # caller keeps the reference; the OS frees it on exit


def doctor_checks(rep, cfg) -> None:
    """Hotkey ownership, which is what actually breaks on this host.

    Another app holding the chord through RegisterHotKey means the key never
    reaches us, and nothing anywhere reports an error.
    """
    from ..hotkey_scan import probe

    for label in ("record_hotkey", "polish_hotkey", "review_hotkey"):
        spec = getattr(cfg, label, "")
        if not spec:
            continue
        status = probe(spec)
        if status == "free":
            rep.good(label, spec)
        elif status == "taken":
            rep.failure(label, f"{spec} is already registered by another app",
                        fix="python -m voice2cursor --scan-hotkeys  (lists free chords)")
        else:
            rep.warning(label, f"{spec}: {status}")
