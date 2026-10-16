"""Model router (architecture/03-model-routing).

Agents ask for a ROLE. The router reads config/models.yaml, walks that role's fallback chain, validates
every output, records one event per attempt WITH THE ERROR, and runs a non-model last resort when every
model fails. Nothing falls back silently: each fallback is logged (server log + `events` table) with its reason,
and `self_test()` (GET /api/health/models) calls every configured model once so a bad id shows up immediately.

Failure = timeout, rate limit, malformed JSON, failed schema/rule check, or missing key.
Jev is a decision model, not a chat model: it is used through `decide()`, never in text chains.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from pydantic import ValidationError

from .db import Store
from .providers.base import Provider, ProviderError, ProviderNotConfigured
from .providers.gemini import GeminiProvider
from .providers.mock import MockProvider
from .providers.ollama import OllamaProvider
from .providers.sarvam import SarvamClient
from .providers.sarvam_chat import SarvamChatProvider
from .providers.typesafe import TypeSafeClient, noul

log = logging.getLogger("aasaan.router")

# Chain steps implemented in code, never calling a model (models.yaml footer).
NON_MODEL_STEPS = {
    "text_only", "linear_path", "cached_lesson", "rule_checks", "cached_questions",
    "exact_match", "insert_review_step", "template_message", "text_only_mode", "no_audio",
    "template_answer", "search_order",
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
        # mode: "mock" = every chat model is served by the offline MockProvider (tests, UI work).
        self.mode = mode or os.environ.get("AASAAN_MODE") or ("live" if os.environ.get("GEMINI_API_KEY") else "mock")
        self.killed: set[str] = set()  # model names forced to fail (demo: "kill the primary")
        self.mock = MockProvider()
        p = self.cfg.get("providers", {})
        self.providers: dict[str, Provider] = {
            "gemini_api": GeminiProvider(p.get("gemini_api", {}).get("api_key_env", "GEMINI_API_KEY")),
            "sarvam": SarvamChatProvider(p.get("sarvam", {}).get("api_key_env", "SARVAM_API_KEY")),
            "ollama": OllamaProvider(p.get("ollama", {}).get("base_url", "http://localhost:11434")),
        }
        self.sarvam = SarvamClient(p.get("sarvam", {}).get("api_key_env", "SARVAM_API_KEY"),
                                   p.get("sarvam", {}).get("tts_speaker", ""))
        ts = p.get("typesafe", {})
        self.jev = TypeSafeClient(ts.get("api_key_env", "TYPESAFE_API_KEY"), ts.get("base_url", "https://api.typesafe.ai"))
        self.defaults = self.cfg.get("defaults", {})
        self._health_cache: Optional[tuple[float, dict]] = None
        # global limit on concurrent model calls (rate limits); AASAAN_MAX_CALLS overrides the config
        limit = int(os.environ.get("AASAAN_MAX_CALLS") or self.defaults.get("max_concurrent_calls", 4))
        self._sem = threading.BoundedSemaphore(max(1, limit))

    # ------------------------------------------------------------ admin hooks
    def kill(self, model: str) -> None:
        self.killed.add(model)

    def revive(self, model: Optional[str] = None) -> None:
        if model:
            self.killed.discard(model)
        else:
            self.killed.clear()

    # ----------------------------------------------------------------- events
    def _event(self, run_id: str, agent: str, model: str, step: int, status: str, latency_ms: int,
               validation: str, summary: str, error: str = "") -> None:
        ev = {
            "run_id": run_id, "ts": datetime.now(timezone.utc).isoformat(), "agent": agent,
            "model_used": model, "fallback_step": step, "status": status,
            "latency_ms": latency_ms, "validation": validation, "summary": summary[:300], "error": error[:600],
        }
        if status in ("retry", "fallback", "failed"):  # never silent: say WHY in the server log
            log.warning("[aasaan] %s step %d (%s) -> %s: %s", agent, step, model, status, error or summary)
        if self.store and self.defaults.get("log_events", True):
            self.store.log_event(ev)

    def _cache_key(self, model_id: str, prompt: str, images) -> Optional[str]:
        """Every live model call is cached by input hash, so retries and repeated runs are cheap."""
        if self.mode != "live" or not self.store or not self.defaults.get("cache_calls", True):
            return None
        h = hashlib.sha256()
        h.update(f"{model_id}\n{prompt}".encode())
        for mime, data in images or []:
            h.update(mime.encode() + hashlib.sha256(data).digest())
        return "llm:" + h.hexdigest()

    def note(self, agent: str, status: str, summary: str, error: str = "") -> None:
        """Record a non-call event (for example a chapter shipped unverified) with its reason."""
        self._event(str(uuid.uuid4()), agent, "none", 0, status, 0, "pass", summary, error)

    # ------------------------------------------------------------------ roles
    def role_name(self, role: str, language: Optional[str]) -> str:
        """Use the Indian-language chain (e.g. teacher_indic) when the student's language is not English."""
        if language and language != "en" and f"{role}_indic" in self.cfg["roles"]:
            return f"{role}_indic"
        return role

    def chain_for(self, role: str) -> list[str]:
        return list(self.cfg["roles"][role]["chain"])

    def _kind(self, model_name: str) -> str:
        return self.cfg["models"][model_name].get("kind", "chat")

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
        language: Optional[str] = None,
    ) -> RouterResult:
        chain_role = self.role_name(role, language)
        role_cfg = self.cfg["roles"][chain_role]
        timeout = role_cfg.get("timeout_s", self.defaults.get("timeout_s", 30))
        max_retries = self.defaults.get("max_retries_per_model", 1)
        run_id = str(uuid.uuid4())
        label = summary or role

        # the first CHAT model is the primary (a leading decision model such as Jev is handled by decide())
        primary_idx = next((i for i, st in enumerate(role_cfg["chain"]) if st not in NON_MODEL_STEPS and self._kind(st) == "chat"), 0)
        for step_idx, step in enumerate(role_cfg["chain"]):
            if step in NON_MODEL_STEPS:
                if last_resort is None:
                    continue
                t0 = time.perf_counter()
                try:
                    value = last_resort(context)
                except Exception as e:  # last resort must not take the app down
                    self._event(run_id, role, step, step_idx, "failed", 0, "fail", label, f"last resort {step} raised: {e}")
                    raise AllFailed(f"{role}: last resort {step} failed: {e}") from e
                self._event(run_id, role, step, step_idx, "fallback", int((time.perf_counter() - t0) * 1000), "pass",
                            f"{label} (code-only last resort)", "all models in the chain failed; see earlier events for this run")
                return RouterResult(value, step, step_idx, used_last_resort=True)

            if self._kind(step) != "chat":
                continue  # decision models (Jev) run through decide(); speech models through service()

            if step in self.killed:
                self._event(run_id, role, step, step_idx, "fallback", 0, "fail", label, "model killed (admin switch)")
                continue

            provider, model_id = self._provider_for(step)
            cur_prompt = prompt
            for attempt in range(max_retries + 1):
                last_try = attempt == max_retries
                t0 = time.perf_counter()
                cache_key = self._cache_key(model_id, cur_prompt, images)
                cached = self.store.cache_get(cache_key) if cache_key else None
                try:
                    if cached is not None:
                        raw = cached
                    else:
                        with self._sem:
                            raw = provider.generate(model_id, cur_prompt, role=role, context=context, images=images,
                                                    timeout_s=timeout)
                except ProviderNotConfigured as e:
                    self._event(run_id, role, step, step_idx, "fallback", 0, "fail", label, str(e))
                    break
                except ProviderError as e:
                    ms = int((time.perf_counter() - t0) * 1000)
                    self._event(run_id, role, step, step_idx, "fallback" if last_try else "retry", ms, "fail", label, str(e))
                    continue
                ms = int((time.perf_counter() - t0) * 1000)
                try:
                    value = validator(parse_json(raw))
                except (ValueError, ValidationError, TypeError, KeyError) as e:  # JSONDecodeError is a ValueError
                    self._event(run_id, role, step, step_idx, "fallback" if last_try else "retry", ms, "fail", label,
                                f"invalid output: {str(e)[:300]}")
                    # one repair retry on the same model, telling it what was wrong
                    cur_prompt = (f"{prompt}\n\nYour previous reply was invalid: {str(e)[:300]}\n"
                                  "Reply again with ONLY valid JSON matching the schema.")
                    continue
                if cache_key and cached is None:  # cache only replies that passed validation
                    self.store.cache_set(cache_key, raw)
                self._event(run_id, role, step, step_idx, "ok" if step_idx == primary_idx else "fallback", ms, "pass",
                            label + (" (cached)" if cached is not None else ""),
                            "" if step_idx == primary_idx else "served by a fallback model")
                return RouterResult(value, step, step_idx)

        self._event(run_id, role, "none", len(role_cfg["chain"]), "failed", 0, "fail", label, "all steps failed")
        raise AllFailed(f"{role}: every step in the chain failed")

    # ----------------------------------------------------------- decision model
    def decide(self, role: str, state: Any, questions: dict[str, dict], summary: str = "") -> Optional[dict]:
        """Ask Jev several typed questions in ONE call. Returns {question_id: answer} or None when Jev is
        unavailable (no key, error, mock mode); the caller then continues with the Gemma chain. Always logged."""
        jev_step = next((s for s in self.cfg["roles"][role]["chain"] if s not in NON_MODEL_STEPS
                         and self._kind(s) == "decision"), None)
        if jev_step is None or self.mode == "mock":
            return None
        run_id, label = str(uuid.uuid4()), summary or role
        if jev_step in self.killed:
            self._event(run_id, role, jev_step, 0, "fallback", 0, "fail", label, "model killed (admin switch)")
            return None
        timeout = self.cfg["roles"][role].get("timeout_s", self.defaults.get("timeout_s", 30))
        t0 = time.perf_counter()
        try:
            answers = self.jev.systemone(self.cfg["models"][jev_step]["id"], state, questions, timeout)
        except ProviderError as e:
            self._event(run_id, role, jev_step, 0, "fallback", int((time.perf_counter() - t0) * 1000), "fail", label, str(e))
            return None
        self._event(run_id, role, jev_step, 0, "ok", int((time.perf_counter() - t0) * 1000), "pass", label)
        return answers

    # --------------------------------------------------------------- services
    def service(self, name: str, runner: Callable[[str, dict], Any]) -> Any:
        """Run a voice/translation service. `runner(step_name, model_cfg)` does the work for a model step and
        may raise ProviderError. Returns None if only non-model steps remain (caller degrades to text-only /
        no audio). Every failed step is logged with its error."""
        run_id = str(uuid.uuid4())
        for idx, step in enumerate(self.cfg["services"][name]["chain"]):
            if step in NON_MODEL_STEPS:
                self._event(run_id, name, step, idx, "fallback", 0, "pass", f"{name}: degraded to {step}",
                            "every speech/translation model in the chain failed or is not configured")
                return None
            if step in self.killed:
                self._event(run_id, name, step, idx, "fallback", 0, "fail", name, "model killed (admin switch)")
                continue
            m = self.cfg["models"][step]
            if self.mode == "mock" and m["provider"] != "gemini_api":
                self._event(run_id, name, step, idx, "fallback", 0, "fail", name, "mock mode: speech models unavailable")
                continue
            t0 = time.perf_counter()
            try:
                out = runner(step, m)
            except ProviderError as e:
                self._event(run_id, name, step, idx, "fallback", int((time.perf_counter() - t0) * 1000), "fail", name, str(e))
                continue
            self._event(run_id, name, step, idx, "ok" if idx == 0 else "fallback",
                        int((time.perf_counter() - t0) * 1000), "pass", name)
            return out
        self._event(run_id, name, "none", 99, "failed", 0, "fail", name, "all steps failed")
        return None

    # -------------------------------------------------------------- self test
    def self_test(self, max_age_s: float = 60.0, force: bool = False) -> dict:
        """Call every configured model once and report ok / failed with the error (GET /api/health/models)."""
        if not force and self._health_cache and time.time() - self._health_cache[0] < max_age_s:
            return self._health_cache[1]
        models = self.cfg["models"]

        def check(name: str) -> tuple[str, dict]:
            m = models[name]
            kind = m.get("kind", "chat")
            base = {"provider": m["provider"], "id": m["id"], "kind": kind, "optional": bool(m.get("optional"))}
            if self.mode == "mock":
                return name, {**base, "status": "mock", "note": "mock mode: models are not called"}
            t0 = time.perf_counter()
            try:
                if kind == "chat":
                    out = self.providers[m["provider"]].generate(m["id"], "Reply with the single word: OK", role="health",
                                                                 timeout_s=25, json_mode=False)
                    if not out.strip():
                        raise ProviderError("empty reply")
                elif kind == "decision":
                    self.jev.systemone(m["id"], "ping", {"alive": noul("Is this a short test message?")}, timeout_s=20)
                elif kind == "tts":
                    if not self.sarvam.tts(m["id"], "Hello", "en", timeout_s=25):
                        raise ProviderError("empty audio")
                elif kind == "translate":
                    if not self.sarvam.translate(m["id"], "Hello", "en", "hi", timeout_s=25):
                        raise ProviderError("empty translation")
                elif kind == "stt":
                    return name, {**base, "status": "skipped", "note": "needs an audio clip; exercised on first voice use"}
                return name, {**base, "status": "ok", "latency_ms": int((time.perf_counter() - t0) * 1000)}
            except Exception as e:
                return name, {**base, "status": "failed", "error": str(e)[:400],
                              "latency_ms": int((time.perf_counter() - t0) * 1000)}

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = dict(pool.map(check, list(models)))
        failed = [n for n, r in results.items() if r["status"] == "failed" and not r["optional"]]
        features = {}
        if self.mode != "mock":  # Google Search grounding with Gemma (used by the Curator for discovery)
            t0 = time.perf_counter()
            try:
                hits = self.providers["gemini_api"].grounded_search(models["gemma_26b"]["id"], "Find one page about binary search", 25)
                features["grounded_search"] = {"status": "ok" if hits else "failed", "results": len(hits),
                                               "error": "" if hits else "no grounding results returned",
                                               "latency_ms": int((time.perf_counter() - t0) * 1000)}
            except Exception as e:
                features["grounded_search"] = {"status": "failed", "error": str(e)[:300]}
        report = {"mode": self.mode, "ok": not failed, "failed": failed, "models": results, "features": features}
        self._health_cache = (time.time(), report)
        return report

    def log_self_test(self) -> dict:
        """Run the self test and write one clear line per model to the server log (called at startup)."""
        rep = self.self_test(force=True)
        if rep["mode"] == "mock":
            log.warning("[aasaan] MOCK MODE: no real model is being called (set GEMINI_API_KEY for live mode)")
            return rep
        for name, r in rep.get("features", {}).items():
            (log.info if r["status"] == "ok" else log.warning)("[aasaan] feature %-14s %s %s", name, r["status"], r.get("error", ""))
        for name, r in rep["models"].items():
            if r["status"] == "ok":
                log.info("[aasaan] model %-14s ok      %s (%d ms)", name, r["id"], r["latency_ms"])
            elif r["status"] == "failed":
                (log.warning if r["optional"] else log.error)("[aasaan] model %-14s FAILED  %s: %s", name, r["id"], r["error"])
            else:
                log.info("[aasaan] model %-14s %s  %s", name, r["status"], r.get("note", ""))
        return rep
