"""One rotating log file, plus stdout.

The Windows build printed to a console nobody sees once it runs from the
Startup folder, and the macOS build already logged properly. This is the macOS
one, made the default for both: a crash at 3pm is worth nothing if the only
record of it went to a hidden console window.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from .config import LOG_DIR

LOG_PATH = os.path.join(LOG_DIR, "voice2cursor.log")

_configured = False


def get_logger(name: str = "voice2cursor") -> logging.Logger:
    """The app logger, configured on first call."""
    global _configured
    log = logging.getLogger(name)
    if _configured:
        return log

    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(fmt)
        log.addHandler(handler)
    except OSError:
        # A read-only or missing state directory is not a reason to refuse to
        # start; stdout below still carries everything.
        pass

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.addHandler(stream)

    _configured = True
    return log
