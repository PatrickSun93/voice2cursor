"""Ollama HTTP client for the optional polish step.

Every call degrades gracefully: if the server is down the app keeps working and
the raw Whisper transcript is used as-is.
"""
from __future__ import annotations

import requests

from .config import Config


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def base(self) -> str:
        return self.cfg.ollama_url.rstrip("/")

    def is_up(self, timeout: float = 1.5) -> bool:
        try:
            requests.get(f"{self.base}/api/tags", timeout=timeout).raise_for_status()
            return True
        except requests.RequestException:
            return False

    def _models_by_size(self, timeout: float = 4.0) -> list[tuple[str, int]]:
        """(name, size_bytes) for installed models, smallest first."""
        try:
            resp = requests.get(f"{self.base}/api/tags", timeout=timeout)
            resp.raise_for_status()
            models = resp.json().get("models", [])
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return []
        pairs = [(m["name"], int(m.get("size") or 0)) for m in models if m.get("name")]
        return sorted(pairs, key=lambda p: (p[1], p[0]))

    def list_models(self, timeout: float = 4.0) -> list[str]:
        """Installed model names, or [] if Ollama is unreachable."""
        return [name for name, _ in self._models_by_size(timeout)]

    def resolve_model(self) -> str:
        """Configured model if installed, else the smallest one available.

        Smallest, not first: punctuating a couple of sentences does not need a
        26B model, and a model larger than VRAM spills onto the CPU and turns a
        0.2 s polish into a minute. The tray menu overrides this.
        """
        available = self._models_by_size()
        if not available:
            raise OllamaError(
                f"No models found at {self.base}. Is `ollama serve` running, "
                "and have you pulled a model?"
            )
        names = [name for name, _ in available]
        if self.cfg.ollama_model in names:
            return self.cfg.ollama_model
        return names[0]

    def polish(self, text: str, timeout: float = 180.0) -> str:
        """Run the polish prompt over `text` and return the cleaned result."""
        if not text.strip():
            return ""
        model = self.resolve_model()
        prompt = self.cfg.polish_prompt.format(text=text)
        try:
            resp = requests.post(
                f"{self.base}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": self.cfg.ollama_keep_alive,
                    "options": {"temperature": 0.2},
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            out = resp.json().get("response", "")
        except requests.RequestException as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc
        except ValueError as exc:
            raise OllamaError("Ollama returned a malformed response") from exc
        return _strip_wrapper(out)


def _strip_wrapper(text: str) -> str:
    """Small models like to wrap replies in fences or quotes; drop those."""
    out = text.strip()
    if out.startswith("```"):
        lines = out.splitlines()
        lines = lines[1:] if len(lines) > 1 else lines
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        out = "\n".join(lines).strip()
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'":
        out = out[1:-1].strip()
    return out
