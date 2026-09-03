"""Entry point: `python -m sonictype`.

CUDA DLL registration happens here, before anything can import faster_whisper,
because CTranslate2 resolves cuBLAS and cuDNN at import time.
"""
from __future__ import annotations

import argparse
import os
import sys

# Windows without Developer Mode cannot symlink into the HF cache, and the
# warning about it is noise the user can do nothing useful about.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from .cuda_setup import register_cuda_dlls  # noqa: E402

register_cuda_dlls()

from .config import CONFIG_PATH, MODEL_SIZES, Config  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m sonictype",
        description="Offline push-to-talk dictation: hotkey -> Whisper -> text at your cursor.",
    )
    p.add_argument("--doctor", action="store_true", help="run diagnostics and exit")
    p.add_argument("--list-devices", action="store_true", help="list microphones and exit")
    p.add_argument("--config", action="store_true", help="print the config file path and exit")
    p.add_argument(
        "--scan-hotkeys",
        action="store_true",
        help="check which global hotkeys other apps already own, and exit",
    )
    p.add_argument("--model", choices=MODEL_SIZES, help="override the Whisper model for this run")
    p.add_argument("--device", choices=("auto", "cuda", "cpu"), help="override compute device")
    p.add_argument(
        "--hotkey-mode", choices=("hold", "toggle"), help="override push-to-talk behaviour"
    )
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

    from .app import App

    App(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
