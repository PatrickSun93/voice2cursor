"""Self-diagnostic for voice2cursor, reached via `python -m voice2cursor --doctor`.

Walks the pipeline one layer at a time - interpreter, config, imports, CUDA,
microphone, model cache, hotkeys, clipboard, Ollama - so that a vague "nothing
happens when I press the hotkey" turns into a single [FAIL] line naming the
layer at fault.

Two rules shape this module:

* Every check is isolated. One broken layer must never hide the state of the
  others, so each check runs inside `_step`, which converts any unexpected
  exception into a FAIL line and carries on.
* Heavy third-party packages are imported lazily, inside the checks that test
  them. Importing sounddevice/pynput/requests at module scope would make the
  doctor itself crash on exactly the broken installs it exists to diagnose.
  Only `config` and `cuda_setup` are safe up here: both are stdlib-only.
"""
from __future__ import annotations

import importlib
import os
import platform
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import CONFIG_PATH, MODEL_SIZES, Config  # noqa: F401
from .cuda_setup import register_cuda_dlls
from .vocabulary import Vocabulary

_LABEL_W = 20
_INDENT = " " * 7  # aligns supplementary text under the tag column
_RULE = "=" * 66

# Approximate download sizes, used only to tell the user what a first run costs.
_DOWNLOAD_SIZES = {
    "tiny": "75MB",
    "base": "145MB",
    "small": "484MB",
    "medium": "1.5GB",
    "large-v3": "3.1GB",
    "distil-large-v3": "1.5GB",
}

# Hard runtime dependencies, in the order the app needs them.
# (import name, pip distribution name)
_CORE_IMPORTS = (
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("sounddevice", "sounddevice"),
    ("pynput", "pynput"),
    ("pyperclip", "pyperclip"),
    ("pystray", "pystray"),
    ("PIL", "pillow"),
    ("requests", "requests"),
)
_CUDA_IMPORTS = (
    ("ctranslate2", "ctranslate2"),
    ("faster_whisper", "faster-whisper"),
)

_MODIFIER_TOKENS = frozenset({"ctrl", "alt", "shift", "cmd"})
_VALID_DEVICES = ("auto", "cuda", "cpu")
_VALID_OUTPUT_MODES = ("paste", "type", "clipboard")
_VALID_HOTKEY_MODES = ("hold", "toggle")


# A freshly opened InputStream can take ~0.9 s to produce its first callback on
# some devices, so the mic check warms up before it starts timing.
_MIC_WARMUP_SECONDS = 1.2
_MIC_CAPTURE_SECONDS = 1.0
_MIC_ATTEMPTS = 3


