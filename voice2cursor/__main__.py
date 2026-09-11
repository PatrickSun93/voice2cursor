"""Entry point: `python -m voice2cursor`.

CUDA DLL registration happens here, before anything can import faster_whisper,
because CTranslate2 resolves cuBLAS and cuDNN at import time. It is a no-op on
hosts without `os.add_dll_directory`, so it costs nothing on macOS.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys

# Windows without Developer Mode cannot symlink into the HF cache, and the
# warning about it is noise the user can do nothing useful about.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from .cuda_setup import register_cuda_dlls  # noqa: E402

register_cuda_dlls()

from . import backends  # noqa: E402
from .config import CONFIG_PATH, MODEL_SIZES, Config  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m voice2cursor",
        description="Offline push-to-talk dictation: hotkey -> Whisper -> text at your cursor.",
    )
    p.add_argument("--doctor", action="store_true", help="run diagnostics and exit")
    p.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    p.add_argument("--config", action="store_true", help="print the config file path and exit")
    p.add_argument(
        "--scan-hotkeys",
        action="store_true",
        help="check which global hotkeys other apps already own, and exit (Windows)",
    )
    p.add_argument("--model", help="override the Whisper model for this run")
    p.add_argument("--engine", choices=("auto", "faster_whisper", "mlx_whisper"),
                   help="override the transcription engine")
    p.add_argument("--device", choices=("auto", "cuda", "cpu"), help="override compute device")
    p.add_argument(
        "--hotkey-mode", choices=("hold", "toggle"), help="override push-to-talk behaviour"
    )
    ui = p.add_mutually_exclusive_group()
    ui.add_argument("--tray", dest="ui", action="store_const", const="tray",
                    help="run with a tray icon (default on Windows)")
    ui.add_argument("--menubar", dest="ui", action="store_const", const="menubar",
                    help="run with a menu-bar icon (default on macOS)")
    ui.add_argument("--headless", dest="ui", action="store_const", const="headless",
                    help="no icon, log to a file")
    p.set_defaults(ui=None)
    p.add_argument("--allow-multiple", action="store_true",
                   help="skip the single-instance check")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if args.config:
        print(CONFIG_PATH)
        return 0

    if args.list_devices:
        from .audio import default_input_name, list_input_devices

        print(f"default input: {default_input_name()}\n")
        for idx, name in list_input_devices():
            print(f"  [{idx:>3}] {name}")
        return 0

    cfg = Config.load()
    # CLI overrides apply to this run only; they are deliberately not saved.
    if args.model:
        cfg.model_size = args.model
    if args.engine:
        cfg.engine = args.engine
    if args.device:
        cfg.device = args.device
    if args.hotkey_mode:
        cfg.hotkey_mode = args.hotkey_mode

    if args.scan_hotkeys:
        from .hotkey_scan import run_scan

        return 1 if run_scan({
            "record": cfg.record_hotkey,
            "polish": cfg.polish_hotkey,
            "review": cfg.review_hotkey,
        }) else 0

    if args.doctor:
        from .doctor import run_doctor

        return 1 if run_doctor(cfg) else 0

    # Two copies both listening for the hotkey means every press fires twice,
    # which is exactly what happens when a debug run joins the autostarted one.
    lock = None
    if not args.allow_multiple:
        lock = backends.single_instance_lock()
        if lock is None:
            print("voice2cursor is already running "
                  "(pass --allow-multiple to start a second copy)", file=sys.stderr)
            return 1

    ui = args.ui or backends.DEFAULT_UI
    if ui == "tray":
        from .app import App

        App(cfg).run()
        return 0

    from .headless import Headless

    runner = Headless(cfg)
    if ui == "menubar":
        from .menubar import run_menubar

        return run_menubar(runner)

    # SIGTERM is what launchd and a plain `kill` send; without this the agent
    # dies mid-transcription instead of finishing and closing the stream.
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, lambda *_: runner.stop())
        except (ValueError, OSError):
            pass
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
