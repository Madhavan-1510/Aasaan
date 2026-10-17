"""Findings from the first live Gemini run: thought parts, thinkingLevel, slow-model handling, OpenRouter."""
import copy

import pytest

from app.providers.base import ProviderError, ProviderNotConfigured, ProviderTimeout
from app.providers.gemini import GeminiProvider
from app.providers.openrouter import OpenRouterProvider
from app.router import ModelRouter


class Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text or str(data)

    def json(self):
        return self._data


@pytest.fixture
def http(monkeypatch):
    calls, queue = [], []

    def fake_post(url, **kw):
        calls.append({"url": url, **copy.deepcopy(kw)})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    return calls, queue


def test_gemini_ignores_thought_parts_and_sends_thinking_level(monkeypatch, http):
    calls, queue = http
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    queue.append(Resp(200, {"candidates": [{"content": {"parts": [
        {"text": "The user wants JSON...", "thought": True}, {"text": "", "thought": True}, {"text": '{"ok": true}'}]}}]}))
    out = GeminiProvider().generate("gemma-4-26b-a4b-it", "hi", thinking_level="minimal")
    assert out == '{"ok": true}'  # reasoning never leaks into the answer
    cfg = calls[0]["json"]["generationConfig"]
    assert cfg["thinkingConfig"] == {"thinkingLevel": "minimal"} and cfg["responseMimeType"] == "application/json"


def test_gemini_drops_thinking_config_if_rejected(monkeypatch, http):
    calls, queue = http
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    queue += [Resp(400, text="Unknown field thinkingConfig / thinking not supported"),
              Resp(200, {"candidates": [{"content": {"parts": [{"text": "{}"}]}}]})]
    p = GeminiProvider()
    assert p.generate("gemma-4-31b-it", "hi", thinking_level="minimal") == "{}"
    assert "thinkingConfig" not in calls[1]["json"]["generationConfig"] and p.thinking_supported is False


def test_gemini_timeout_is_a_timeout_error_without_the_key(monkeypatch, http):
    import httpx
    calls, queue = http
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET123")
    queue.append(httpx.ReadTimeout("The read operation timed out"))
    with pytest.raises(ProviderTimeout) as e:
        GeminiProvider().generate("gemma-4-31b-it", "hi")
    assert "SECRET123" not in str(e.value)


def test_openrouter_request_shape(monkeypatch, http):
    calls, queue = http
    monkeypatch.setenv("OPENROUTER_API_KEY", "ok-key")
    queue.append(Resp(200, {"choices": [{"message": {"content": "hello"}}]}))
    assert OpenRouterProvider().generate("google/gemma-4-26b-a4b-it", "hi") == "hello"
    c = calls[0]
    assert c["url"] == "https://openrouter.ai/api/v1/chat/completions" and c["headers"]["Authorization"] == "Bearer ok-key"
    assert c["json"]["model"] == "google/gemma-4-26b-a4b-it" and c["json"]["messages"][0] == {"role": "user", "content": "hi"}
    with pytest.raises(ProviderNotConfigured):
        OpenRouterProvider().generate("m", "x", images=[("image/png", b"1")])
    queue.append(Resp(402, text='{"error": {"message": "insufficient credits"}}'))
    with pytest.raises(ProviderError, match="402.*insufficient credits"):
        OpenRouterProvider().generate("m", "x")


def test_timeout_is_not_retried_on_the_same_model(store, monkeypatch):
    r = ModelRouter(store=store)
    calls = []

    def slow(m, p, **k):
        calls.append(m)
        raise ProviderTimeout("timed out after 30s")

    monkeypatch.setattr(r.providers["gemini_api"], "generate", slow)
    monkeypatch.setattr(r.providers["openrouter"], "generate", lambda *a, **k: '{"ok": 1}')
    res = r.call("critic", "p", {}, lambda raw: raw)
    assert calls == ["gemma-4-31b-it", "gemma-4-26b-a4b-it"]  # one try each, not two
    assert res.model_used == "or_gemma_31b"


