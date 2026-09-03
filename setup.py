r"""Create or update the `sonictype` conda env, then prove the three things that
actually break on a fresh machine: the interpreter, CUDA visibility through the
pip-installed NVIDIA DLLs, and a usable input device.

This is the Python port of setup.ps1, for machines where PowerShell is blocked
by policy. Safe to re-run.

Run it with a *bootstrap* interpreter -- conda's base python, not the sonictype
env (which may not exist yet) and never a bare `python` (PATH here resolves to
a stale C:\Python34):

    %LOCALAPPDATA%\miniconda3\python.exe setup.py

or just double-click setup.vbs, which finds that interpreter for you.

    --verify-only   skip conda entirely and only run the checks
    --env-name X    operate on env X instead of `sonictype`
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(PROJECT_ROOT, "environment.yml")
OLLAMA_URL = "http://127.0.0.1:11434/api/tags"

# Runs inside the target env and reports the facts as one JSON line, so this
# script never has to guess whether the CUDA DLL shim worked.
VERIFY_SRC = r"""
import json, sys

out = {"python": sys.version.split()[0], "prefix": sys.prefix,
       "cuda": False, "inputs": 0, "notes": []}
try:
    from sonictype.cuda_setup import cuda_available
    out["cuda"] = bool(cuda_available())
except Exception as exc:
    out["notes"].append("cuda: %s: %s" % (type(exc).__name__, exc))
try:
    from sonictype.audio import list_input_devices
    out["inputs"] = len(list_input_devices())
except Exception as exc:
    out["notes"].append("audio: %s: %s" % (type(exc).__name__, exc))
