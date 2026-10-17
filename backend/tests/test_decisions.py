"""Decisions: the `decisions` chain (Jev, Jev via OpenRouter, Mercury Decide, Gemma), student-data rules, escalation, events."""
import pytest

from app.providers.base import ProviderError, ProviderTimeout
from app.providers.typesafe import TypeSafeClient, choice, noul, score
from app.router import ModelRouter

Q = {"q1": noul("Is it relevant?")}


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-key-123456")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key-123456")


def sure(p=0.95):
    return lambda *a, **k: {"q1": {"type": "noul", "noul": p}}


@pytest.fixture
def r(store, keys):
    return ModelRouter(store=store, mode="live")


def test_typesafe_is_preferred_when_both_keys_exist(r, monkeypatch):
    called = []
    monkeypatch.setattr(r.jev, "systemone", lambda m, s, q, t=15: called.append(("typesafe", m)) or sure()(m))
    monkeypatch.setattr(r.or_decider, "systemone", lambda m, s, q, t=15: called.append(("openrouter", m)) or sure()(m))
    out = r.decide("gate", "state", Q)
    assert called == [("typesafe", "jev-latest")] and out["q1"]["_by"] == "jev"


def test_jev_via_openrouter_when_there_is_no_typesafe_key(store, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key-123456")
    r = ModelRouter(store=store, mode="live")
    called = []
    monkeypatch.setattr(r.or_decider, "systemone", lambda m, s, q, t=15: called.append(m) or sure()(m))
    out = r.decide("gate", "state", Q)
    assert called == ["typesafe/jev-1.13"] and out["q1"]["_by"] == "jev_or"


def test_chain_order_jev_then_jev_or_then_mercury_then_gemma(r, monkeypatch, store):
    seen = []

    def fail(name):
        def f(m, s, q, t=15):
            seen.append(name)
            raise ProviderError(f"{name} HTTP 503")
        return f

    monkeypatch.setattr(r.jev, "systemone", fail("jev"))
    monkeypatch.setattr(r.or_decider, "systemone", lambda m, s, q, t=15: (seen.append(m), (_ for _ in ()).throw(ProviderError("down")))[1])
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda *a, **k: '{"q1": {"noul": 0.9}}')
    out = r.decide("gate", "state", Q)
    assert seen == ["jev", "typesafe/jev-1.13", "inception/mercury-decide:free"] and out["q1"]["_by"] == "gemma_26b"
    errs = [(e["model_used"], e["error"]) for e in reversed(store.events())
            if e["agent"] == "gate" and e["status"] == "fallback" and e["error"] and "answered by" not in e["error"]]
    assert [m for m, _ in errs] == ["jev", "jev_or", "mercury_decide"]           # every escalation is in events, with its reason
    assert all("503" in e or "down" in e for _, e in errs)


def test_student_data_never_reaches_the_free_tier_model(r, monkeypatch):
    seen = []
    monkeypatch.setattr(r.jev, "systemone", lambda *a, **k: (_ for _ in ()).throw(ProviderError("down")))
    monkeypatch.setattr(r.or_decider, "systemone", lambda m, s, q, t=15: (seen.append(m), (_ for _ in ()).throw(ProviderError("down")))[1])
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda *a, **k: '{"q1": {"noul": 0.9}}')
    out = r.decide("diagnoser", {"student_answer": "my answer"}, Q, student_data=True)
    assert "inception/mercury-decide:free" not in seen and seen == ["typesafe/jev-1.13"]
    assert out["q1"]["_by"] == "gemma_26b"
    seen.clear()
    r.decide("page_health", {"text": "public page text"}, Q)                       # public content may use it
    assert "inception/mercury-decide:free" in seen


def test_low_confidence_escalates_and_the_better_answer_wins(r, monkeypatch, store):
    qs = {"a": noul("?"), "b": choice("pick", {"x": "X", "y": "Y"}), "c": score("how much", ["no", "some", "yes"])}
    monkeypatch.setattr(r.jev, "systemone", lambda m, s, q, t=15: {
        "a": {"type": "noul", "noul": 0.5},                                         # unsure (0.35-0.65)
        "b": {"type": "choice", "choice": "x", "confidence": 0.3},                  # unsure
        "c": {"type": "score", "score": 1.9, "confidence": 0.9}})                   # sure
    asked = []
    monkeypatch.setattr(r.or_decider, "systemone", lambda m, s, q, t=15: (asked.append((m, sorted(q))), {
        "a": {"type": "noul", "noul": 0.97}, "b": {"type": "choice", "choice": "y", "confidence": 0.9}})[1])
    out = r.decide("gate", "state", qs)
    assert asked == [("typesafe/jev-1.13", ["a", "b"])]                              # only the unsure questions are re-asked
    assert out["a"]["_by"] == "jev_or" and out["b"]["choice"] == "y" and out["c"]["_by"] == "jev" and not any(v.get("unsure") for v in out.values())
    assert any("unsure about a, b" in e["error"] for e in store.events())


