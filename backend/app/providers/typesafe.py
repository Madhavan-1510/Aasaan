"""Jev (TypeSafe): a DECISION model. It answers typed questions about a piece of state; it does not chat.

POST {base_url}/v1/systemone  with  Authorization: Bearer <key>
body {"state": ..., "model": "jev-latest", "questions": {id: {type, instructions, criteria}}}
response {"answers": {id: {"type": "noul", "noul": 0.95} | {"type": "choice", "choice", "probabilities",
"confidence"} | {"type": "score", "score", "legend", "probabilities", "confidence"}}}
Source: https://docs.typesafe.ai/api.md and /primitives.md. Several questions go in ONE call.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from .base import Provider, ProviderError, ProviderNotConfigured


def noul(instructions: str, true: str = "", false: str = "") -> dict:
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {"true": true, "false": false}
    return q


def choice(instructions: str, options: dict[str, str]) -> dict:
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions: str, levels: list[str]) -> dict:
    return {"type": "score", "instructions": instructions, "criteria": levels}


class TypeSafeClient:
    name = "typesafe"

    def __init__(self, api_key_env: str = "TYPESAFE_API_KEY", base_url: str = "https://api.typesafe.ai"):
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")

    def systemone(self, model_id: str, state: Any, questions: dict[str, dict], timeout_s: float = 15) -> dict:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ProviderNotConfigured(f"{self.api_key_env} is not set")
        body = {"state": state, "model": model_id, "questions": questions}
        try:
            r = httpx.post(f"{self.base_url}/v1/systemone", headers={"Authorization": f"Bearer {key}"},
                           json=body, timeout=timeout_s)
        except httpx.HTTPError as e:
            raise ProviderError(f"jev transport error: {type(e).__name__}: {e}") from e
        if r.status_code == 401:
            raise ProviderNotConfigured(f"jev rejected the API key (401): {r.text[:200]}")
        if r.status_code != 200:  # 422 bad request, 429 rate limit, 529 overloaded, ...
            raise ProviderError(f"jev HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["answers"]
        except (KeyError, ValueError) as e:
            raise ProviderError(f"jev unexpected response: {r.text[:300]}") from e


class JevTextStub(Provider):
    """Jev cannot generate text. If a chain ever sends it a text prompt, fail loudly and move on."""
    name = "typesafe"

    def generate(self, model_id, prompt, *, role="", context=None, images=None, timeout_s=30, json_mode=True):
        raise ProviderNotConfigured("Jev is a decision model; it is used through router.decide(), not text chains")