print("SONICTYPE_VERIFY " + json.dumps(out))
"""


class SetupError(Exception):
    """Anything that should abort setup with a readable message."""


# --------------------------------------------------------------- conda lookup

def resolve_conda() -> str:
    """Locate conda.exe, probing known installs before trusting PATH."""
    local = os.environ.get("LOCALAPPDATA", "")
    home = os.environ.get("USERPROFILE", "")
    candidates = [
        os.path.join(local, "miniconda3", "Scripts", "conda.exe"),
        os.path.join(local, "Anaconda3", "Scripts", "conda.exe"),
        os.path.join(home, "miniconda3", "Scripts", "conda.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    found = shutil.which("conda")
    if found:
        return found

    checked = "\n  ".join(candidates)
    raise SetupError(
        "conda was not found. Install Miniconda for the current user, then re-run:\n\n"
        "    winget install --id Anaconda.Miniconda3 --scope user\n\n"
        "Checked:\n  %s\n  PATH (shutil.which)" % checked
    )


def _env_rows(conda: str) -> list[list[str]]:
    """Parse `conda env list` into [name, ..., path] field lists."""
    proc = subprocess.run([conda, "env", "list"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SetupError("`conda env list` failed (exit %d):\n%s"
                         % (proc.returncode, proc.stderr.strip()))
    if not proc.stdout.strip():
        raise SetupError("conda env list returned nothing -- is this conda install healthy?")

    rows = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(line.split())
    return rows


def env_path(conda: str, name: str) -> str | None:
    """Return the install path of env `name`, or None if it does not exist."""
    for fields in _env_rows(conda):
        # `conda env list` prints "<name>  [*]  <path>".
        if fields[0] == name:
            return fields[-1]
    return None


# ------------------------------------------------------------------ the steps

def build_env(conda: str, name: str) -> None:
    if not os.path.isfile(ENV_FILE):
        raise SetupError("environment.yml not found at %s" % ENV_FILE)

    if env_path(conda, name):
        print("Env '%s' exists -- updating from environment.yml (--prune)..." % name)
        cmd = [conda, "env", "update", "--name", name, "--file", ENV_FILE, "--prune"]
    else:
        print("Env '%s' not found -- creating from environment.yml..." % name)
        cmd = [conda, "env", "create", "--name", name, "--file", ENV_FILE]

    # Inherit stdio: conda's progress output is the only sign of life during a
    # multi-minute solve, so it must stream rather than be captured.
    code = subprocess.run(cmd).returncode
    if code != 0:
        raise SetupError("conda env create/update failed (exit %d)." % code)


def resolve_env_python(conda: str, name: str, built: bool = True) -> str:
    path = env_path(conda, name)
    if not path:
        if built:
            raise SetupError("Env '%s' still not listed by conda after create/update." % name)
        raise SetupError("Env '%s' does not exist. Re-run without --verify-only to create it."
                         % name)
    python = os.path.join(path, "python.exe")
    if not os.path.isfile(python):
        raise SetupError("No python.exe at %s" % python)
    return python


def verify(env_python: str) -> dict:
    """Run VERIFY_SRC inside the target env and parse its one JSON line."""
    # cwd must be the project root so `import sonictype.*` resolves.
    proc = subprocess.run(
        [env_python, "-"], input=VERIFY_SRC,
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("SONICTYPE_VERIFY "):
            return json.loads(line[len("SONICTYPE_VERIFY "):])

    detail = (proc.stdout + proc.stderr).strip()
    raise SetupError("Verification script produced no result line.\n%s" % detail)


def ollama_models() -> list[str] | None:
    """Model names Ollama reports, [] if up but empty, None if unreachable."""
    try:
        with urllib.request.urlopen(OLLAMA_URL, timeout=3) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return [m["name"] for m in data.get("models", []) if m.get("name")]


# --------------------------------------------------------------------- report

def summarise(info: dict) -> list[str]:
    warnings: list[str] = []

    print("")
    print("--- summary -------------------------------------------------")
    print("PASS  python %s" % info["python"])
    print("      prefix %s" % info["prefix"])

    if info["cuda"]:
        print("PASS  ctranslate2 sees a CUDA device")
    else:
        print("WARN  no CUDA device visible to ctranslate2 -- transcription will run on CPU")
        warnings.append("cuda")

    if info["inputs"] > 0:
        print("PASS  %d audio input device(s)" % info["inputs"])
    else:
        print("WARN  no audio input devices found -- check the mic and Windows privacy settings")
        warnings.append("audio")

    for note in info["notes"]:
        print("      note: %s" % note)

    # Ollama is the user's own install; only report on it.
    names = ollama_models()
    if names is None:
        print("WARN  Ollama not answering on 127.0.0.1:11434 -- start it before using cleanup features")
        warnings.append("ollama")
    elif not names:
        print("WARN  Ollama is up but has no models pulled (try: ollama pull llama3.2)")
        warnings.append("ollama-models")
    else:
        print("PASS  Ollama is up with %d model(s): %s" % (len(names), ", ".join(names)))

    print("-------------------------------------------------------------")
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up and verify the SonicType conda env.")
    parser.add_argument("--env-name", default="sonictype")
    parser.add_argument("--verify-only", action="store_true",
                        help="skip conda create/update; only run the checks")
    parser.add_argument("--pause", action="store_true",
                        help="wait for Enter before exiting (used by setup.vbs, "
                             "whose console window would otherwise close on exit)")
    args = parser.parse_args()

    try:
        return _run(args)
    finally:
        if args.pause:
            try:
                input("\nPress Enter to close...")
            except (EOFError, KeyboardInterrupt):
                pass


def _run(args: argparse.Namespace) -> int:
    try:
        conda = resolve_conda()
        print("conda:   %s" % conda)
        print("project: %s" % PROJECT_ROOT)
        print("")

        if not args.verify_only:
            build_env(conda, args.env_name)

        env_python = resolve_env_python(conda, args.env_name, built=not args.verify_only)

        print("")
        print("Verifying...")
        warnings = summarise(verify(env_python))

        if not warnings:
            print("Setup complete. Start SonicType with:")
            print("    %s -m sonictype" % env_python)
        else:
            print("Setup complete with %d warning(s): %s"
                  % (len(warnings), ", ".join(warnings)))
            print("The app will still start. Try it with:")
            print("    %s -m sonictype" % env_python)
        return 0

    except SetupError as exc:
        print("")
        print("SETUP FAILED")
        for line in str(exc).splitlines():
            print("  %s" % line)
        print("")
        print("Nothing was left half-installed that a re-run cannot fix -- "
              "fix the cause above and run setup.py again.")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
