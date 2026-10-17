"""OpenRouter: an extra Gemma 4 fallback (same family as the primary, different infrastructure).

POST https://openrouter.ai/api/v1/chat/completions, Authorization: Bearer <key>, body {model, messages, max_tokens, temperature},
text at choices[0].message.content (https://openrouter.ai/docs/api-reference/chat-completion). Model ids come from the public
GET /api/v1/models list (google/gemma-4-31b-it, google/gemma-4-26b-a4b-it). Image input is not wired (shape not verified here),
so image requests are skipped and the router moves on.
"""
from __future__ import annotations

import os

import httpx

from .base import Provider, ProviderError, ProviderNotConfigured, ProviderTimeout

BASE = "https://openrouter.ai/api/v1"


class OpenRouterProvider(Provider):
    name = "openrouter"

    def __init__(self, api_key_env: str = "OPENROUTER_API_KEY"):
        self.api_key_env = api_key_env

    def generate(self, model_id, prompt, *, role="", context=None, images=None, timeout_s=30, json_mode=True, thinking_level=None):
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderNotConfigured(f"{self.api_key_env} is not set")
        if images:
            raise ProviderNotConfigured("OpenRouter image input is not wired; skipping")
        headers = {"Authorization": f"Bearer {key}", "X-Title": "Aasaan",
                   "HTTP-Referer": os.environ.get("AASAAN_CONTACT", "https://aasaan.local")}
        body = {"model": model_id, "messages": [{"role": "user", "content": prompt}], "max_tokens": 4096, "temperature": 0.3}
        try:
            r = httpx.post(f"{BASE}/chat/completions", headers=headers, json=body, timeout=timeout_s)
        except httpx.TimeoutException as e:
            raise ProviderTimeout(f"openrouter {model_id} timed out after {timeout_s}s") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"openrouter transport error: {type(e).__name__}: {str(e).replace(key, '***')}") from e
        if r.status_code != 200:
            raise ProviderError(f"openrouter HTTP {r.status_code} for {model_id}: {' '.join(r.text.split())[:300]}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise ProviderError(f"openrouter unexpected response: {r.text[:300]}") from e
