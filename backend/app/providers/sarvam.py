"""Sarvam AI voice services: speech to text, text to speech, translation.

Endpoint paths follow Sarvam's public REST docs as best known. NOT verified.
TODO (before demo): confirm paths, field names, language codes and model ids.
These are services behind the router, not agents (PRD section 8, decision 4).
"""
from __future__ import annotations

import base64
import os

import httpx

from .base import ProviderError, ProviderNotConfigured

BASE = "https://api.sarvam.ai"
LANG = {"en": "en-IN", "hi": "hi-IN", "kn": "kn-IN", "ta": "ta-IN"}


class SarvamClient:
    name = "sarvam"

    def __init__(self, api_key_env: str = "SARVAM_API_KEY"):
        self.api_key_env = api_key_env

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderNotConfigured(f"{self.api_key_env} not set")
        return key

    def _post(self, path: str, timeout_s: float, **kw) -> dict:
        try:
            r = httpx.post(f"{BASE}{path}", headers={"api-subscription-key": self._key()}, timeout=timeout_s, **kw)
        except httpx.HTTPError as e:
            raise ProviderError(f"sarvam transport error: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"sarvam HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def stt(self, model_id: str, audio: bytes, language: str, timeout_s: float = 30) -> str:
        data = {"language_code": LANG.get(language, "en-IN")}
        if model_id and model_id != "TODO":
            data["model"] = model_id
        out = self._post("/speech-to-text", timeout_s, data=data, files={"file": ("audio.wav", audio, "audio/wav")})
        return out.get("transcript", "")

    def tts(self, model_id: str, text: str, language: str, timeout_s: float = 30) -> bytes:
        body = {"inputs": [text[:500]], "target_language_code": LANG.get(language, "en-IN")}
        if model_id and model_id != "TODO":
            body["model"] = model_id
        out = self._post("/text-to-speech", timeout_s, json=body)
        return base64.b64decode(out["audios"][0])

    def translate(self, model_id: str, text: str, source: str, target: str, timeout_s: float = 30) -> str:
        body = {"input": text, "source_language_code": LANG.get(source, "en-IN"),
                "target_language_code": LANG.get(target, "en-IN")}
        out = self._post("/translate", timeout_s, json=body)
        return out.get("translated_text", "")
