"""Tray icons drawn at runtime, so there are no image assets to ship.

The icon doubles as the only always-visible status indicator: colour alone tells
you whether the app is idle, capturing, or working.
"""
from __future__ import annotations

from PIL import Image, ImageDraw

STATE_COLOURS = {
    "idle": (150, 158, 170),      # grey
    "recording": (226, 68, 68),   # red
    "busy": (232, 162, 44),       # amber
    "error": (140, 60, 160),      # purple
}
_SIZE = 64


def make_icon(state: str = "idle") -> Image.Image:
    """A microphone glyph tinted to indicate `state`."""
    colour = STATE_COLOURS.get(state, STATE_COLOURS["idle"])
    img = Image.new("RGBA", (_SIZE, _SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Capsule body.
    d.rounded_rectangle((24, 10, 40, 38), radius=8, fill=colour)
    # Cradle arc under the capsule.
    d.arc((18, 22, 46, 46), start=0, end=180, fill=colour, width=5)
    # Stand and base.
    d.line((32, 46, 32, 53), fill=colour, width=5)
    d.line((23, 54, 41, 54), fill=colour, width=5)

    if state == "recording":  # extra dot so it reads at 16px too
        d.ellipse((46, 4, 60, 18), fill=colour)
    return img
