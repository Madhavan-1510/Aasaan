"""Sarvam AI voice services: speech to text, text to speech, translation (REST).

Request shapes follow the official docs (checked 2026-10-16):
- STT  POST /speech-to-text  multipart: file, model (saaras:v4 | saaras:v3), mode (transcribe|translate|verbatim|translit|codemix).
       Language is auto-detected. Sync limit 30 s, 16 kHz mono preferred. Response: request_id, transcript, language_code.
- TTS  POST /text-to-speech  JSON: text (<=2500 chars), language_code, model (bulbul:v4-flash | bulbul:v3), speaker, pace,
       speech_sample_rate, output_audio_codec. Response: audios[] of base64 WAV.
- Translate  POST /translate  JSON: input, source_language_code, target_language_code, model (mayura:v1 | sarvam-translate:v1), mode.
Header for all: api-subscription-key. These are services behind the router, not agents.
"""
from __future__ import annotations

import base64
import os

import httpx

from .base import ProviderError, ProviderNotConfigured

BASE = "https://api.sarvam.ai"
LANG = {"en": "en-IN", "hi": "hi-IN", "kn": "kn-IN", "ta": "ta-IN", "te": "te-IN", "ml": "ml-IN", "bn": "bn-IN",
        "mr": "mr-IN", "gu": "gu-IN", "pa": "pa-IN", "od": "od-IN"}


class SarvamClient:
    name = "sarvam"

    def __init__(self, api_key_env: str = "SARVAM_API_KEY", tts_speaker: str = ""):
        self.api_key_env = api_key_env
        self.tts_speaker = tts_speaker

    def _key(self) -> str:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderNotConfigured(f"{self.api_key_env} is not set")
        return key

    def _post(self, path: str, timeout_s: float, **kw) -> dict:
        try:
            r = httpx.post(f"{BASE}{path}", headers={"api-subscription-key": self._key()}, timeout=timeout_s, **kw)
        except httpx.HTTPError as e:
            raise ProviderError(f"sarvam {path} transport error: {type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise ProviderError(f"sarvam {path} HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError as e:
            raise ProviderError(f"sarvam {path} returned non-JSON: {r.text[:200]}") from e

    def stt(self, model_id: str, audio: bytes, language: str = "", mode: str = "codemix", timeout_s: float = 30,
            filename: str = "audio.wav", content_type: str = "audio/wav") -> str:
        """Speech to text. `codemix` handles mixed speech such as Kannada plus English. Clips must be < 30 s."""
        out = self._post("/speech-to-text", timeout_s, data={"model": model_id, "mode": mode},
                         files={"file": (filename, audio, content_type)})
        return out.get("transcript", "") or ""

    def tts(self, model_id: str, text: str, language: str, timeout_s: float = 30) -> bytes:
        body = {"text": text[:2500], "language_code": LANG.get(language, "en-IN"), "model": model_id,
                "pace": 1.0, "speech_sample_rate": 24000}
        if self.tts_speaker:
            body["speaker"] = self.tts_speaker
        out = self._post("/text-to-speech", timeout_s, json=body)
        try:
            return base64.b64decode(out["audios"][0])
        except (KeyError, IndexError) as e:
            raise ProviderError(f"sarvam tts response had no audio: {str(out)[:200]}") from e

    def translate(self, model_id: str, text: str, source: str, target: str, timeout_s: float = 30) -> str:
        body = {"input": text, "source_language_code": LANG.get(source, "auto") if source else "auto",
                "target_language_code": LANG.get(target, "en-IN"), "model": model_id}
        out = self._post("/translate", timeout_s, json=body)
        return out.get("translated_text", "") or ""
