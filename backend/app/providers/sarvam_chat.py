"""Sarvam chat models (sarvam-105b, gemma4) via POST /v2/chat/completions.

Request/response shape from https://docs.sarvam.ai/api/api-guides-tutorials/chat-completion/overview :
header `api-subscription-key`, body {model, messages:[{role, content}], max_tokens, temperature},
text at choices[0].message.content. The docs do not show the image-input shape, so image requests
are refused here (the router then moves to the next model) instead of guessing.
"""
from __future__ import annotations

import os

import httpx

from .base import Provider, ProviderError, ProviderNotConfigured, ProviderTimeout

BASE = "https://api.sarvam.ai"


class SarvamChatProvider(Provider):
    name = "sarvam"

    def __init__(self, api_key_env: str = "SARVAM_API_KEY"):
        self.api_key_env = api_key_env

    def generate(self, model_id, prompt, *, role="", context=None, images=None, timeout_s=30, json_mode=True, thinking_level=None):
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderNotConfigured(f"{self.api_key_env} is not set")
        if images:
            raise ProviderNotConfigured("Sarvam chat image input shape is not documented; skipping")
        body = {"model": model_id, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 4096, "temperature": 0.3}
        try:
            r = httpx.post(f"{BASE}/v2/chat/completions", headers={"api-subscription-key": key}, json=body,
                           timeout=timeout_s)
        except httpx.TimeoutException as e:
            raise ProviderTimeout(f"timed out after {timeout_s}s: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"sarvam transport error: {type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"sarvam chat HTTP {r.status_code} for {model_id}: {r.text[:300]}")
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise ProviderError(f"sarvam chat unexpected response: {r.text[:300]}") from e
