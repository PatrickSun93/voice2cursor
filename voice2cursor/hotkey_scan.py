"""Find which global hotkeys are already claimed, via `--scan-hotkeys`.

Uses the Win32 `RegisterHotKey` API: if a combination is already registered by
another process the call fails with ERROR_HOTKEY_ALREADY_REGISTERED, so a
register/unregister round trip is a non-destructive way to test availability.

Known blind spot: this only sees hotkeys registered through `RegisterHotKey`.
Applications that watch the keyboard with a low-level hook instead - voice2cursor
itself does, via pynput - are invisible here. So FREE means "no RegisterHotKey
owner", not "guaranteed unused". Editors and terminals that consume a chord
only while focused also will not show up.
"""
from __future__ import annotations

import ctypes

_MODIFIERS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}

# Virtual key codes. Letters and digits follow ASCII; the rest are named.
_VKS: dict[str, int] = {
    **{chr(0x41 + i).lower(): 0x41 + i for i in range(26)},
    **{str(d): 0x30 + d for d in range(10)},
    "space": 0x20,
    "enter": 0x0D,
    "tab": 0x09,
    "backspace": 0x08,
    "insert": 0x2D,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},  # F1=0x70 .. F24=0x87
    "backtick": 0xC0,
    "backslash": 0xDC,
    "semicolon": 0xBA,
    "comma": 0xBC,
    "period": 0xBE,
    "slash": 0xBF,
}

# Probed by default: plausible push-to-talk chords plus the function keys that
# tend to be free because no physical key sends them.
_CANDIDATES = (
    "ctrl+alt+space", "ctrl+shift+space", "ctrl+win+space", "win+alt+space",
    "win+shift+space", "ctrl+alt+shift+space",
    "ctrl+alt+d", "ctrl+alt+g", "ctrl+alt+j", "ctrl+alt+k", "ctrl+alt+m",
    "ctrl+alt+q", "ctrl+alt+w", "ctrl+alt+z", "ctrl+alt+p", "ctrl+alt+r",
    "ctrl+shift+d", "ctrl+shift+j", "ctrl+shift+m", "ctrl+shift+q",
    "win+alt+d", "win+alt+v",
    "f13", "f14", "f15", "f16",
    "ctrl+alt+f13", "ctrl+alt+backtick", "ctrl+shift+backtick",
)

_ERROR_HOTKEY_ALREADY_REGISTERED = 1409
_PROBE_ID_BASE = 0xBEE0


def parse(spec: str) -> tuple[int, int]:
    """Turn "ctrl+alt+space" into (modifier mask, virtual key code)."""
    parts = [p.strip().strip("<>").lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey: {spec!r}")
    mods = 0
    for part in parts[:-1]:
        if part not in _MODIFIERS:
            raise ValueError(f"{part!r} is not a modifier in {spec!r}")
        mods |= _MODIFIERS[part]
    key = parts[-1]
    if key not in _VKS:
        raise ValueError(f"unknown key {key!r} in {spec!r}")
    return mods, _VKS[key]


def probe(spec: str, probe_id: int = _PROBE_ID_BASE) -> str:
    """Return "free", "taken", or "error: ...". Never leaves a hotkey behind."""
    try:
        mods, vk = parse(spec)
    except ValueError as exc:
        return f"error: {exc}"

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    # hWnd=None registers against the calling thread, so no window is needed.
    if user32.RegisterHotKey(None, probe_id, mods, vk):
        user32.UnregisterHotKey(None, probe_id)
        return "free"
    err = ctypes.get_last_error()
    if err == _ERROR_HOTKEY_ALREADY_REGISTERED:
        return "taken"
    return f"error: win32 error {err}"


def scan(specs: tuple[str, ...] | list[str] | None = None) -> list[tuple[str, str]]:
    """Probe each spec, returning (spec, status) pairs in the order given."""
    specs = tuple(specs) if specs else _CANDIDATES
    return [
        (spec, probe(spec, _PROBE_ID_BASE + i)) for i, spec in enumerate(specs)
    ]


def run_scan(current: dict[str, str] | None = None) -> int:
    """Print a report. Returns the number of configured hotkeys that are taken."""
    import os

    if os.name != "nt":
        print("Hotkey scanning uses the Win32 RegisterHotKey API; Windows only.")
        return 0

    conflicts = 0
    if current:
        print("Your configured hotkeys")
        print("-----------------------")
        for label, spec in current.items():
            status = probe(spec)
            flag = {"free": "[ OK ]", "taken": "[FAIL]"}.get(status, "[WARN]")
            note = ""
            if status == "taken":
                conflicts += 1
                note = "  <- another app owns this; pick one marked free below"
            print(f"{flag} {label:<16} {spec:<24} {status}{note}")
        print()

    print("Candidate hotkeys")
    print("-----------------")
    free, taken = [], []
    for spec, status in scan():
        (free if status == "free" else taken).append(spec)
        mark = {"free": "free ", "taken": "TAKEN"}.get(status, status)
        print(f"  {mark}  {spec}")

    print()
    print(f"{len(free)} free, {len(taken)} taken")
    if taken:
        print(f"taken: {', '.join(taken)}")
    print()
    print("Note: this detects hotkeys registered via RegisterHotKey. An app that")
    print("uses a low-level keyboard hook instead will not appear, so 'free' is")
    print("a strong hint rather than a guarantee. Stop voice2cursor before scanning,")
    print("or its own hotkeys may be reported by whatever else is listening.")
    return conflicts


if __name__ == "__main__":
    import sys

    sys.exit(run_scan())
