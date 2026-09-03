"""Global hotkeys with both press and release events.

pynput's own GlobalHotKeys only fires on press, which cannot express
push-to-talk. This tracks the pressed-key set instead so a combination can
report activation and deactivation separately.
"""
from __future__ import annotations

import threading
from typing import Callable

from pynput import keyboard

# Left/right variants collapse to one token so either Ctrl key works.
_ALIASES = {
    "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "shift_l": "shift", "shift_r": "shift",
    "cmd_l": "cmd", "cmd_r": "cmd", "win": "cmd",
    "return": "enter", "escape": "esc",
    # Either spelling may appear in a config file; both normalise to the name
    # hotkey_scan.py uses.
    "`": "backtick", "~": "backtick", "\\": "backslash",
    ";": "semicolon", ",": "comma", ".": "period", "/": "slash",
}

# Punctuation keys translate to a character only for some modifier states -
# with Ctrl held ToUnicodeEx declines and reports no character - so they must
# be resolved by virtual key code instead. Names match hotkey_scan._VKS, so a
# combination the scanner reports free is one the listener can actually match.
_VK_NAMES = {
    0xC0: "backtick",
    0xDC: "backslash",
    0xBA: "semicolon",
    0xBC: "comma",
    0xBE: "period",
    0xBF: "slash",
}


def _token(key) -> str | None:
    """Normalise a pynput key event to a lowercase token, or None if unknown."""
    if isinstance(key, keyboard.Key):
        return _ALIASES.get(key.name, key.name)
    if isinstance(key, keyboard.KeyCode):
        # With Ctrl held, Windows reports control characters (Ctrl+P -> '\x10'),
        # so trust the virtual key code for letters and digits.
        vk = getattr(key, "vk", None)
        if vk is not None and (0x30 <= vk <= 0x5A):
            return chr(vk).lower()
        if vk in _VK_NAMES:
            return _VK_NAMES[vk]
        if key.char and key.char.isprintable():
            return key.char.lower()
        if vk is not None:
            return f"vk{vk}"
    return None


def parse_hotkey(spec: str) -> frozenset[str]:
    """Parse "ctrl+alt+space" or "<ctrl>+<alt>+<space>" into a token set."""
    tokens = set()
    for part in spec.split("+"):
        part = part.strip().strip("<>").lower()
        if part:
            tokens.add(_ALIASES.get(part, part))
    if not tokens:
        raise ValueError(f"empty hotkey: {spec!r}")
    return frozenset(tokens)


class HotkeyManager:
    """Watches the keyboard and reports when registered combinations engage.

    Callbacks run on the listener thread, so they must not block.
    """

    def __init__(self):
        self._pressed: set[str] = set()
        self._combos: dict[str, frozenset[str]] = {}
        self._on_press: dict[str, Callable[[], None]] = {}
        self._on_release: dict[str, Callable[[], None]] = {}
        self._engaged: set[str] = set()
        self._lock = threading.Lock()
        self._listener: keyboard.Listener | None = None

    def register(
        self,
        name: str,
        spec: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None] | None = None,
    ) -> None:
        self._combos[name] = parse_hotkey(spec)
        self._on_press[name] = on_press
        if on_release:
            self._on_release[name] = on_release

    def start(self) -> None:
        self._listener = keyboard.Listener(
            on_press=self._handle_press, on_release=self._handle_release
        )
        self._listener.daemon = True
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None

    # ---------- listener callbacks ----------

    def _handle_press(self, key) -> None:
        tok = _token(key)
        if tok is None:
            return
        with self._lock:
            self._pressed.add(tok)
            newly = [
                n for n, combo in self._combos.items()
                if combo <= self._pressed and n not in self._engaged
            ]
            self._engaged.update(newly)
        for name in newly:
            self._fire(self._on_press.get(name))

    def _handle_release(self, key) -> None:
        tok = _token(key)
        if tok is None:
            return
        with self._lock:
            self._pressed.discard(tok)
            # A combo disengages as soon as any of its keys is no longer held.
            dropped = [
                n for n in list(self._engaged)
                if not self._combos[n] <= self._pressed
            ]
            for name in dropped:
                self._engaged.discard(name)
        for name in dropped:
            self._fire(self._on_release.get(name))

    @staticmethod
    def _fire(cb: Callable[[], None] | None) -> None:
        if cb is None:
            return
        try:
            cb()
        except Exception:  # never let a callback kill the listener thread
            import traceback

            traceback.print_exc()

    # ---------- state queries ----------

    _MODIFIERS = frozenset({"ctrl", "alt", "shift", "cmd"})

    def modifiers_held(self) -> bool:
        with self._lock:
            return bool(self._MODIFIERS & self._pressed)

    def wait_for_modifiers_clear(self, timeout: float = 2.0, poll: float = 0.02) -> bool:
        """Block until no modifier key is physically held, or `timeout` elapses.

        Pasting sends Ctrl+V. If the user is still holding the Ctrl+Alt of the
        record hotkey, that arrives as Ctrl+Alt+V, which most apps ignore - the
        text would silently vanish. Transcription usually outlasts the key
        release, but on very short clips it does not.
        """
        import time

        deadline = time.monotonic() + timeout
        while self.modifiers_held():
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)
        return True
