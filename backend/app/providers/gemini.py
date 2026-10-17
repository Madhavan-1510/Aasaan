"""Gemma 4 through the Gemini API (Google AI Studio key).

Only gemma-4-26b-a4b-it and gemma-4-31b-it are served here (confirmed in Google's Gemma docs).
Both accept images and system instructions. JSON mode (responseMimeType) is requested but is not
confirmed for Gemma, so a 400 about it is logged and the call is retried once without it.
"""
from __future__ import annotations

import base64
import logging
import os

import httpx

from .base import Provider, ProviderError, ProviderNotConfigured, ProviderTimeout

log = logging.getLogger("aasaan.gemini")
BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider(Provider):
    name = "gemini_api"

    def __init__(self, api_key_env: str = "GEMINI_API_KEY"):
        self.api_key_env = api_key_env
        self.json_mode_supported = True  # flips to False the first time the API rejects it
        self.thinking_supported = True   # same for thinkingConfig

    def _post(self, model_id: str, key: str, body: dict, timeout_s: float) -> httpx.Response:
        try:
            return httpx.post(f"{BASE}/{model_id}:generateContent", params={"key": key}, json=body, timeout=timeout_s)
        except httpx.TimeoutException as e:
            raise ProviderTimeout(f"gemini {model_id} timed out after {timeout_s}s ({type(e).__name__})") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"gemini transport error: {type(e).__name__}: {str(e).replace(key, '***')}") from e

    def generate(self, model_id, prompt, *, role="", context=None, images=None, timeout_s=30, json_mode=True, thinking_level=None):
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderNotConfigured(f"{self.api_key_env} is not set")
        parts: list[dict] = [{"text": prompt}]
        for mime, data in images or []:
            parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})
        gen_cfg: dict = {"temperature": 0.3}
        want_json = json_mode and self.json_mode_supported
        if want_json:
            gen_cfg["responseMimeType"] = "application/json"
        want_think = bool(thinking_level) and self.thinking_supported
        if want_think:  # Gemma 4 "thinks" first; "minimal" is much faster and enough for structured writing tasks
            gen_cfg["thinkingConfig"] = {"thinkingLevel": thinking_level}
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": gen_cfg}
        r = self._post(model_id, key, body, timeout_s)
        if r.status_code == 400 and want_json and ("mime" in r.text.lower() or "json" in r.text.lower()):
            log.warning("[aasaan] %s rejected JSON mode (%s); retrying without it", model_id, r.text[:160])
            self.json_mode_supported = False
            gen_cfg.pop("responseMimeType", None)
            r = self._post(model_id, key, body, timeout_s)
        if r.status_code == 400 and want_think and "think" in r.text.lower():
            log.warning("[aasaan] %s rejected thinkingConfig (%s); retrying without it", model_id, r.text[:160])
            self.thinking_supported = False
            gen_cfg.pop("thinkingConfig", None)
            r = self._post(model_id, key, body, timeout_s)
        if r.status_code != 200:
            raise ProviderError(f"gemini HTTP {r.status_code} for {model_id}: {' '.join(r.text.split())[:300]}")
        try:
            data = r.json()
            # Gemma 4 returns its reasoning as separate parts flagged `thought`: never mix them into the answer
            return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"] if not p.get("thought"))
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError(f"gemini unexpected response for {model_id}: {r.text[:300]}") from e

    def grounded_search(self, model_id: str, prompt: str, timeout_s: float = 30) -> list[dict]:
        """Gemini API Google Search grounding (`tools: [{"google_search": {}}]`, shown in Google's Gemma docs).
        Returns [{"uri", "title"}] from groundingMetadata.groundingChunks. URIs are redirect links and are
        untrusted candidates: the caller must verify them."""
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderNotConfigured(f"{self.api_key_env} is not set")
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0.3, "thinkingConfig": {"thinkingLevel": "minimal"}}}  # default thinking returned 0 results live
        r = self._post(model_id, key, body, timeout_s)
        if r.status_code != 200:
            raise ProviderError(f"gemini grounded search HTTP {r.status_code} for {model_id}: {r.text[:300]}")
        try:
            meta = r.json()["candidates"][0].get("groundingMetadata", {})
        except (KeyError, IndexError, ValueError) as e:
            raise ProviderError(f"gemini grounded search unexpected response: {r.text[:300]}") from e
        return [{"uri": c["web"]["uri"], "title": c["web"].get("title", "")}
                for c in meta.get("groundingChunks", []) if c.get("web", {}).get("uri")]
