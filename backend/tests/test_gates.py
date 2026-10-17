"""Jev gates: page_health, claim_support, scoper, video_marker, quiz_sanity, input_gate. Each goes through router.decide, has a code
fallback, and records escalations as events."""
import pytest

from app import web
from app.agents import critic, gates
from app.engine import UserError
from app.providers.base import ProviderError
from app.router import ModelRouter
from app.schemas import ChapterContent, LearnerProfile, QuizOut
from conftest import FakeResp

GOOD = "Binary search halves the sorted list at every step so it needs only about log n comparisons to find a value. " * 8


def answers(**by_id):
    """A stand-in decide(): answers by question id; anything not named is left unanswered."""
    def fake(gate, state, questions, **kw):
        fake.calls.append((gate, kw, questions))
        out = {}
        for qid, val in by_id.items():
            if qid in questions:
                out[qid] = val if isinstance(val, dict) else {"type": "noul", "noul": val, "_by": "jev"}
        return out or None
    fake.calls = []
    return fake


# ------------------------------------------------------------------------------------------ page_health
def test_cookie_wall_makes_the_fetcher_try_the_next_tier(fake_web, router, monkeypatch):
    wall = "We use cookies. Accept all cookies to continue reading this site and its content today. " * 3
    fake_web.get(lambda url, p: FakeResp(200, "<html><body>x</body></html>") if url == "https://news.example.com/a" else None)
    fake_web.get(lambda url, p: FakeResp(200, "Title: Binary search\n\nMarkdown Content:\n" + GOOD, headers={"content-type": "text/plain"})
                 if url.startswith("https://r.jina.ai/") else None)
    monkeypatch.setattr(web, "extract_text", lambda html: wall)
    web.set_page_gate(lambda url, title, text: gates.page_health(router, url, title, text))
    res = web.fetch_page("https://news.example.com/a")
    assert res.ok and res.tier == 1 and "Binary search" in res.text           # tier 0 was a wall, Jina gave the real page


def test_model_judgement_rejects_a_tier_and_every_tier_failing_says_why(fake_web, router, monkeypatch):
    fake_web.get(lambda url, p: FakeResp(200, "<html></html>") if url == "https://x.example.com/a" else None)
    monkeypatch.setattr(web, "extract_text", lambda html: GOOD)
    router.decide = answers(real=0.05)
    web.set_page_gate(lambda url, title, text: gates.page_health(router, url, title, text))
    res = web.fetch_page("https://x.example.com/a")
    assert not res.ok and "page_health" in res.error


def test_no_decision_provider_means_accept_the_page(fake_web, router, monkeypatch):
    fake_web.get(lambda url, p: FakeResp(200, "<html></html>") if url == "https://x.example.com/a" else None)
    monkeypatch.setattr(web, "extract_text", lambda html: GOOD)
    web.set_page_gate(lambda url, title, text: gates.page_health(router, url, title, text))      # mock mode: decide() is None
    assert web.fetch_page("https://x.example.com/a").ok


# ---------------------------------------------------------------------------------------- claim_support
NOTES = ("## Binary search\nBinary search works on a sorted list and halves the search range at every step.\n"
         "It needs about 20 comparisons for one million items, because 2 to the power 20 is just over one million.\n")
CHUNKS = [{"chunk_id": "s1-p1-c1", "page": 1, "text": "Binary search halves a sorted list each step. About 20 comparisons cover a million items.", "source_id": "s1"}]


def content():
    return ChapterContent(notes_md=NOTES + "More text to be long enough for the schema check on notes. " * 2,
                          slides=[{"title": f"S{i}", "points": ["a point here", "another point"]} for i in range(3)], summary="s", source_refs=["s1-p1-c1"])


def test_all_claims_supported_skips_the_critic_model(router, store, monkeypatch):
    monkeypatch.setattr(router, "decide", lambda g, s, q, **kw: {qid: {"type": "noul", "noul": 0.95, "_by": "jev"} for qid in q})
    out = critic.run(router, content(), CHUNKS, LearnerProfile(learner_id="l"))
    assert out.value.verdict == "approve" and [c for c in router.mock.calls if c[0] == "critic"] == []
    assert any("Critic model not needed" in e["summary"] for e in store.events())


