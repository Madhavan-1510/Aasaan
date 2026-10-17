"""The shared resilience pieces (used by ModelRouter and ToolRunner)."""
import time

import pytest

from app.db import Store
from app.resilience import CircuitBreaker, EventSink, StaleCache, TokenBucket, backoff_delay, mask_key, scrub


def test_scrub_removes_secret_values_and_key_shapes(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyDUMMYDUMMYDUMMY1234567890")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-abcdef123456")
    text = ("HTTP 400 for https://x/y?key=AIzaSyDUMMYDUMMYDUMMY1234567890&z=1 and fc-abcdef123456 "
            "Authorization: Bearer sk-or-v1-abcdefghijklmnop api_key=zzzz1234")
    out = scrub(text)
    for secret in ("AIzaSyDUMMY", "fc-abcdef", "sk-or-v1-abcdef", "zzzz1234"):
        assert secret not in out
    assert out.count("***") >= 3


def test_mask_key_never_reveals_characters(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_live_abcdefghijkl")
    m = mask_key("TYPESAFE_API_KEY")
    assert m == {"env": "TYPESAFE_API_KEY", "set": True, "masked": "********"} and "abcd" not in str(m)
    assert mask_key("SARVAM_API_KEY") == {"env": "SARVAM_API_KEY", "set": False, "masked": ""}


def test_backoff_grows_is_capped_jittered_and_honours_retry_after():
    for attempt, hi in [(0, 0.5), (1, 1.0), (2, 2.0), (10, 8.0)]:
        ds = [backoff_delay(attempt) for _ in range(50)]
        assert all(hi * 0.5 - 1e-9 <= d <= hi + 1e-9 for d in ds)
        assert len(set(round(d, 4) for d in ds)) > 5      # jitter: not all equal
    assert backoff_delay(0, retry_after=3) >= 3
    assert backoff_delay(0, retry_after=999) <= 30.0


def test_circuit_breaker_open_half_open_closed():
    br = CircuitBreaker(threshold=2, cooldown_s=0.15)
    assert br.state() == "closed" and br.open_remaining() == 0
    assert br.record_failure() is False
    assert br.record_failure() is True and br.state() == "open" and br.open_remaining() > 0
    time.sleep(0.2)
    assert br.state() == "half_open" and br.open_remaining() == 0     # one trial allowed
    assert br.record_failure() is True and br.state() == "open"       # trial failed: straight back to open
    time.sleep(0.2)
    br.record_success()
    assert br.state() == "closed" and br.consecutive == 0
    br.trip(5)
    assert br.state() == "open" and br.open_remaining() > 4


def test_token_bucket_limits_the_rate():
    tb = TokenBucket(rate=20, burst=2)
    assert tb.try_acquire() and tb.try_acquire() and not tb.try_acquire()  # burst of 2, then empty
    t0 = time.monotonic()
    assert tb.acquire(timeout=1.0)                                          # waits for a refill (~50 ms)
    assert 0.02 < time.monotonic() - t0 < 0.5
    slow = TokenBucket(rate=0.1, burst=1)
    slow.try_acquire()
    assert slow.acquire(timeout=0.1) is False


def test_stale_cache_reports_fresh_then_stale():
    c = StaleCache(Store(":memory:"), "tool")
    assert c.get("k", 10) is None
    c.set("k", {"a": 1})
    v, age, fresh = c.get("k", 10)
    assert v == {"a": 1} and fresh and age < 1
    time.sleep(0.06)
    v, age, fresh = c.get("k", 0.01)
    assert v == {"a": 1} and not fresh                                      # still readable: stale-while-error


def test_event_sink_scrubs_and_logs_reasons(monkeypatch, caplog):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-SECRETSECRETSECRET")
    store = Store(":memory:")
    sink = EventSink(store)
    with caplog.at_level("WARNING", logger="aasaan.resilience"):
        sink.emit("tool:commons", "commons", 0, "fallback", 12, "fail", "search", "HTTP 429 key=sk-or-v1-SECRETSECRETSECRET")
    row = store.events()[0]
    assert row["agent"] == "tool:commons" and row["status"] == "fallback" and "SECRET" not in row["error"] and "***" in row["error"]
    assert "SECRET" not in caplog.text and "429" in caplog.text
    EventSink(store, enabled=False).emit("x", "y", 0, "ok")
    assert len(store.events()) == 1                                         # disabled sink writes nothing