def test_circuit_breaker_skips_a_failing_model_then_recovers(store, monkeypatch):
    r = ModelRouter(store=store)
    r.breaker_cooldown_s = 0.2
    seen = []

    def gem(m, p, **k):
        seen.append(m)
        raise ProviderError("HTTP 503 high demand")

    monkeypatch.setattr(r.providers["gemini_api"], "generate", gem)
    monkeypatch.setattr(r.providers["openrouter"], "generate", lambda *a, **k: '{"ok": 1}')
    r.call("critic", "one", {}, lambda raw: raw)       # trips both Gemini models
    assert r.circuit_open("gemma_31b") > 0 and r.circuit_open("gemma_26b") > 0
    n = len(seen)
    r.call("critic", "two", {}, lambda raw: raw)       # skipped instantly: no new Gemini calls
    assert len(seen) == n
    assert any("circuit open" in e["error"] for e in store.events())
    import time
    time.sleep(0.25)                                   # cooldown over: tried again
    r.call("critic", "three", {}, lambda raw: raw)
    assert len(seen) > n
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda *a, **k: '{"ok": 2}')
    time.sleep(0.25)
    assert r.call("critic", "four", {}, lambda raw: raw).model_used == "gemma_31b"
    assert r.circuit_open("gemma_31b") == 0            # a success closes the circuit


def test_missing_keys_do_not_trip_the_breaker(store, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = ModelRouter(store=store)
    r.call("critic", "p", {}, lambda raw: raw, last_resort=lambda c: {"ok": 1})
    assert r.circuit_open("gemma_31b") == 0


def test_thinking_level_is_passed_from_config(store, monkeypatch):
    r = ModelRouter(store=store)
    got = {}
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda m, p, **k: got.update(k) or '{"ok": 1}')
    r.call("critic", "p", {}, lambda raw: raw)
    assert got["thinking_level"] == "minimal"


def test_slow_answers_demote_a_model_like_failures(store, monkeypatch):
    r = ModelRouter(store=store)
    r.slow_call_s = 0.05
    import time

    def slowish(m, p, **k):
        time.sleep(0.08)
        return '{"ok": 1}'

    monkeypatch.setattr(r.providers["gemini_api"], "generate", slowish)
    monkeypatch.setattr(r.providers["openrouter"], "generate", lambda *a, **k: '{"ok": 2}')
    first = r.call("critic", "a", {}, lambda raw: raw)
    assert first.value == {"ok": 1}                      # the slow answer is still used
    r.call("critic", "b", {}, lambda raw: raw)
    assert r.circuit_open("gemma_31b") > 0               # two slow answers in a row: demoted
    assert r.call("critic", "c", {}, lambda raw: raw).model_used != "gemma_31b"


def test_startup_demotes_models_that_answer_slowly(store):
    r = ModelRouter(store=store)
    rep = {"models": {"gemma_31b": {"kind": "chat", "status": "ok", "latency_ms": 57000},
                      "gemma_26b": {"kind": "chat", "status": "ok", "latency_ms": 1500},
                      "or_gemma_31b": {"kind": "chat", "status": "failed", "latency_ms": 100}}}
    assert r.demote_slow_models(rep) == ["gemma_31b"]
    assert r.circuit_open("gemma_31b") > 200 and r.circuit_open("gemma_26b") == 0


def test_tolerant_json_parsing():
    from app.router import parse_json
    assert parse_json('{"a": "line1\nline2", "b": [1, 2,],}') == {"a": "line1\nline2", "b": [1, 2]}
    assert parse_json('Sure!\n```json\n{"a": 1,}\n```') == {"a": 1}


def test_titles_are_cleaned():
    from app import web
    assert web.clean_text("Free DSA Visualizer \ufffd Interactive &amp; Code") == "Free DSA Visualizer - Interactive & Code"


def test_startup_demotes_models_that_time_out(store):
    r = ModelRouter(store=store)
    rep = {"models": {"gemma_31b": {"kind": "chat", "status": "failed", "error": "gemini gemma-4-31b-it timed out after 100s (ReadTimeout)"},
                      "gemma_26b": {"kind": "chat", "status": "failed", "error": "gemini HTTP 503 for gemma-4-26b-a4b-it: unavailable"},
                      "sarvam_105b": {"kind": "chat", "status": "failed", "error": "SARVAM_API_KEY is not set"}}}
    assert sorted(r.demote_slow_models(rep)) == ["gemma_26b", "gemma_31b"]      # a missing key is not a health problem