def test_only_flagged_claims_reach_the_critic(router, store, monkeypatch):
    seen = {}
    monkeypatch.setattr(router, "decide", lambda g, s, q, **kw: {qid: {"type": "noul", "noul": 0.1 if qid == "c1" else 0.95, "_by": "jev"} for qid in q})
    orig = router.call
    monkeypatch.setattr(router, "call", lambda role, prompt, *a, **k: (seen.update(prompt=prompt), orig(role, prompt, *a, **k))[1])
    critic.run(router, content(), CHUNKS, LearnerProfile(learner_id="l"))
    assert '"flagged_claims": ["It needs about 20 comparisons' in seen["prompt"]
    assert [c for c in router.mock.calls if c[0] == "critic"]


def test_claim_support_without_sources_or_providers_changes_nothing(router):
    assert gates.claim_support(router, NOTES, []) is None
    assert gates.claim_support(router, NOTES, CHUNKS) is None                      # mock mode: no provider answered


# ------------------------------------------------------------------------------------------- scoper
def test_scoper_reads_track_and_level_and_never_sends_student_data_to_mercury(router, monkeypatch):
    fake = answers(track={"type": "choice", "choice": "dsa", "confidence": 0.9, "_by": "jev"}, level={"type": "score", "score": 1.2, "confidence": 0.8, "_by": "jev"})
    monkeypatch.setattr(router, "decide", fake)
    out = gates.scoper(router, "Teach me DSA, I know some Python", [])
    assert out["track"] == "dsa" and out["level"] == "intermediate"
    assert fake.calls[0][1]["student_data"] is True


def test_mercury_is_really_skipped_for_student_data_gates(store, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-key-123456")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key-123456")
    r = ModelRouter(store=store, mode="live")
    used = []
    for name, client in (("jev", r.jev), ("or", r.or_decider)):
        monkeypatch.setattr(client, "systemone", lambda m, s, q, t=15, n=name: used.append(m) or (_ for _ in ()).throw(ProviderError("down")))
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda *a, **k: '{"kind": {"choice": "learn_request", "confidence": 0.9}}')
    gates.input_gate(r, "Teach me binary search")
    assert "inception/mercury-decide:free" not in used


# ---------------------------------------------------------------------------------------- video_marker
DESC = "Great lecture.\n0:00 Intro\n3:15 Linear search\n10:42 Binary search\n21:05 Summary\n"


def test_markers_are_parsed_from_the_description():
    m = gates.parse_markers(DESC)
    assert [x["start"] for x in m] == [0, 195, 642, 1265] and m[2]["title"] == "Binary search"
    assert gates.parse_markers("no timestamps here") == [] and gates.parse_markers("1:00 a\n2:00 b\n3:00 c") == []   # must start at 0:00


def test_video_marker_picks_a_start_time(router, monkeypatch):
    monkeypatch.setattr(router, "decide", answers(vid1={"type": "choice", "choice": "m2", "confidence": 0.9, "_by": "jev"}))
    assert gates.video_marker(router, "Binary search", {"vid1": gates.parse_markers(DESC), "vid2": []}) == {"vid1": 642}
    monkeypatch.setattr(router, "decide", answers(vid1={"type": "choice", "choice": "none", "confidence": 0.9, "_by": "jev"}))
    assert gates.video_marker(router, "Binary search", {"vid1": gates.parse_markers(DESC)}) == {}
    monkeypatch.setattr(router, "decide", answers(vid1={"type": "choice", "choice": "m1", "confidence": 0.2, "unsure": True, "_by": "gemma_26b"}))
    assert gates.video_marker(router, "Binary search", {"vid1": gates.parse_markers(DESC)}) == {}        # unsure -> start at 0:00


# ----------------------------------------------------------------------------------------- quiz_sanity
def quiz(n=4):
    qs = [{"id": f"q{i}", "text": f"Question {i}?", "options": ["a", "b", "c", "d"], "answer_key": "b", "concept": "c", "hint": "h"} for i in range(1, n + 1)]
    return QuizOut.model_validate({"questions": qs})


def test_ambiguous_questions_are_dropped_while_three_remain(router, monkeypatch):
    monkeypatch.setattr(router, "decide", answers(q2=0.1))
    out, notes = gates.quiz_sanity(router, quiz(4), "notes")
    assert [q.id for q in out.questions] == ["q1", "q3", "q4"] and "q2" in notes[0]


