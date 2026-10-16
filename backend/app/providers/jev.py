"""Jev (optional fast-decision model). Access and API shape are unconfirmed.

Until the real API is known this provider reports "not configured", so the
Diagnoser chain falls straight through to gemma_small. Replace generate() once
hackathon docs are available.
"""
from __future__ import annotations

import os

from .base import Provider, ProviderNotConfigured


class JevProvider(Provider):
    name = "jev"

    def __init__(self, api_key_env: str = "JEV_API_KEY"):
        self.api_key_env = api_key_env

    def generate(self, model_id, prompt, *, role="", context=None, images=None, timeout_s=10):
        if not os.environ.get(self.api_key_env) or model_id in ("TODO", ""):
            raise ProviderNotConfigured("Jev not configured")
        raise ProviderNotConfigured("Jev API client not implemented yet (see TODO in providers/jev.py)")
