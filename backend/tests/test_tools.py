"""ToolRunner: structured results, error classes, retries with backoff, breakers, budgets, stale-while-error, chaos, health."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import Store
from app.main import create_app
from app.resilience import EventSink
from app.tools import Chaos, ToolFailure, ToolRunner, ToolSpec, check_response


class R:  # minimal response
    def __init__(self, code, text="", headers=None):
        self.status_code, self.text, self.headers = code, text, headers or {}


def make(specs=None, chaos=""):
    store = Store(":memory:")
    runner = ToolRunner(store, EventSink(store), Chaos(chaos), specs)
    runner.sleep = lambda s: runner.slept.append(s)
    runner.slept = []
    return runner, store


def counter(results):
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        r = results[min(calls["n"] - 1, len(results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    return fn, calls


def test_success_is_a_structured_result():
    runner, store = make()
    res = runner.run("wikipedia", lambda: {"title": "Binary search"}, summary="search")
    d = res.to_dict()
    assert d["ok"] and d["data"] == {"title": "Binary search"} and d["error"] is None and d["provider"] == "wikipedia"
    assert d["cached"] is False and d["latency_ms"] >= 0
    ev = store.events()[0]
    assert ev["agent"] == "tool:wikipedia" and ev["status"] == "ok"


@pytest.mark.parametrize("status,kind,retryable", [(429, "rate_limit", True), (503, "server", True), (500, "server", True),
                                                   (408, "timeout", True), (401, "auth", False), (403, "auth", False),
                                                   (402, "auth", False), (404, "invalid", False)])
def test_http_errors_are_classified(status, kind, retryable):
    with pytest.raises(ToolFailure) as e:
        check_response(R(status, "nope"), "x")
    assert e.value.kind == kind and e.value.retryable is retryable and e.value.status == status
    check_response(R(200))  # success passes through


def test_exceptions_are_classified():
    runner, _ = make({"t": ToolSpec("t", retries=0)})
    assert runner.run("t", lambda: (_ for _ in ()).throw(httpx.ReadTimeout("slow"))).error.kind == "timeout"
    assert runner.run("t", lambda: (_ for _ in ()).throw(httpx.ConnectError("down"))).error.kind == "server"
    assert runner.run("t", lambda: 1 / 0).error.kind == "server"          # a bug in a tool never escapes


def test_retryable_errors_retry_with_backoff_and_honour_retry_after():
    runner, store = make({"t": ToolSpec("t", retries=2)})
    fn, calls = counter([ToolFailure("rate_limit", "429", True, status=429, retry_after=4), ToolFailure("server", "503", True), {"ok": 1}])
    res = runner.run("t", fn)
    assert res.ok and calls["n"] == 3 and len(runner.slept) == 2
    assert runner.slept[0] >= 4                                            # Retry-After wins
    assert [e["status"] for e in reversed(store.events())] == ["retry", "retry", "ok"]
    # non-retryable errors are not retried
    fn2, c2 = counter([ToolFailure("auth", "bad key", False, status=403)])
    assert not runner.run("t2", fn2).ok and c2["n"] == 1


def test_thin_and_empty_output_are_invalid():
    runner, _ = make({"t": ToolSpec("t", retries=0, breaker_threshold=99)})
    assert runner.run("t", lambda: "short", min_chars=300).error.kind == "thin"
    assert runner.run("t", lambda: "").error.kind == "invalid"
    assert runner.run("t", lambda: {"a": 1}, validate=lambda d: (_ for _ in ()).throw(ValueError("no title"))).error.kind == "invalid"
    assert runner.run("t", lambda: "x" * 400, min_chars=300).ok


def test_cache_hit_then_stale_while_error():
    runner, store = make({"t": ToolSpec("t", retries=0, cache_ttl_s=0.05)})
    fn, calls = counter([{"v": 1}, ToolFailure("server", "boom", True)])
    assert runner.run("t", fn, cache_key="k").ok and calls["n"] == 1
    hit = runner.run("t", fn, cache_key="k")
    assert hit.cached and not hit.stale and calls["n"] == 1                # fresh cache: no call
    import time
    time.sleep(0.08)                                                       # now stale; the live call fails
    res = runner.run("t", fn, cache_key="k")
    assert res.ok and res.cached and res.stale and res.data == {"v": 1} and calls["n"] == 2
    assert any("stale cache" in e["summary"] for e in store.events())


def test_circuit_breaker_per_tool_skips_calls_then_serves_stale():
    runner, store = make({"a": ToolSpec("a", retries=0, breaker_threshold=2, breaker_cooldown_s=60),
                          "b": ToolSpec("b", retries=0)})
    fn, calls = counter([ToolFailure("server", "boom", True)])
    runner.run("a", fn)
    runner.run("a", fn)
    assert calls["n"] == 2
    res = runner.run("a", fn)
    assert calls["n"] == 2 and res.error.kind == "circuit_open"            # not even called
    assert runner.run("b", lambda: {"ok": 1}).ok                           # other tools unaffected
    assert runner.health()["a"]["state"] == "open_circuit" and runner.health()["b"]["state"] == "ok"


def test_per_notebook_budget_caps_calls():
    runner, _ = make({"fc": ToolSpec("fc", retries=0, budget_default=2, cache_ttl_s=0)})
    fn, calls = counter([{"x": 1}])
    with runner.notebook_scope("nb1"):
        assert runner.run("fc", fn).ok and runner.run("fc", fn).ok
        third = runner.run("fc", fn)
    assert not third.ok and third.error.kind == "budget" and calls["n"] == 2
    with runner.notebook_scope("nb2"):                                     # another notebook has its own budget
        assert runner.run("fc", fn).ok
    assert runner.run("fc", fn).ok                                         # outside any notebook: uncapped


def test_budget_env_override(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_MAX_CALLS_PER_NOTEBOOK", "1")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key-123456")
    runner, _ = make()
    fn, calls = counter([{"x": 1}])
    with runner.notebook_scope("nb"):
        assert runner.run("firecrawl", fn).ok
        assert runner.run("firecrawl", fn).error.kind == "budget"
    assert runner.health()["firecrawl"]["budget_per_notebook"] == 1


@pytest.mark.parametrize("code", [401, 402])
def test_firecrawl_is_disabled_for_the_session_after_401_or_402(monkeypatch, code):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key-123456")
    runner, store = make()
    fn, calls = counter([ToolFailure("auth", f"HTTP {code}", False, status=code)])
    assert runner.run("firecrawl", fn).error.kind == "auth"
    again = runner.run("firecrawl", fn)
    assert calls["n"] == 1 and again.error.kind == "disabled"             # never called again
    h = runner.health()["firecrawl"]
    assert h["state"] == "disabled" and f"HTTP {code}" in h["reason"]
    runner.enable("firecrawl")
    assert runner.run("firecrawl", lambda: {"ok": 1}).ok


def test_required_key_missing_means_unconfigured_without_calling(monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    runner, _ = make()
    fn, calls = counter([{"x": 1}])
    res = runner.run("firecrawl", fn)
    assert res.error.kind == "unconfigured" and "FIRECRAWL_API_KEY" in res.error.message and calls["n"] == 0
    assert runner.health()["firecrawl"]["state"] == "disabled"


@pytest.mark.parametrize("mode,kind", [("fail", "chaos"), ("timeout", "timeout"), ("rate_limit", "rate_limit"), ("empty", "invalid")])
def test_chaos_modes(mode, kind):
    runner, store = make({"commons": ToolSpec("commons", retries=0)}, chaos=f"tool:commons={mode}")
    fn, calls = counter([{"ok": 1}])
    res = runner.run("commons", fn)
    assert not res.ok and res.error.kind == kind
    assert (calls["n"] == 1) if mode == "empty" else (calls["n"] == 0)    # 'empty' still calls the tool, then blanks the output
    assert runner.health()["commons"]["chaos"] == mode
    runner.chaos.set("")
    assert runner.run("commons", fn).ok


def test_chaos_spec_parsing():
    c = Chaos("tool:commons=fail, model:jev=timeout ,junk,tool:x=bogus,tool:openverse=rate_limit")
    assert c.mode_for("tool", "commons") == "fail" and c.mode_for("model", "jev") == "timeout"
    assert c.mode_for("tool", "x") is None and c.mode_for("tool", "openverse") == "rate_limit"
    assert c.describe() == "model:jev=timeout,tool:commons=fail,tool:openverse=rate_limit"


def test_events_and_errors_never_contain_keys(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaSyYOUTUBEKEYYOUTUBEKEY1234")
    runner, store = make({"youtube": ToolSpec("youtube", retries=0)})
    runner.run("youtube", lambda: (_ for _ in ()).throw(ToolFailure("server", "HTTP 500 for ?key=AIzaSyYOUTUBEKEYYOUTUBEKEY1234", True)))
    blob = str(store.events()) + str(runner.health()) + str(runner.run("youtube", lambda: 1 / 0).to_dict())
    assert "AIzaSyYOUTUBE" not in blob


def test_health_states_and_masked_keys(monkeypatch):
    monkeypatch.setenv("OPENVERSE_CLIENT_ID", "ov-id-123456789")
    monkeypatch.setenv("OPENVERSE_CLIENT_SECRET", "ov-secret-123456789")
    runner, _ = make({"openverse": ToolSpec("openverse", key_env="OPENVERSE_CLIENT_ID", extra_keys=("OPENVERSE_CLIENT_SECRET",), retries=0,
                                            breaker_threshold=5)})
    assert runner.health()["openverse"]["state"] == "ok"
    runner.run("openverse", lambda: (_ for _ in ()).throw(ToolFailure("server", "boom", True)))
    h = runner.health()["openverse"]
    assert h["state"] == "degraded" and h["last_error"] == "boom" and h["last_error_at"]
    runner.run("openverse", lambda: {"ok": 1})
    h = runner.health()["openverse"]
    assert h["state"] == "ok" and h["last_success_at"]
    assert [k["masked"] for k in h["keys"]] == ["********", "********"] and "ov-id" not in str(h)
    default_runner, _ = make()
    assert default_runner.health()["codeforces"]["state"] == "disabled" and "not integrated" in default_runner.health()["codeforces"]["reason"]


def test_local_tools_appear_in_health():
    runner, _ = make()
    runner.register_local("svg_renderer", lambda: {"note": "deterministic"})
    runner.register_local("broken", lambda: 1 / 0)
    h = runner.health()
    assert h["svg_renderer"] == {"state": "ok", "kind": "local", "note": "deterministic"} and h["broken"]["state"] == "degraded"


# ------------------------------------------------------------------------------ API
@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(db_path=str(tmp_path / "t.db"), mode="mock", start_worker=False)) as c:
        yield c


def test_health_tools_endpoint(client, monkeypatch):
    rep = client.get("/api/health/tools").json()
    assert {"web_fetch", "jina", "firecrawl", "wikipedia", "commons", "openverse", "youtube", "codeforces"} <= set(rep["tools"])
    assert rep["tools"]["firecrawl"]["state"] == "disabled" and rep["tools"]["wikipedia"]["state"] == "ok"
    assert "jev" in rep["decision_models"] and rep["decision_models"]["jev"]["keys"][0]["env"] == "TYPESAFE_API_KEY"
    assert "masked" in rep["tools"]["firecrawl"]["keys"][0]


def test_chaos_switch_over_the_admin_api(client):
    assert client.post("/api/admin/chaos", json={"spec": "tool:commons=fail,model:jev=fail"}).json()["spec"] == "model:jev=fail,tool:commons=fail"
    assert client.get("/api/health/tools").json()["tools"]["commons"]["chaos"] == "fail"
    assert client.post("/api/admin/chaos", json={"spec": ""}).json()["spec"] == ""


def test_chaos_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("AASAAN_CHAOS", "tool:commons=fail")
    with TestClient(create_app(db_path=str(tmp_path / "c.db"), mode="mock", start_worker=False)) as c:
        assert c.get("/api/admin/chaos").json()["spec"] == "tool:commons=fail"


def test_model_chaos_forces_fallback_with_a_reason(router, store):
    router.chaos.set("model:gemma_31b=fail")
    router.mock.script("critic", [{"ok": 1}])
    res = router.call("critic", "p", {}, lambda raw: raw)
    assert res.model_used == "gemma_26b"
    assert any("chaos: forced fail" in e["error"] for e in store.events())
