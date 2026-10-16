"""Gemma 4 through the Gemini API (Google AI Studio key). Not exercised until a key is set.

Endpoint shape follows the public generateContent REST API.
TODO (before demo): confirm the Gemma 4 model ids in AI Studio and that image input works.
"""
from __future__ import annotations

import base64
import os

import httpx

from .base import Provider, ProviderError, ProviderNotConfigured

BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider(Provider):
    name = "gemini_api"

    def __init__(self, api_key_env: str = "GEMINI_API_KEY"):
        self.api_key_env = api_key_env

    def generate(self, model_id, prompt, *, role="", context=None, images=None, timeout_s=30):
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderNotConfigured(f"{self.api_key_env} not set")
        parts: list[dict] = [{"text": prompt}]
        for mime, data in images or []:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0.3}}
        try:
            r = httpx.post(f"{BASE}/{model_id}:generateContent", params={"key": key}, json=body, timeout=timeout_s)
        except httpx.HTTPError as e:  # timeouts, connection errors
            raise ProviderError(f"gemini transport error: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"gemini HTTP {r.status_code}: {r.text[:200]}")
        try:
            cands = r.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in cands)
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError(f"gemini unexpected response: {r.text[:200]}") from e