def test_quiz_is_kept_when_dropping_would_leave_fewer_than_three(router, monkeypatch):
    monkeypatch.setattr(router, "decide", answers(q2=0.1))
    out, notes = gates.quiz_sanity(router, quiz(3), "notes")
    assert len(out.questions) == 3 and "kept" in notes[0]


# ------------------------------------------------------------------------------------------ input_gate
def test_input_gate_blocks_only_confident_unsafe_or_nonsense(router, monkeypatch):
    for choice_, conf, expect_block in (("unsafe", 0.9, True), ("unsafe", 0.5, False), ("not_a_request", 0.95, True), ("not_a_request", 0.6, False),
                                       ("learn_request", 0.99, False)):
        monkeypatch.setattr(router, "decide", answers(kind={"type": "choice", "choice": choice_, "confidence": conf, "_by": "jev"}))
        assert bool(gates.input_gate(router, "two words")) is expect_block, (choice_, conf)
    monkeypatch.setattr(router, "decide", answers(kind={"type": "choice", "choice": "unsafe", "confidence": 0.9, "unsure": True, "_by": "gemma_26b"}))
    assert gates.input_gate(router, "two words") is None


def test_create_notebook_refuses_a_blocked_goal_but_never_when_material_is_given(engine, monkeypatch, sample_text):
    monkeypatch.setattr(engine.router, "decide", answers(kind={"type": "choice", "choice": "unsafe", "confidence": 0.95, "_by": "jev"}))
    with pytest.raises(UserError, match="can't build a course"):
        engine.create_notebook("L1", "something awful here", "en", [])
    assert engine.create_notebook("L1", sample_text, "en", [])


def test_input_gate_runs_with_student_data_and_a_short_timeout(router, monkeypatch):
    fake = answers(kind={"type": "choice", "choice": "learn_request", "confidence": 0.9, "_by": "jev"})
    monkeypatch.setattr(router, "decide", fake)
    gates.input_gate(router, "Teach me recursion")
    assert fake.calls[0][1]["student_data"] is True and fake.calls[0][1]["timeout_s"] <= 8


# --------------------------------------------------------------- acceptance: Jev and Mercury disabled -> Gemma, events show it
def test_with_jev_and_mercury_disabled_gemma_decides_and_events_say_so(store, monkeypatch):
    for k in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = ModelRouter(store=store, mode="live")
    monkeypatch.setattr(r.providers["gemini_api"], "generate", lambda *a, **k: '{"real": {"noul": 0.9}}')
    healthy, _ = gates.page_health(r, "https://x.example.com", "T", GOOD)
    assert healthy
    evs = [e for e in store.events(100) if e["agent"] in ("decisions", "page_health")]
    assert any("TYPESAFE_API_KEY is not set" in e["error"] for e in evs)           # Jev skipped, with the reason
    assert any(e["agent"] == "page_health" and e["model_used"] == "gemma_26b" for e in evs)


def test_gate_escalation_is_recorded_when_jev_is_unsure(store, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test-key-123456")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-test-key-123456")
    r = ModelRouter(store=store, mode="live")
    monkeypatch.setattr(r.jev, "systemone", lambda m, s, q, t=15: {"real": {"type": "noul", "noul": 0.5}})
    monkeypatch.setattr(r.or_decider, "systemone", lambda m, s, q, t=15: {"real": {"type": "noul", "noul": 0.9}})
    assert gates.page_health(r, "https://x.example.com", "T", GOOD)[0]
    assert any("unsure about real: escalating" in e["error"] for e in store.events(100))


def test_input_gate_never_holds_the_submit_button_longer_than_its_budget(router, monkeypatch, store):
    import time
    monkeypatch.setattr(gates, "INPUT_GATE_BUDGET_S", 0.2)
    monkeypatch.setattr(router, "decide", lambda *a, **k: time.sleep(1.0))
    t0 = time.perf_counter()
    assert gates.input_gate(router, "teach me recursion") is None
    assert time.perf_counter() - t0 < 0.8
    assert any("input gate skipped" in e["summary"] for e in store.events())
