"""Model router (PRD section 8, decision 1; architecture/03-model-routing).

Agents ask for a ROLE. The router reads config/models.yaml, walks that role's
fallback chain, validates every output, logs one event per attempt, and runs a
non-model last resort when every model fails.

Failure = timeout, rate limit, malformed JSON, or failed schema/rule check.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from pydantic import ValidationError

from .db import Store
from .providers.base import Provider, ProviderError, ProviderNotConfigured
from .providers.gemini import GeminiProvider
from .providers.jev import JevProvider
from .providers.mock import MockProvider
from .providers.ollama import OllamaProvider
from .providers.sarvam import SarvamClient

# Chain steps implemented in code, never calling a model (models.yaml footer).
NON_MODEL_STEPS = {
    "text_only", "linear_path", "cached_lesson", "rule_checks", "cached_questions",
    "exact_match", "insert_review_step", "template_message", "text_only_mode", "no_audio",
    "template_answer",
}

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


class AllFailed(Exception):
    """Every step in the chain failed (including the last resort, if any)."""


@dataclass
class RouterResult:
    value: Any
    model_used: str
    fallback_step: int
    used_last_resort: bool = False


def parse_json(raw: str) -> Any:
    """Tolerant JSON extraction: strips code fences and surrounding prose."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


class ModelRouter:
    def __init__(
        self,
        config_path: str | Path = DEFAULT_CONFIG,
        store: Optional[Store] = None,
        mode: Optional[str] = None,
    ):
        with open(config_path, "r", encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)
        self.store = store
        # mode: "mock" = every model step is served by the offline MockProvider.
        self.mode = mode or os.environ.get("SWARM_MODE") or ("live" if os.environ.get("GEMINI_API_KEY") else "mock")
        self.killed: set[str] = set()  # model names forced to fail (demo: "kill the primary")
        self.mock = MockProvider()
        p = self.cfg.get("providers", {})
        self.providers: dict[str, Provider] = {
            "gemini_api": GeminiProvider(p.get("gemini_api", {}).get("api_key_env", "GEMINI_API_KEY")),
            "ollama": OllamaProvider(p.get("ollama", {}).get("base_url", "http://localhost:11434")),
            "jev": JevProvider(p.get("jev", {}).get("api_key_env", "JEV_API_KEY")),
        }
        self.sarvam = SarvamClient(p.get("sarvam", {}).get("api_key_env", "SARVAM_API_KEY"))
        self.defaults = self.cfg.get("defaults", {})

    # ------------------------------------------------------------ admin hooks
    def kill(self, model: str) -> None:
        self.killed.add(model)

    def revive(self, model: Optional[str] = None) -> None:
        if model:
            self.killed.discard(model)
        else:
            self.killed.clear()

    # ----------------------------------------------------------------- events
    def _event(self, run_id: str, agent: str, model: str, step: int, status: str,
               latency_ms: int, validation: str, summary: str) -> None:
        ev = {
            "run_id": run_id, "ts": datetime.now(timezone.utc).isoformat(), "agent": agent,
            "model_used": model, "fallback_step": step, "status": status,
            "latency_ms": latency_ms, "validation": validation, "summary": summary[:300],
        }
        if self.store and self.defaults.get("log_events", True):
            self.store.log_event(ev)

    # ------------------------------------------------------------------ roles
    def chain_for(self, role: str) -> list[str]:
        return list(self.cfg["roles"][role]["chain"])

    def _provider_for(self, model_name: str) -> tuple[Provider, str]:
        m = self.cfg["models"][model_name]
        if self.mode == "mock":
            return self.mock, m["id"]
        return self.providers[m["provider"]], m["id"]

    def call(
        self,
        role: str,
        prompt: str,
        context: dict[str, Any],
        validator: Callable[[Any], Any],
        last_resort: Optional[Callable[[dict[str, Any]], Any]] = None,
        images: Optional[list[tuple[str, bytes]]] = None,
        summary: str = "",
    ) -> RouterResult:
        role_cfg = self.cfg["roles"][role]
        timeout = role_cfg.get("timeout_s", self.defaults.get("timeout_s", 30))
        max_retries = self.defaults.get("max_retries_per_model", 1)
        run_id = str(uuid.uuid4())
        label = summary or role

        for step_idx, step in enumerate(role_cfg["chain"]):
            if step in NON_MODEL_STEPS:
                if last_resort is None:
                    continue
                t0 = time.perf_counter()
                try:
                    value = last_resort(context)
                except Exception as e:  # last resort must not take the app down
                    self._event(run_id, role, step, step_idx, "failed", 0, "fail", f"{label}: {e}")
                    raise AllFailed(f"{role}: last resort {step} failed: {e}") from e
                self._event(run_id, role, step, step_idx, "fallback",
                            int((time.perf_counter() - t0) * 1000), "pass", f"{label} (last resort)")
                return RouterResult(value, step, step_idx, used_last_resort=True)

            if step in self.killed:
                self._event(run_id, role, step, step_idx, "fallback", 0, "fail", f"{label}: model killed")
                continue

            provider, model_id = self._provider_for(step)
            cur_prompt = prompt
            for attempt in range(max_retries + 1):
                last_try = attempt == max_retries
                t0 = time.perf_counter()
                try:
                    raw = provider.generate(model_id, cur_prompt, role=role, context=context,
                                            images=images, timeout_s=timeout)
                except ProviderNotConfigured as e:
                    self._event(run_id, role, step, step_idx, "fallback", 0, "fail", f"{label}: {e}")
                    break
                except ProviderError as e:
                    ms = int((time.perf_counter() - t0) * 1000)
                    self._event(run_id, role, step, step_idx, "fallback" if last_try else "retry", ms, "fail",
                                f"{label}: {e}")
                    continue
                ms = int((time.perf_counter() - t0) * 1000)
                try:
                    value = validator(parse_json(raw))
                except (ValueError, ValidationError, TypeError, KeyError) as e:  # JSONDecodeError is a ValueError
                    self._event(run_id, role, step, step_idx, "fallback" if last_try else "retry", ms, "fail",
                                f"{label}: invalid output: {str(e)[:120]}")
                    # one repair retry on the same model, telling it what was wrong
                    cur_prompt = (f"{prompt}\n\nYour previous reply was invalid: {str(e)[:300]}\n"
                                  "Reply again with ONLY valid JSON matching the schema.")
                    continue
                self._event(run_id, role, step, step_idx, "ok" if step_idx == 0 else "fallback", ms, "pass", label)
                return RouterResult(value, step, step_idx)

        self._event(run_id, role, "none", len(role_cfg["chain"]), "failed", 0, "fail", f"{label}: all steps failed")
        raise AllFailed(f"{role}: every step in the chain failed")

    # --------------------------------------------------------------- services
    def service(self, name: str, runner: Callable[[str, dict], Any], timeout_s: Optional[float] = None) -> Any:
        """Run a voice/translation service. `runner(step_name, model_cfg)` does the work for a
        model step and may raise ProviderError. Returns None if only non-model steps remain
        (caller degrades to text-only mode / no audio)."""
        run_id = str(uuid.uuid4())
        for idx, step in enumerate(self.cfg["services"][name]["chain"]):
            if step in NON_MODEL_STEPS:
                self._event(run_id, name, step, idx, "fallback", 0, "pass", f"{name}: degraded to {step}")
                return None
            if step in self.killed:
                continue
            m = self.cfg["models"][step]
            if self.mode == "mock" and m["provider"] != "gemini_api":
                self._event(run_id, name, step, idx, "fallback", 0, "fail", f"{name}: {step} unavailable in mock mode")
                continue
            t0 = time.perf_counter()
            try:
                out = runner(step, m)
            except ProviderError as e:
                self._event(run_id, name, step, idx, "fallback", int((time.perf_counter() - t0) * 1000), "fail",
                            f"{name}: {e}")
                continue
            self._event(run_id, name, step, idx, "ok" if idx == 0 else "fallback",
                        int((time.perf_counter() - t0) * 1000), "pass", name)
            return out
        self._event(run_id, name, "none", 99, "failed", 0, "fail", f"{name}: all steps failed")
        return None
