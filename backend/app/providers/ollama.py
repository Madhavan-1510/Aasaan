"""Local Gemma 4 via Ollama (F13, optional offline mode)."""
from __future__ import annotations

import base64

import httpx

from .base import Provider, ProviderError, ProviderTimeout


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url.rstrip("/")

    def generate(self, model_id, prompt, *, role="", context=None, images=None, timeout_s=30, json_mode=True, thinking_level=None):
        body = {"model": model_id, "prompt": prompt, "stream": False}
        if json_mode:
            body["format"] = "json"
        if images:
            body["images"] = [base64.b64encode(d).decode() for _, d in images]
        try:
            r = httpx.post(f"{self.base_url}/api/generate", json=body, timeout=timeout_s)
        except httpx.TimeoutException as e:
            raise ProviderTimeout(f"timed out after {timeout_s}s: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"ollama transport error: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"ollama HTTP {r.status_code}: {r.text[:200]}")
        return r.json().get("response", "")
