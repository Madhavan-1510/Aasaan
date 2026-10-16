"""Model-call cache by input hash, the global call limit, and notes."""
import threading
import time

from app.router import ModelRouter


def ok_validator(raw):
    if "ok" not in raw:
        raise ValueError("no ok")
    return raw


def test_live_calls_are_cached_by_input_hash(store, monkeypatch):
    r = ModelRouter(store=store, mode="live")
    calls = []
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda m, p, **k: calls.append(p) or '{"ok": 1}')
    first = r.call("critic", "same prompt", {}, ok_validator)
    second = r.call("critic", "same prompt", {}, ok_validator)
    assert first.value == second.value == {"ok": 1} and len(calls) == 1
    assert any("(cached)" in e["summary"] for e in store.events())
    r.call("critic", "different prompt", {}, ok_validator)
    assert len(calls) == 2


def test_invalid_replies_are_never_cached(store, monkeypatch):
    r = ModelRouter(store=store, mode="live")
    replies = iter(['{"bad": 1}', '{"bad": 1}', '{"ok": 2}'])  # invalid twice on the first model, then valid on the next
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda m, p, **k: next(replies))
    r.call("critic", "p", {}, ok_validator)
    assert store.cache_get("llm:" + "x") is None
    n_cached = len([1 for _ in store._all("SELECT key FROM cache WHERE key LIKE 'llm:%'")])
    assert n_cached == 1  # only the validated reply was stored


def test_mock_mode_does_not_cache(router, store):
    router.mock.script("critic", [{"ok": 1}, {"ok": 2}])
    assert router.call("critic", "p", {}, ok_validator).value == {"ok": 1}
    assert router.call("critic", "p", {}, ok_validator).value == {"ok": 2}


def test_global_semaphore_limits_concurrent_model_calls(store, monkeypatch):
    monkeypatch.setenv("AASAAN_MAX_CALLS", "2")
    r = ModelRouter(store=store, mode="live")
    active, peak, lock = 0, 0, threading.Lock()

    def slow(m, p, **k):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return '{"ok": 1}'

    monkeypatch.setattr(r.providers["gemini_api"], "generate", slow)
    threads = [threading.Thread(target=r.call, args=("critic", f"prompt {i}", {}, ok_validator)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak == 2


def test_note_records_an_event_with_reason(router, store):
    router.note("critic", "fallback", "Chapter shipped unverified", "2 revisions used")
    ev = store.events()[0]
    assert ev["agent"] == "critic" and ev["error"] == "2 revisions used"


def test_self_test_includes_grounded_search_feature(store, monkeypatch):
    r = ModelRouter(store=store, mode="live")
    for p in r.providers.values():
        monkeypatch.setattr(p, "generate", lambda *a, **k: "OK")
    monkeypatch.setattr(r.jev, "systemone", lambda *a, **k: {})
    monkeypatch.setattr(r.sarvam, "tts", lambda *a, **k: b"x")
    monkeypatch.setattr(r.sarvam, "translate", lambda *a, **k: "x")
    monkeypatch.setattr(r.providers["gemini_api"], "grounded_search", lambda *a, **k: [{"uri": "https://a", "title": "a"}])
    assert r.self_test(force=True)["features"]["grounded_search"]["status"] == "ok"
    monkeypatch.setattr(r.providers["gemini_api"], "grounded_search", lambda *a, **k: [])
    assert r.self_test(force=True)["features"]["grounded_search"]["status"] == "failed"
