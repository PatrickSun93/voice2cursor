"""Put the pip-installed NVIDIA runtime DLLs on the loader path.

CTranslate2 links cuBLAS and cuDNN dynamically. The wheels drop them under
site-packages/nvidia/*/bin, which Windows does not search by default, so
importing faster_whisper without this yields a bare "Library cudnn64_9.dll is
not found" error and a silent fall back to CPU. Must run before the first
`import faster_whisper`.
"""
from __future__ import annotations

import os
import sys

_SUBPACKAGES = ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime")


def register_cuda_dlls() -> list[str]:
    """Return the directories successfully added to the DLL search path."""
    if not hasattr(os, "add_dll_directory"):  # non-Windows
        return []
    base = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
    added = []
    for sub in _SUBPACKAGES:
        path = os.path.join(base, sub, "bin")
        if os.path.isdir(path):
            try:
                os.add_dll_directory(path)
            except OSError:
                continue
            added.append(path)
    if added:
        # Some CTranslate2 builds resolve via PATH rather than the DLL directories.
        os.environ["PATH"] = os.pathsep.join(added) + os.pathsep + os.environ.get("PATH", "")
    return added


def cuda_available() -> bool:
    """True if CTranslate2 can see a usable CUDA device."""
    register_cuda_dlls()
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False