def _has_nvidia_gpu() -> bool:
    """True if nvidia-smi reports a GPU. Used to decide whether missing CUDA
    wheels are a real failure or just an unused option on this machine."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:
        return False


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


class _Report:
    """Tallies results and prints aligned, colour-free lines (may be piped)."""

    def __init__(self) -> None:
        self.ok = 0
        self.warn = 0
        self.fail = 0
        # (label, detail, fix) of the first FAIL. The summary hint keys off it
        # because the first broken layer usually explains the ones after it.
        self.first_fail: tuple[str, str, str | None] | None = None

    def section(self, title: str) -> None:
        print()
        print(title)
        print("-" * len(title))

    def _emit(self, tag: str, label: str, detail: str) -> None:
        print(f"[{tag}] {label:<{_LABEL_W}}  {detail}".rstrip())

    def good(self, label: str, detail: str = "") -> None:
        self.ok += 1
        self._emit(" OK ", label, detail)

    def warning(self, label: str, detail: str = "") -> None:
        self.warn += 1
        self._emit("WARN", label, detail)

    def failure(self, label: str, detail: str = "", fix: str | None = None) -> None:
        self.fail += 1
        if self.first_fail is None:
            self.first_fail = (label, detail, fix)
        self._emit("FAIL", label, detail)

    def info(self, text: str) -> None:
        """Supplementary detail. Not a check, so it does not affect the tally."""
        print(f"{_INDENT}{text}")


@contextmanager
def _step(rep: _Report, label: str, fix: str | None = None) -> Iterator[None]:
    """Run a check, turning any unexpected exception into a FAIL line."""
    try:
        yield
    except Exception as exc:  # a doctor must survive anything it probes
        rep.failure(label, f"{type(exc).__name__}: {exc}", fix=fix)


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _device_label(device: str | None) -> str:
    return "system default" if not device else f"{device!r}"


# --------------------------------------------------------------------------
# 1. platform
# --------------------------------------------------------------------------


def _check_platform(rep: _Report) -> None:
    rep.section("1. Platform")

    with _step(rep, "python"):
        v = sys.version_info
        detail = f"{v.major}.{v.minor}.{v.micro}  ({sys.executable})"
        if v < (3, 10):
            rep.warning("python", f"{detail}  - below the supported minimum of 3.10")
        else:
            rep.good("python", detail)

    with _step(rep, "environment"):
        # Dependencies have to live in *this* prefix. A mismatch here is the
        # usual reason imports still fail after "but I did pip install it".
        rep.good("environment", sys.prefix)
        if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
            rep.info("not a virtualenv (packages come from the base interpreter)")

    with _step(rep, "os"):
        detail = (
            f"{platform.system()} {platform.release()} "
            f"{platform.version()} ({platform.machine()})"
        )
        try:
            from . import backends

            rep.good("os", f"{detail}  - {backends.NAME} backend")
        except Exception as exc:
            rep.failure(
                "os",
                f"{detail}  - no backend for this host ({exc})",
                fix="voice2cursor supports macOS and Windows",
            )


# --------------------------------------------------------------------------
# 2. config
# --------------------------------------------------------------------------


def _check_config(rep: _Report, cfg: Config, load_error: str | None) -> None:
    rep.section("2. Configuration")

    with _step(rep, "config file"):
        if load_error is not None:
            rep.failure(
                "config file",
                f"could not be read ({load_error}); defaults are in use",
                fix=f"repair or delete {CONFIG_PATH}",
            )
        elif os.path.exists(CONFIG_PATH):
            rep.good("config file", CONFIG_PATH)
        else:
            rep.good(
                "config file",
                f"not created yet, built-in defaults in use  ({CONFIG_PATH})",
            )
        rep.info(f"edit this file to change anything below: {CONFIG_PATH}")

    with _step(rep, "settings"):
        compute = cfg.compute_type
        if compute == "auto":
            compute = "auto (float16 on cuda, int8 on cpu)"
        rep.info(f"model_size      {cfg.model_size}")
        rep.info(f"device          {cfg.device}      compute_type {compute}")
        rep.info(
            f"language        {cfg.language or 'auto-detect'}      "
            f"beam_size {cfg.beam_size}      vad_filter {cfg.vad_filter}"
        )
        rep.info(f"input_device    {_device_label(cfg.input_device)}")
        rep.info(f"record_hotkey   {cfg.record_hotkey}      mode {cfg.hotkey_mode}")
        rep.info(f"polish_hotkey   {cfg.polish_hotkey}")
        rep.info(f"review_hotkey   {cfg.review_hotkey}")
        rep.info(
            f"output_mode     {cfg.output_mode}      "
            f"restore_clipboard {cfg.restore_clipboard}"
        )
        rep.info(
            f"auto_polish     {cfg.auto_polish}      "
            f"open_review_window {cfg.open_review_window}"
        )
        rep.info(f"ollama_url      {cfg.ollama_url}")
        rep.info(f"ollama_model    {cfg.ollama_model or '(first model Ollama reports)'}")

    with _step(rep, "vocabulary"):
        path = cfg.resolved_vocabulary_path()
        if not cfg.vocabulary:
            rep.good("vocabulary", "disabled (vocabulary=false)")
        elif not os.path.exists(path):
            rep.good("vocabulary", f"no file yet, no corrections applied  ({path})")
        else:
            try:
                vocab = Vocabulary.load(path)
                rep.good("vocabulary", f"{len(vocab)} spellings loaded from {path}")
            except (OSError, ValueError) as exc:
                rep.warning(
                    "vocabulary",
                    f"could not be parsed ({type(exc).__name__}: {exc}); "
                    f"transcripts will not be corrected - fix the JSON in {path}",
                )
        prompt_len = len(cfg.initial_prompt)
        rep.info(
            f"initial_prompt  {prompt_len} chars"
            + ("  (empty)" if not prompt_len else "")
        )

    with _step(rep, "setting values"):
        problems = []
        if cfg.model_size not in MODEL_SIZES:
            problems.append(f"model_size={cfg.model_size!r} not in {list(MODEL_SIZES)}")
        if cfg.device not in _VALID_DEVICES:
            problems.append(f"device={cfg.device!r} not in {list(_VALID_DEVICES)}")
        if cfg.output_mode not in _VALID_OUTPUT_MODES:
            problems.append(
                f"output_mode={cfg.output_mode!r} not in {list(_VALID_OUTPUT_MODES)}"
            )
        if cfg.hotkey_mode not in _VALID_HOTKEY_MODES:
            problems.append(
                f"hotkey_mode={cfg.hotkey_mode!r} not in {list(_VALID_HOTKEY_MODES)}"
            )
        if problems:
            rep.failure(
                "setting values",
                problems[0],
                fix=f"correct the offending key(s) in {CONFIG_PATH}",
            )
            for extra in problems[1:]:
                rep.info(extra)
        else:
            rep.good("setting values", "all recognised")


# --------------------------------------------------------------------------
# 3. imports
# --------------------------------------------------------------------------


def _module_version(module: object, dist: str) -> str:
    for attr in ("__version__", "VERSION", "version"):
        value = getattr(module, attr, None)
        if isinstance(value, str) and value:
            return value
    try:
        from importlib.metadata import version

        return version(dist)
    except Exception:
        return "version unknown"


def _try_import(rep: _Report, name: str, dist: str) -> object | None:
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # usually ImportError, but broken wheels raise more
        rep.failure(name, f"{type(exc).__name__}: {exc}", fix=f"pip install {dist}")
        return None
    rep.good(name, _module_version(module, dist))
    return module


def _check_imports(rep: _Report) -> None:
    rep.section("3. Imports")

    for name, dist in _CORE_IMPORTS:
        _try_import(rep, name, dist)

    # CTranslate2 resolves cuBLAS and cuDNN when its extension module loads, and
    # Windows does not search site-packages/nvidia/*/bin. Registering those dirs
    # AFTER the import would be too late: the DLL lookup has already failed and
    # CTranslate2 then offers CPU only, with no error the user ever sees.
    with _step(rep, "cuda dll dirs"):
        added = register_cuda_dlls()
        nvidia_base = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
        if added:
            rep.good(
                "cuda dll dirs", f"{len(added)} registered before the ctranslate2 import"
            )
            for path in added:
                rep.info(path)
        elif hasattr(os, "add_dll_directory"):
            # Only a failure if the user actually asked for GPU work. A machine
            # with no NVIDIA GPU, or device="cpu", is correctly configured
            # without these wheels, and must not exit non-zero.
            detail = f"no nvidia/*/bin directories under {nvidia_base}"
            fix = "pip install nvidia-cublas-cu12 nvidia-cudnn-cu12"
            if cfg.device == "cpu":
                rep.good("cuda dll dirs", f"{detail} - not needed, device is set to cpu")
            elif not _has_nvidia_gpu():
                rep.warning(
                    "cuda dll dirs", f"{detail} - no NVIDIA GPU detected, so CPU it is"
                )
            else:
                rep.failure("cuda dll dirs", detail, fix=fix)
        else:
            rep.good("cuda dll dirs", "not applicable, this is not Windows")

    for name, dist in _CUDA_IMPORTS:
        _try_import(rep, name, dist)


# --------------------------------------------------------------------------
# 4. cuda
# --------------------------------------------------------------------------


def _check_cuda(rep: _Report, cfg: Config) -> None:
    rep.section("4. CUDA and compute types")

    ct2 = None
    device_count = 0
    with _step(rep, "cuda devices"):
        import ctranslate2

        ct2 = ctranslate2
        device_count = ctranslate2.get_cuda_device_count()
        if device_count > 0:
            rep.good("cuda devices", f"{device_count} visible to CTranslate2")
        else:
            rep.warning(
                "cuda devices",
                "0 visible - the app will transcribe on CPU. That works, but it is "
                "several times slower than a GPU.",
            )

    if ct2 is not None:
        for device in ("cuda", "cpu"):
            label = f"compute types {device}"
            try:
                types = sorted(ct2.get_supported_compute_types(device))
                rep.good(label, ", ".join(types) if types else "none reported")
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                if device == "cuda":
                    # Expected on a machine with no usable GPU, so not a failure.
                    rep.warning(label, f"unavailable ({detail})")
                else:
                    rep.failure(label, detail, fix="reinstall ctranslate2")

        with _step(rep, "configured device"):
            wanted = cfg.device
            if wanted == "auto":
                wanted = "cuda" if device_count else "cpu"
            try:
                compute = cfg.resolved_compute_type(wanted)
            except Exception:
                compute = cfg.compute_type
            if wanted == "cuda" and device_count == 0:
                rep.warning(
                    "configured device",
                    f"config asks for {cfg.device!r} but no CUDA device is visible, so "
                    "the model load will fall back to CPU",
                )
            else:
                rep.good("configured device", f"{wanted} with compute_type {compute}")

    with _step(rep, "nvidia-smi"):
        # Purely informational. A missing nvidia-smi is normal on CPU machines.
        try:
            proc = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,memory.used",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            rep.good(
                "nvidia-smi",
                "not installed - expected on machines without an NVIDIA GPU",
            )
        except subprocess.TimeoutExpired:
            rep.warning("nvidia-smi", "timed out after 10s (the driver may be hung)")
        else:
            if proc.returncode == 0 and proc.stdout.strip():
                rep.good("nvidia-smi", "reported the following GPU(s)")
                for line in proc.stdout.strip().splitlines():
                    rep.info(line.strip())
            else:
                message = _first_line(proc.stderr) or _first_line(proc.stdout)
                rep.warning("nvidia-smi", message or f"exit code {proc.returncode}")


# --------------------------------------------------------------------------
# 5. audio
# --------------------------------------------------------------------------


def _check_audio(rep: _Report, cfg: Config) -> None:
    rep.section("5. Audio input")

    with _step(rep, "device list"):
        from .audio import list_input_devices

        devices = list_input_devices()
        if not devices:
            rep.failure(
                "device list",
                "no capture devices reported by PortAudio",
                fix="enable a microphone, then check Settings > Privacy > Microphone",
            )
        else:
            rep.good(
                "device list",
                f"{len(devices)} capture device(s), deduplicated by name",
            )
            for index, name in devices:
                mark = "   <- configured input_device" if cfg.input_device == index else ""
                rep.info(f"[{index:>2}] {name}{mark}")

    with _step(rep, "default input"):
        from .audio import default_input_name

        name = default_input_name()
        if name == "unknown":
            rep.warning("default input", "PortAudio could not name a default input device")
        else:
            rep.good("default input", name)

    with _step(rep, "16 kHz mono f32"):
        import sounddevice as sd

        from .audio import TARGET_RATE

        try:
            sd.check_input_settings(
                device=cfg.input_device,
                samplerate=TARGET_RATE,
                channels=1,
                dtype="float32",
            )
            rep.good("16 kHz mono f32", f"accepted by {_device_label(cfg.input_device)}")
        except Exception as exc:
            # Recorder falls back to the device's native rate and resamples, so
            # this costs a little CPU rather than breaking recording outright.
            native = ""
            try:
                info = sd.query_devices(cfg.input_device, "input")
                native = f"; native rate is {int(info['default_samplerate'])} Hz"
            except Exception:
                pass
            rep.warning(
                "16 kHz mono f32",
                f"rejected ({type(exc).__name__}: {exc}){native} - will record at the "
                "native rate and resample",
            )

    with _step(
        rep,
        "mic capture",
        fix="another app may hold the device exclusively; close it, or set "
        f"input_device to a working index in {CONFIG_PATH}",
    ):
        import numpy as np

        from .audio import TARGET_RATE, Recorder

        # A freshly opened stream can take most of a second to deliver its first
        # callback (0.93 s measured on a Bluetooth headset). Open it, let it warm
        # up, and only then time a capture - otherwise a healthy mic reads as dead.
        # Retried because the default device can change mid-check - a Bluetooth
        # headset connecting or dropping reassigns it, and a single attempt then
        # reports a healthy mic as dead.
        audio = None
        for attempt in range(_MIC_ATTEMPTS):
            recorder = Recorder(cfg.input_device)
            recorder.open()
            try:
                time.sleep(_MIC_WARMUP_SECONDS)
                recorder.start()
                time.sleep(_MIC_CAPTURE_SECONDS)
                audio = recorder.stop()
            finally:
                recorder.close()
            if audio is not None and audio.size:
                if attempt:
                    rep.info(f"succeeded on attempt {attempt + 1} of {_MIC_ATTEMPTS}")
                break

        count = int(audio.size) if audio is not None else 0
        if count == 0:
            rep.failure(
                "mic capture",
                f"no samples across {_MIC_ATTEMPTS} attempts of "
                f"{_MIC_CAPTURE_SECONDS:.1f} s each",
                fix="the driver accepted the stream without producing data. A "
                "Bluetooth mic that has gone idle is the usual cause; reconnect "
                "it, or pick a wired device via --list-devices and input_device",
            )
        else:
            rms = float(np.sqrt(np.mean(np.square(audio.astype(np.float64)))))
            peak = float(np.max(np.abs(audio)))
            detail = (
                f"{count} samples ({count / TARGET_RATE:.2f} s at {TARGET_RATE} Hz), "
                f"rms={rms:.6f} peak={peak:.6f}"
            )
            if rms == 0.0:
                rep.warning(
                    "mic capture",
                    f"{detail} - digital silence, so the mic is probably muted or its "
                    "input level is 0",
                )
            else:
                rep.good("mic capture", detail)


# --------------------------------------------------------------------------
# 6. whisper model cache
# --------------------------------------------------------------------------


def _hf_hub_dir() -> Path:
    """Where huggingface_hub will look for cached models."""
    # Honour the standard overrides before the documented default, otherwise this
    # check reports "will download" for anyone who has moved their HF cache.
    hub = os.environ.get("HF_HUB_CACHE")
    if hub:
        return Path(hub)
    home = os.environ.get("HF_HOME")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _check_model(rep: _Report, cfg: Config) -> None:
    rep.section("6. Whisper model cache")

    # Deliberately filesystem-only. Instantiating WhisperModel here would start a
    # multi-gigabyte download from a command the user ran to diagnose a problem.
    with _step(rep, "model cache"):
        hub = _hf_hub_dir()
        size = cfg.model_size
        repo_dir = hub / f"models--Systran--faster-whisper-{size}"
        approx = _DOWNLOAD_SIZES.get(size, "unknown size")

        if not hub.is_dir():
            rep.warning(
                "model cache",
                f"no Hugging Face cache at {hub} - {size} will download on first "
                f"use (~{approx})",
            )
            return

        if repo_dir.is_dir():
            if any(repo_dir.glob("snapshots/*/*")):
                rep.good("model cache", f"{size} is cached")
                rep.info(str(repo_dir))
            else:
                rep.warning(
                    "model cache",
                    f"{repo_dir} exists but holds no snapshot files, which means an "
                    f"interrupted download; delete it and let {size} (~{approx}) "
                    "fetch again",
                )
        else:
            # A few sizes ship under a variant repo name; globbing keeps the check
            # from announcing a download for a model that is already on disk.
            variants = [p for p in sorted(hub.glob(f"models--*{size}*")) if p != repo_dir]
            if variants:
                rep.good("model cache", f"{size} is cached under a variant repo name")
                rep.info(str(variants[0]))
            else:
                rep.warning(
                    "model cache",
                    f"{size} not cached - faster-whisper will download ~{approx} on "
                    "the first transcription, which looks like a hang",
                )
                rep.info(f"looked for {repo_dir}")

        cached = sorted(p.name for p in hub.glob("models--*whisper*"))
        if cached:
            rep.info(f"whisper repos already in the cache: {', '.join(cached)}")


# --------------------------------------------------------------------------
# 7. hotkeys
# --------------------------------------------------------------------------


def _check_hotkeys(rep: _Report, cfg: Config) -> None:
    rep.section("7. Hotkeys")

    # Guarded separately from the per-hotkey steps: importing .hotkeys pulls in
    # pynput, so a missing pynput must not be reported as a bad hotkey string.
    try:
        from .hotkeys import parse_hotkey
    except Exception as exc:
        rep.failure(
            "hotkeys module", f"{type(exc).__name__}: {exc}", fix="pip install pynput"
        )
        return

    specs = (
        ("record_hotkey", cfg.record_hotkey),
        ("polish_hotkey", cfg.polish_hotkey),
        ("review_hotkey", cfg.review_hotkey),
    )
    parsed: dict[str, frozenset[str]] = {}

    for name, spec in specs:
        with _step(rep, name, fix=f"fix the {name} value in {CONFIG_PATH}"):
            tokens = parse_hotkey(spec)
            parsed[name] = tokens
            rep.good(name, f"{spec!r} -> {{{', '.join(sorted(tokens))}}}")

    with _step(rep, "hotkey shape"):
        bare = [name for name, tokens in parsed.items() if tokens <= _MODIFIER_TOKENS]
        if bare:
            rep.warning(
                "hotkey shape",
                f"only modifier keys in {', '.join(bare)} - would fire whenever "
                "those modifiers are held",
            )
        else:
            rep.good("hotkey shape", "every hotkey pairs modifiers with a real key")

    with _step(rep, "hotkey collisions"):
        names = list(parsed)
        clashes = [
            f"{names[i]} == {names[j]}"
            for i in range(len(names))
            for j in range(i + 1, len(names))
            if parsed[names[i]] == parsed[names[j]]
        ]
        if clashes:
            rep.warning(
                "hotkey collisions",
                f"{'; '.join(clashes)} - identical token sets, so both actions fire "
                "on the same keypress",
            )
        elif len(parsed) == len(specs):
            rep.good("hotkey collisions", "all three resolve to distinct token sets")
        else:
            rep.good("hotkey collisions", "no duplicates among the hotkeys that parsed")


# --------------------------------------------------------------------------
# 8. clipboard
# --------------------------------------------------------------------------


def _check_clipboard(rep: _Report) -> None:
    rep.section("8. Clipboard round trip")

    with _step(
        rep,
        "clipboard",
        fix=f"set output_mode to 'type' in {CONFIG_PATH} to bypass the clipboard",
    ):
        import pyperclip

        try:
            original = pyperclip.paste()
        except Exception as exc:
            rep.failure(
                "clipboard read",
                f"{type(exc).__name__}: {exc}",
                fix="pyperclip found no working copy/paste mechanism; set output_mode "
                f"to 'type' in {CONFIG_PATH} to bypass the clipboard entirely",
            )
            return

        sentinel = f"voice2cursor-doctor-{os.getpid()}-{int(time.time())}"
        restored = False
        try:
            pyperclip.copy(sentinel)
            readback = pyperclip.paste()
            if readback == sentinel:
                rep.good("clipboard", f"wrote and read back {len(sentinel)} characters")
            else:
                rep.failure(
                    "clipboard",
                    f"read back {readback[:48]!r} instead of the sentinel",
                    fix="something else is overwriting the clipboard; clipboard "
                    "managers and remote-desktop sessions are the usual culprits",
                )
        finally:
            # The user's clipboard is not ours to keep, whatever went wrong above.
            try:
                pyperclip.copy(original)
                restored = True
            except Exception:
                pass

        if restored:
            rep.good(
                "clipboard restore",
                f"previous contents put back ({len(original)} characters)",
            )
        else:
            rep.warning(
                "clipboard restore",
                "could not put the previous clipboard contents back",
            )


# --------------------------------------------------------------------------
# 9. ollama
# --------------------------------------------------------------------------


def _check_ollama(rep: _Report, cfg: Config) -> None:
    rep.section("9. Ollama (optional polish step)")

    with _step(rep, "ollama server"):
        from .ollama_client import OllamaClient, OllamaError

        client = OllamaClient(cfg)
        if not client.is_up():
            # Never a FAIL: the app is built to fall back to the raw transcript.
            rep.warning("ollama server", f"unreachable at {cfg.ollama_url}")
            rep.info("Transcription still works; only the polish step is unavailable.")
            rep.info(
                "Start it with `ollama serve`, then `ollama pull llama3.2` "
                "(any chat model will do)."
            )
            return

        rep.good("ollama server", f"responding at {cfg.ollama_url}")

        models = client.list_models()
        if not models:
            rep.warning(
                "ollama models",
                "server is up but no models are installed - run `ollama pull llama3.2`",
            )
            return
        rep.good("ollama models", f"{len(models)} installed")
        for name in models:
            rep.info(name)

        try:
            chosen = client.resolve_model()
        except OllamaError as exc:
            rep.warning("polish model", str(exc))
            return

        if cfg.ollama_model and cfg.ollama_model != chosen:
            rep.warning(
                "polish model",
                f"configured {cfg.ollama_model!r} is not installed, falling back "
                f"to {chosen!r}",
            )
        elif cfg.ollama_model:
            rep.good("polish model", chosen)
        else:
            rep.good("polish model", f"{chosen}  (auto-picked, ollama_model is unset)")


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def _check_host(rep: "_Report", cfg: Config) -> None:
    """Whatever breaks on this host specifically -- TCC permissions on macOS,
    hotkey ownership on Windows. The backend owns the list; this calls it."""
    rep.section("10. Host")
    with _step(rep, "host"):
        from . import backends

        rep.info(f"{backends.NAME} backend, state in {backends.STATE_DIR}")
        backends.doctor_checks(rep, cfg)


def run_doctor(cfg: Config | None = None) -> int:
    """Run every diagnostic, print a report, and return the number of FAILures."""
    rep = _Report()

    load_error: str | None = None
    if cfg is None:
        try:
            cfg = Config.load()  # swallows malformed JSON itself, but be safe
        except Exception as exc:
            load_error = f"{type(exc).__name__}: {exc}"
            cfg = Config()

    print(_RULE)
    print("voice2cursor doctor")
    print(time.strftime("%Y-%m-%d %H:%M:%S"))
    print(_RULE)

    checks = (
        lambda: _check_platform(rep),
        lambda: _check_config(rep, cfg, load_error),
        lambda: _check_imports(rep),
        lambda: _check_cuda(rep, cfg),
        lambda: _check_audio(rep, cfg),
        lambda: _check_model(rep, cfg),
        lambda: _check_hotkeys(rep, cfg),
        lambda: _check_clipboard(rep),
        lambda: _check_ollama(rep, cfg),
        lambda: _check_host(rep, cfg),
    )
    # Second safety net: _step guards the individual checks, this guards the code
    # between them so one surprise cannot truncate the rest of the report.
    for check in checks:
        try:
            check()
        except Exception as exc:
            rep.failure("section aborted", f"{type(exc).__name__}: {exc}")

    print()
    print(_RULE)
    print(f"Summary: {rep.ok} OK, {rep.warn} WARN, {rep.fail} FAIL")
    if rep.fail and rep.first_fail is not None:
        label, detail, fix = rep.first_fail
        print(f"First failure:   [{label}] {detail}")
        print(f"Most likely fix: {fix or 'see the first [FAIL] line above'}")
    elif rep.warn:
        print("No failures. The WARN lines above are non-fatal - voice2cursor will run.")
    else:
        print("Everything checks out.")
    print(_RULE)

    return rep.fail


if __name__ == "__main__":
    sys.exit(run_doctor())
