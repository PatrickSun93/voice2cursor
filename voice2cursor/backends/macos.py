"""macOS backend.

Paste is Cmd+V, sounds are the system AIFFs through afplay, and state lives in
~/.voice2cursor -- the directory the launchd agent and its wrapper already use.
"""

from __future__ import annotations

import fcntl
import os
import subprocess

NAME = "macOS"

# Kept at ~/.voice2cursor rather than ~/Library/Application Support because the
# launchd agent, its wrapper script and the log paths in install.sh all point
# here already, and moving it would orphan an installed agent.
STATE_DIR = os.path.expanduser("~/.voice2cursor")
LEGACY_STATE_DIRS: tuple = ()

PASTE_MODIFIER = "cmd"

# mlx-whisper runs on the Apple Silicon GPU; faster-whisper would fall back to
# CPU here, which is several times slower for the same model.
DEFAULT_ENGINE = "mlx_whisper"

# The single right-Option key: macOS has no free Ctrl+Alt+letter chords worth
# betting on, and a lone modifier is the gesture push-to-talk actually wants.
DEFAULT_RECORD_HOTKEY = "alt_r"

# A menu-bar item drawn straight through AppKit (menubar.py). No .app bundle is
# needed: a LaunchAgent runs inside the user's GUI session and can show one.
# --headless still works, and --tray from a terminal if pystray is installed.
DEFAULT_UI = "menubar"

# An open Bluetooth microphone pins the headset to its hands-free profile, so
# everything played through it drops to telephone quality for as long as the
# stream is up. Open the mic per clip instead, giving up the pre-roll.
DEFAULT_KEEP_MIC_OPEN = False

UI_FONT = ("SF Pro Text", 13)
MONO_FONT = ("SF Mono", 12)

_SOUNDS = {
    "start": "/System/Library/Sounds/Tink.aiff",
    "done": "/System/Library/Sounds/Pop.aiff",
    "error": "/System/Library/Sounds/Basso.aiff",
}


def open_folder(path: str) -> None:
    subprocess.Popen(["open", path])


def open_text_file(path: str) -> None:
    # -t is "the default text editor", which respects whatever the user set
    # rather than forcing TextEdit.
    subprocess.Popen(["open", "-t", path])


def play_sound(kind: str, enabled: bool = True) -> None:
    path = _SOUNDS.get(kind)
    if not enabled or not path:
        return
    subprocess.Popen(["afplay", path],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def clipboard_set(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)


def clipboard_get() -> str:
    out = subprocess.run(["pbpaste"], capture_output=True)
    return out.stdout.decode("utf-8", "replace")


def single_instance_lock(name: str = "lock"):
    """An exclusive flock, or None if another instance already holds it.

    Without this a launchd-managed copy and a hand-started debug copy both
    listen for the hotkey, and every press fires twice.
    """
    os.makedirs(STATE_DIR, exist_ok=True)
    handle = open(os.path.join(STATE_DIR, name), "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle  # caller keeps the reference; closing it drops the lock


def doctor_checks(rep, cfg) -> None:
    """TCC permissions, which is what actually breaks on this host.

    Accessibility and Input Monitoring are granted per executable, and the
    executable is the venv's python -- not Terminal, and not the system python.
    Until both are granted the hotkey never fires and the paste silently does
    nothing, with no error raised anywhere.
    """
    import sys

    rep.info(f"interpreter: {sys.executable}")
    rep.info("Accessibility AND Input Monitoring must both list that exact path")
    rep.info("System Settings > Privacy & Security")

    # A paste that no-ops is the symptom; osascript refusing is the cause, and
    # it is the one part we can actually test from here.
    try:
        out = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to name of first process'],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            rep.good("Accessibility", "System Events accepts scripted input")
        else:
            rep.failure(
                "Accessibility",
                (out.stderr or "osascript refused").strip()[:160],
                fix=f"grant Accessibility to {sys.executable}",
            )
    except FileNotFoundError:
        rep.warning("Accessibility", "osascript not found")
    except subprocess.TimeoutExpired:
        # The permission prompt is modal and blocks osascript until answered.
        rep.warning("Accessibility", "osascript timed out (a permission dialog may be open)")
