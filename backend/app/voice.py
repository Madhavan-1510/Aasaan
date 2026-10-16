"""Voice and translation services (PRD section 8, decision 4): a service behind the router, not an agent.

Chains live in config/models.yaml under `services`. When Sarvam is unavailable the
caller degrades to text-only mode / no audio instead of failing.
"""
from __future__ import annotations

from typing import Optional

from .router import ModelRouter


class Voice:
    def __init__(self, router: ModelRouter):
        self.router = router

    def stt(self, audio: bytes, language: str) -> Optional[str]:
        """Speech to text. None = unavailable -> text-only mode (student types instead)."""
        def run(step: str, m: dict):
            return self.router.sarvam.stt(m["id"], audio, language)
        return self.router.service("speech_to_text", run)

    def tts(self, text: str, language: str) -> Optional[bytes]:
        """Text to speech. None = no audio (the UI falls back to the browser voice or plain text)."""
        def run(step: str, m: dict):
            return self.router.sarvam.tts(m["id"], text, language)
        return self.router.service("text_to_speech", run)

    def translate(self, text: str, source: str, target: str) -> Optional[str]:
        """Sarvam first, then Gemma for translation. None if every step failed."""
        def run(step: str, m: dict):
            if m["provider"] == "sarvam":
                return self.router.sarvam.translate(m["id"], text, source, target)
            prompt = f"Translate from {source} to {target}. Reply with only the translation.\n\n{text}"
            return self.router.providers[m["provider"]].generate(m["id"], prompt, role="translate", json_mode=False).strip()
        return self.router.service("translate", run)
