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
from .providers.base import Provider, ProviderError, ProviderNotConfigured, ProviderTimeout
from .providers.gemini import GeminiProvider
from .providers.mock import MockProvider
from .providers.ollama import OllamaProvider
from .providers.openrouter import OpenRouterProvider
from .providers.sarvam import SarvamClient
from .providers.sarvam_chat import SarvamChatProvider
from .providers.typesafe import TypeSafeClient, noul
from .resilience import CircuitBreaker, EventSink, scrub
from .tools import Chaos

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
    def load(t: str):
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            # models sometimes put raw newlines inside strings or leave trailing commas: both are safe to tolerate
            return json.loads(re.sub(r",\s*([}\]])", r"\1", t), strict=False)

    try:
        return load(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return load(text[start : end + 1])
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
        self.chaos = Chaos.from_env()  # AASAAN_CHAOS="model:jev=fail,tool:commons=fail" (shared with the ToolRunner)
        self.mock = MockProvider()
        p = self.cfg.get("providers", {})
        self.providers: dict[str, Provider] = {
            "gemini_api": GeminiProvider(p.get("gemini_api", {}).get("api_key_env", "GEMINI_API_KEY")),
            "sarvam": SarvamChatProvider(p.get("sarvam", {}).get("api_key_env", "SARVAM_API_KEY")),
            "ollama": OllamaProvider(p.get("ollama", {}).get("base_url", "http://localhost:11434")),
            "openrouter": OpenRouterProvider(p.get("openrouter", {}).get("api_key_env", "OPENROUTER_API_KEY")),
        }
        self.sarvam = SarvamClient(p.get("sarvam", {}).get("api_key_env", "SARVAM_API_KEY"),
                                   p.get("sarvam", {}).get("tts_speaker", ""))
        ts, orc = p.get("typesafe", {}), p.get("openrouter", {})
        # decision models (typed answers, no generated text). TypeSafe is preferred; the same Jev and Mercury Decide are also on OpenRouter.
        self.jev = TypeSafeClient(ts.get("api_key_env", "TYPESAFE_API_KEY"), ts.get("base_url", "https://api.typesafe.ai"), "/v1/systemone")
        self.or_decider = TypeSafeClient(orc.get("api_key_env", "OPENROUTER_API_KEY"), orc.get("base_url", "https://openrouter.ai/api/v1"), "/systemone")
        self.decision_clients = {"typesafe": self.jev, "openrouter": self.or_decider}
        self._skip_logged: set[str] = set()
        self.defaults = self.cfg.get("defaults", {})
        self._health_cache: Optional[tuple[float, dict]] = None
        # global limit on concurrent model calls (rate limits); AASAAN_MAX_CALLS overrides the config
        limit = int(os.environ.get("AASAAN_MAX_CALLS") or self.defaults.get("max_concurrent_calls", 4))
        self._sem = threading.BoundedSemaphore(max(1, limit))
        # circuit breaker: a model that keeps timing out or failing server-side is skipped for a cooldown, so one slow
        # model does not add its full timeout to every call (it is retried after the cooldown)
        self._breakers: dict[str, CircuitBreaker] = {}   # shared implementation: app/resilience.py
        self.breaker_threshold = int(self.defaults.get("circuit_threshold", 2))
        self.breaker_cooldown_s = float(self.defaults.get("circuit_cooldown_s", 90))
        self.slow_call_s = float(self.defaults.get("slow_call_s", 40))
        self.sink = EventSink(store, self.defaults.get("log_events", True))

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
        self.sink.emit(agent, model, step, status, latency_ms, validation, summary, error, run_id=run_id)

    def _cache_key(self, model_id: str, prompt: str, images) -> Optional[str]:
        """Every live model call is cached by input hash, so retries and repeated runs are cheap."""
        if self.mode != "live" or not self.store or not self.defaults.get("cache_calls", True):
            return None
        h = hashlib.sha256()
        h.update(f"{model_id}\n{prompt}".encode())
        for mime, data in images or []:
            h.update(mime.encode() + hashlib.sha256(data).digest())
        return "llm:" + h.hexdigest()

    def _breaker(self, model: str) -> CircuitBreaker:
        br = self._breakers.get(model)
        if br is None:
            br = self._breakers[model] = CircuitBreaker(self.breaker_threshold, self.breaker_cooldown_s, model)
        br.threshold, br.cooldown_s = self.breaker_threshold, self.breaker_cooldown_s  # picks up later tuning
        return br

    def circuit_open(self, model: str) -> float:
        """Seconds left before a tripped model is tried again (0 = closed)."""
        return self._breaker(model).open_remaining()

    def _record(self, model: str, ok: bool) -> None:
        br = self._breaker(model)
        if ok:
            br.record_success()
        elif br.record_failure():
            log.warning("[aasaan] circuit OPEN for %s after %d consecutive failures; skipping it for %.0fs", model, br.consecutive, br.cooldown_s)

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
            if self.chaos.mode_for("model", step):
                self._event(run_id, role, step, step_idx, "fallback", 0, "fail", label, f"chaos: forced {self.chaos.mode_for('model', step)} (AASAAN_CHAOS)")
                continue

            left = self.circuit_open(step)
            if left > 0:
                self._event(run_id, role, step, step_idx, "fallback", 0, "fail", label,
                            f"circuit open: {step} failed {self._breaker(step).consecutive} times in a row; skipped for another {left:.0f}s")
                continue
            provider, model_id = self._provider_for(step)
            thinking = self.cfg["models"][step].get("thinking_level")
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
                                                    timeout_s=timeout, thinking_level=thinking)
                except ProviderNotConfigured as e:
                    self._event(run_id, role, step, step_idx, "fallback", 0, "fail", label, str(e))
                    break
                except ProviderTimeout as e:  # retrying the same slow model would only stall again
                    ms = int((time.perf_counter() - t0) * 1000)
                    self._record(step, False)
                    self._event(run_id, role, step, step_idx, "fallback", ms, "fail", label, str(e))
                    break
                except ProviderError as e:
                    ms = int((time.perf_counter() - t0) * 1000)
                    self._record(step, False)
                    self._event(run_id, role, step, step_idx, "fallback" if last_try else "retry", ms, "fail", label, str(e))
                    if self.circuit_open(step) > 0:
                        break
                    continue
                ms = int((time.perf_counter() - t0) * 1000)
                if cached is None:
                    # a model that keeps answering far too slowly is demoted like one that fails (it still gets this answer used)
                    self._record(step, ms / 1000 <= self.slow_call_s)
                    if ms / 1000 > self.slow_call_s:
                        log.warning("[aasaan] %s answered in %.0fs (slow threshold %.0fs)", step, ms / 1000, self.slow_call_s)
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

    # ---------------------------------------------------------- decision models
    NOUL_UNSURE = (0.35, 0.65)   # a yes/no probability in this band means "not sure"

    def _usable_deciders(self, student_data: bool) -> list[str]:
        """The `decisions` chain minus entries whose key is missing and, for student data, minus free-tier models."""
        out = []
        for step in self.cfg["roles"]["decisions"]["chain"]:
            if step in NON_MODEL_STEPS or step not in self.cfg["models"]:
                continue
            mc = self.cfg["models"][step]
            if mc.get("kind", "chat") == "decision":
                env = self.cfg["providers"].get(mc["provider"], {}).get("api_key_env")
                if env and not os.environ.get(env):
                    if step not in self._skip_logged:  # said once, so a missing key is never invisible but never spams
                        self._skip_logged.add(step)
                        self._event(str(uuid.uuid4()), "decisions", step, 0, "fallback", 0, "fail", "decision model skipped", f"{env} is not set")
                    continue
                if student_data and mc.get("free_tier"):
                    continue  # never send student data to a free-tier model whose data handling is unconfirmed
            out.append(step)
        return out

    @classmethod
    def _is_unsure(cls, ans: dict, min_conf: float) -> bool:
        t = ans.get("type") or ("noul" if "noul" in ans else "choice" if "choice" in ans else "score")
        if t == "noul":
            try:
                return cls.NOUL_UNSURE[0] <= float(ans["noul"]) <= cls.NOUL_UNSURE[1]
            except (KeyError, TypeError, ValueError):
                return True
        conf = ans.get("confidence")
        return conf is not None and float(conf) < min_conf

    def _decide_with_chat(self, step: str, state: Any, questions: dict[str, dict], timeout: float) -> dict:
        """Last decision fallback: a Gemma model answers the same typed questions. Output is validated per question."""
        m = self.cfg["models"][step]
        provider, model_id = self._provider_for(step)
        prompt = ("You are a careful classifier. Read the state, then answer EVERY question. Question types: noul = probability 0 to 1 that the answer is "
                  "yes; choice = pick exactly one key of `criteria`; score = a number from 0 to (number of criteria - 1) on the ordered levels. "
                  'Reply with ONLY JSON: {"<id>": {"noul": 0.9} | {"choice": "<key>", "confidence": 0.8} | {"score": 1.5, "confidence": 0.7}}.\n\n'
                  f"State:\n{json.dumps(state, ensure_ascii=False)[:6000]}\n\nQuestions:\n{json.dumps(questions, ensure_ascii=False)}")
        with self._sem:
            raw = provider.generate(model_id, prompt, role="decide", timeout_s=timeout, thinking_level=m.get("thinking_level"))
        try:
            data = parse_json(raw)
        except ValueError as e:
            raise ProviderError(f"decision answer was not JSON: {e}") from e
        clamp = lambda v, lo=0.0, hi=1.0: max(lo, min(hi, float(v)))  # noqa: E731
        out: dict = {}
        for qid, q in questions.items():
            a = data.get(qid) if isinstance(data, dict) else None
            if not isinstance(a, dict):
                continue
            try:
                if q["type"] == "noul":
                    out[qid] = {"type": "noul", "noul": clamp(a["noul"])}
                elif q["type"] == "choice" and a["choice"] in q["criteria"]:
                    out[qid] = {"type": "choice", "choice": a["choice"], "confidence": clamp(a.get("confidence", 0.6))}
                elif q["type"] == "score":
                    out[qid] = {"type": "score", "score": clamp(a["score"], 0, len(q["criteria"]) - 1), "confidence": clamp(a.get("confidence", 0.6))}
            except (KeyError, TypeError, ValueError):
                continue
        if not out:
            raise ProviderError("decision model returned no usable answers")
        return out

    def decide(self, gate: str, state: Any, questions: dict[str, dict], *, student_data: bool = False, summary: str = "",
               min_confidence: float = 0.5) -> Optional[dict]:
        """Ask typed questions through the `decisions` chain (Jev, Jev via OpenRouter, Mercury Decide, then Gemma): ONE batched call per
        provider. A question the provider is unsure about (low confidence) is escalated to the next provider; if nobody is sure the best
        answer is kept and flagged `"unsure": true`. Returns {question_id: answer} (each tagged `_by`) or None when no provider could answer
        (the caller then uses its own rule). Every skip, failure and escalation is recorded in `events`. Mock mode returns None."""
        if self.mode == "mock" or not questions:
            return None
        steps = self._usable_deciders(student_data)
        timeout = self.cfg["roles"]["decisions"].get("timeout_s", 15)
        run_id, label = str(uuid.uuid4()), summary or gate
        remaining, answers, tentative = dict(questions), {}, {}
        for i, step in enumerate(steps):
            if not remaining:
                break
            if step in self.killed:
                self._event(run_id, gate, step, i, "fallback", 0, "fail", label, "model killed (admin switch)")
                continue
            if self.chaos.mode_for("model", step):
                self._event(run_id, gate, step, i, "fallback", 0, "fail", label, f"chaos: forced {self.chaos.mode_for('model', step)} (AASAAN_CHAOS)")
                continue
            left = self.circuit_open(step)
            if left > 0:
                self._event(run_id, gate, step, i, "fallback", 0, "fail", label, f"circuit open: {step} skipped for another {left:.0f}s")
                continue
            mc, t0 = self.cfg["models"][step], time.perf_counter()
            try:
                if mc.get("kind", "chat") == "decision":
                    got = self.decision_clients[mc["provider"]].systemone(mc["id"], state, remaining, timeout)
                else:
                    got = self._decide_with_chat(step, state, remaining, timeout)
            except ProviderNotConfigured as e:
                self._event(run_id, gate, step, i, "fallback", 0, "fail", label, str(e))
                continue
            except ProviderError as e:
                self._record(step, False)
                self._event(run_id, gate, step, i, "fallback", int((time.perf_counter() - t0) * 1000), "fail", label, str(e))
                continue
            self._record(step, True)
            has_next, unsure = i < len(steps) - 1, []
            for qid, ans in (got or {}).items():
                if qid not in remaining:
                    continue
                tagged = {**ans, "_by": step}
                if self._is_unsure(tagged, min_confidence):
                    if has_next:
                        tentative[qid] = tagged
                        unsure.append(qid)
                        continue
                    tagged["unsure"] = True  # the last provider is not sure either: keep its answer, but say so
                answers[qid] = tagged
                remaining.pop(qid)
            self._event(run_id, gate, step, i, "ok" if i == 0 else "fallback", int((time.perf_counter() - t0) * 1000), "pass", label,
                        "" if i == 0 else "answered by a fallback decision provider")
            if unsure and remaining:
                self._event(run_id, gate, step, i, "fallback", 0, "pass", label, f"unsure about {', '.join(unsure)[:120]}: escalating to the next provider")
        for qid in remaining:  # nobody was sure: keep the best answer we have, flagged
            if qid in tentative:
                answers[qid] = {**tentative[qid], "unsure": True}
        return answers or None

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
                                                                 timeout_s=100, json_mode=False, thinking_level=m.get("thinking_level"))
                    if not out.strip():
                        raise ProviderError("empty reply")
                elif kind == "decision":
                    self.decision_clients[m["provider"]].systemone(m["id"], "ping", {"alive": noul("Is this a short test message?")}, timeout_s=20)
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
                return name, {**base, "status": "failed", "error": scrub(e)[:400],
                              "latency_ms": int((time.perf_counter() - t0) * 1000)}

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = dict(pool.map(check, list(models)))
        failed = [n for n, r in results.items() if r["status"] == "failed" and not r["optional"]]
        features = {}
        if self.mode != "mock":  # Google Search grounding with Gemma (used by the Curator for discovery)
            t0 = time.perf_counter()
            try:
                hits = self.providers["gemini_api"].grounded_search(models["gemma_26b"]["id"], "Find one page about binary search", 80)
                features["grounded_search"] = {"status": "ok" if hits else "failed", "results": len(hits),
                                               "error": "" if hits else "no grounding results returned",
                                               "latency_ms": int((time.perf_counter() - t0) * 1000)}
            except Exception as e:
                features["grounded_search"] = {"status": "failed", "error": scrub(e)[:300]}
        report = {"mode": self.mode, "ok": not failed, "failed": failed, "models": results, "features": features,
                  "circuit_open": {m: round(self.circuit_open(m)) for m in models if self.circuit_open(m) > 0}}
        self._health_cache = (time.time(), report)
        return report

    def demote_slow_models(self, report: dict, cooldown_s: float = 300) -> list[str]:
        """After the startup self test: chat models that answered a trivial prompt slower than `slow_call_s` start with an open
        circuit, so the first real calls do not each wait out a full timeout on them. They are retried after the cooldown."""
        demoted = []
        for name, res in report.get("models", {}).items():
            err = (res.get("error") or "").lower()
            unhealthy = res["status"] == "failed" and ("timed out" in err or " http 5" in err or "http 503" in err)
            if res.get("kind") == "chat" and ((res["status"] == "ok" and res.get("latency_ms", 0) / 1000 > self.slow_call_s) or unhealthy):
                self._breaker(name).trip(cooldown_s)
                demoted.append(name)
                log.warning("[aasaan] %s is slow or unhealthy at startup (%.0fs, %s): skipping it for %.0fs", name, res.get("latency_ms", 0) / 1000,
                            res["status"], cooldown_s)
        return demoted

    def log_self_test(self) -> dict:
        """Run the self test and write one clear line per model to the server log (called at startup)."""
        rep = self.self_test(force=True)
        if rep["mode"] != "mock":
            self.demote_slow_models(rep)
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
