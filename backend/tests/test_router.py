import pytest

from app.providers.base import ProviderError
from app.router import AllFailed


def v(raw):
    if "ok" not in raw:
        raise ValueError("no ok")
    return raw


def test_primary_used_and_event_logged(router, store):
    router.fake.script("critic", [{"ok": 1}])
    res = router.call("critic", "p", {}, v)
    assert res.model_used == "gemma_31b" and res.fallback_step == 0
    ev = store.events()[0]
    assert ev["status"] == "ok" and ev["agent"] == "critic"


def test_killed_primary_falls_back(router):
    router.kill("gemma_31b")
    router.fake.script("critic", [{"ok": 1}])
    res = router.call("critic", "p", {}, v)
    assert res.model_used == "gemma_26b" and res.fallback_step == 1


def test_invalid_json_gets_one_repair_retry_on_same_model(router):
    router.fake.script("critic", ["not json at all", {"ok": 1}])
    res = router.call("critic", "p", {}, v)
    assert res.model_used == "gemma_31b"
    assert [m for _, m in router.fake.calls] == ["gemma-4-31b-it"] * 2


def test_persistent_invalid_output_moves_to_next_model(router):
    router.fake.script("critic", [{"bad": 1}, {"bad": 1}, {"ok": 1}])
    res = router.call("critic", "p", {}, v)
    assert res.model_used == "gemma_26b"


def test_provider_errors_exhaust_to_last_resort(router, store):
    router.fake.script("critic", [ProviderError("rate limit")] * 4)
    res = router.call("critic", "p", {}, v, last_resort=lambda c: {"ok": "rules"})
    assert res.used_last_resort and res.model_used == "rule_checks"
    assert any(e["status"] == "fallback" for e in store.events())


def test_all_failed_without_last_resort(router, store):
    for m in router.cfg["models"]:
        router.kill(m)
    with pytest.raises(AllFailed):
        router.call("critic", "p", {}, v)
    assert store.events()[0]["status"] == "failed"


def test_fenced_json_is_parsed(router):
    router.fake.script("critic", ['Here you go:\n```json\n{"ok": 2}\n```'])
    assert router.call("critic", "p", {}, v).value == {"ok": 2}