def test_nobody_sure_keeps_the_best_answer_flagged_unsure(r, monkeypatch):
    for client in (r.jev, r.or_decider):
        monkeypatch.setattr(client, "systemone", lambda m, s, q, t=15: {"q1": {"type": "noul", "noul": 0.5}})
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda *a, **k: '{"q1": {"noul": 0.5}}')
    out = r.decide("gate", "s", Q)
    assert out["q1"]["unsure"] is True and out["q1"]["noul"] == 0.5


def test_gemma_decider_validates_its_answers(r, monkeypatch):
    monkeypatch.setattr(r.jev, "systemone", lambda *a, **k: (_ for _ in ()).throw(ProviderError("x")))
    monkeypatch.setattr(r.or_decider, "systemone", lambda *a, **k: (_ for _ in ()).throw(ProviderError("x")))
    qs = {"n": noul("?"), "c": choice("pick", {"x": "X", "y": "Y"}), "s": score("how", ["a", "b", "c"]), "bad": choice("pick", {"x": "X"})}
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda *a, **k: (
        '```json\n{"n": {"noul": 7}, "c": {"choice": "y", "confidence": 0.8}, "s": {"score": 99, "confidence": 0.7}, "bad": {"choice": "nope"}}\n```'))
    out = r.decide("gate", "s", qs)
    assert out["n"]["noul"] == 1.0 and out["c"]["choice"] == "y" and out["s"]["score"] == 2 and "bad" not in out     # clamped; invalid dropped


def test_everything_down_returns_none_and_events_say_why(r, monkeypatch, store):
    boom = lambda *a, **k: (_ for _ in ()).throw(ProviderTimeout("timed out after 15s"))  # noqa: E731
    monkeypatch.setattr(r.jev, "systemone", boom)
    monkeypatch.setattr(r.or_decider, "systemone", boom)
    monkeypatch.setattr(r.providers["gemini_api"], "generate", boom)
    assert r.decide("gate", "s", Q) is None
    assert len([e for e in store.events() if e["agent"] == "gate"]) == 4


def test_chaos_and_kill_switches_apply_to_decision_models(r, monkeypatch, store):
    r.chaos.set("model:jev=fail,model:jev_or=fail,model:mercury_decide=fail")      # acceptance 7: Jev and Mercury Decide disabled
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda *a, **k: '{"q1": {"noul": 0.9}}')
    out = r.decide("claim_support", "s", Q)
    assert out["q1"]["_by"] == "gemma_26b"
    chaos = [e for e in store.events() if "chaos: forced fail" in e["error"]]
    assert {e["model_used"] for e in chaos} == {"jev", "jev_or", "mercury_decide"}
    r.chaos.set("")
    r.kill("jev")
    monkeypatch.setattr(r.or_decider, "systemone", sure())
    assert r.decide("claim_support", "s", Q)["q1"]["_by"] == "jev_or"


def test_decision_client_uses_the_right_url_and_scrubs(monkeypatch):
    import httpx
    sent = {}
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key-123456")
    monkeypatch.setattr(httpx, "post", lambda url, **kw: (sent.update(url=url, **kw), type("R", (), {"status_code": 200, "text": "", "json": lambda s: {"answers": {"q": {"noul": 1}}}})())[1])
    c = TypeSafeClient("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "/systemone")
    assert c.systemone("typesafe/jev-1.13", "s", Q) == {"q": {"noul": 1}}
    assert sent["url"] == "https://openrouter.ai/api/v1/systemone" and sent["headers"] == {"Authorization": "Bearer or-test-key-123456"}
    assert sent["json"]["model"] == "typesafe/jev-1.13"
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("failed for or-test-key-123456")))
    with pytest.raises(ProviderError) as e:
        c.systemone("m", "s", Q)
    assert "or-test-key" not in str(e.value)
