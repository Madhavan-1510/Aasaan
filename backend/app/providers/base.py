"""Provider interface. A provider turns (model id, prompt) into raw text."""
from __future__ import annotations

from typing import Any, Optional


class ProviderError(Exception):
    """Timeout, rate limit, network or server failure. Router retries, then moves on."""


class ProviderNotConfigured(ProviderError):
    """Missing key or unsupported call. Never retried; router moves to the next step."""


class Provider:
    name = "base"

    def generate(
        self,
        model_id: str,
        prompt: str,
        *,
        role: str = "",
        context: Optional[dict[str, Any]] = None,
        images: Optional[list[tuple[str, bytes]]] = None,  # (mime, bytes)
        timeout_s: float = 30,
        json_mode: bool = True,
    ) -> str:
        raise NotImplementedError
