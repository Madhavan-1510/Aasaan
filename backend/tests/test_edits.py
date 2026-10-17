"""Course edits: the Editor proposes, code validates, the student applies, and every applied change can be undone."""
import pytest

from app import edits
from app.agents import editor, gates
from app.engine import UserError
from fake_provider import FakeProvider


def outline(n=6, links=True):
    return [{"position": i, "id": 100 + i, "title": f"Chapter {i}", "summary": "", "kind": "core", "prereq_ids": [100 + i - 1] if (links and i > 1) else []} for i in range(1, n + 1)]


def add(after=2, title="Delta Lake basics"):
    return {"op": "add_chapter", "after": after, "title": title, "objectives": ["Explain it"], "minutes": 12}


# ------------------------------------------------------------------------------------------ validation
def test_valid_operations_are_normalised_to_chapter_ids():
    ops = editor.validate_ops([add(2), {"op": "remove_chapter", "chapter": 5}, {"op": "set_level", "level": "advanced"}], outline(), "beginner")
    assert ops[0]["after_id"] == 102 and ops[1]["chapter_id"] == 105 and ops[2]["level"] == "advanced"


def test_positions_always_mean_the_course_as_it_is_now():
    ops = editor.validate_ops([{"op": "remove_chapter", "chapter": 2}, {"op": "regenerate_chapter", "chapter": 4, "focus": "examples"}], outline(), "beginner")
    assert ops[1]["chapter_id"] == 104                                         # not 105: removing chapter 2 does not renumber the request
    with pytest.raises(ValueError, match="removed"):
        editor.validate_ops([{"op": "remove_chapter", "chapter": 2}, {"op": "regenerate_chapter", "chapter": 2}], outline(), "beginner")


@pytest.mark.parametrize("bad,why", [
    ([], "between 1 and"), ([{"op": "explode"}], "one of"), ([add(9)], "between 0 and 6"), ([add(2, "x")], "title"),
    ([{"op": "remove_chapter", "chapter": 9}], "between 1 and 6"), ([{"op": "move_chapter", "chapter": 3, "after": 3}], "actually moves"),
    ([{"op": "set_level", "level": "expert"}], "level must be"), ([{"op": "set_level", "level": "beginner"}], "already"),
    ([add(1)] * 5, "between 1 and 4"),
])
def test_invalid_operations_are_rejected_with_a_reason_the_model_can_repair(bad, why):
    with pytest.raises(ValueError, match=why):
        editor.validate_ops(bad, outline(), "beginner")


def test_prerequisites_must_still_come_first():
    with pytest.raises(ValueError, match="before a chapter it depends on"):
        editor.validate_ops([{"op": "move_chapter", "chapter": 2, "after": 5}], outline(), "beginner")     # 3 depends on 2
    editor.validate_ops([{"op": "move_chapter", "chapter": 2, "after": 5}], outline(links=False), "beginner")   # fine when nothing depends on it
    with pytest.raises(ValueError, match="at least 3"):
        editor.validate_ops([{"op": "remove_chapter", "chapter": 1}], outline(3), "beginner")


def test_removing_a_chapter_bridges_its_dependants():
    ops = editor.validate_ops([{"op": "remove_chapter", "chapter": 3}], outline(), "beginner")   # 4 depended on 3, 3 on 2: still ordered
    assert ops == [{"op": "remove_chapter", "chapter_id": 103}]


# ------------------------------------------------------------------------------------------ proposals
def built(engine, worker, goal="Teach me binary search"):
    nid = engine.create_notebook("L1", goal, "en", [])
    worker.run_until_idle()
    return nid


def titles(store, nid):
    return [(c["position"], c["title"]) for c in store.chapters(nid)]


def ask(engine, nid, text):
    return engine.handle_chat(nid, "L1", text)


def test_an_edit_request_becomes_a_card_and_changes_nothing(engine, worker, store):
    nid = built(engine, worker)
    before = titles(store, nid)
    res = ask(engine, nid, "add a chapter on Delta Lake after chapter 2")
    assert res["type"] == "proposal" and res["proposal"]["status"] == "pending" and "Delta Lake" in res["proposal"]["summary"]
    assert res["proposal"]["lines"][0].startswith('Add a chapter on "Delta Lake" after chapter 2')
    assert titles(store, nid) == before and store.chapters(nid)[0]["status"] == "ready"           # nothing applied
    card = [m for m in engine.chat_history(nid) if m["kind"] == "proposal"][0]
    assert card["proposal"]["id"] == res["proposal"]["id"] and card["proposal"]["can_undo"] is False


def test_apply_adds_the_chapter_builds_only_that_chapter_and_undo_removes_it(engine, worker, store):
    nid = built(engine, worker)
    n, ready_before = len(store.chapters(nid)), {c["id"] for c in store.chapters(nid)}
    pid = ask(engine, nid, "add a chapter on Delta Lake after chapter 2")["proposal"]["id"]
    v = engine.apply_proposal(nid, pid)
    chs = store.chapters(nid)
    new = next(c for c in chs if c["id"] not in ready_before)
    assert v["status"] == "applied" and len(chs) == n + 1 and new["position"] == 3 and new["title"] == "Delta Lake" and new["prereqs"] == [chs[1]["id"]]
    assert [c["position"] for c in chs] == list(range(1, n + 2)) and new["status"] == "queued"
    snap = engine.notebook_view(nid)
    assert snap["highlight"]["ids"] == [new["id"]] and snap["proposals"][0]["can_undo"] is True
    worker.run_until_idle()                                                                      # only the new chapter is built
    assert store.get_chapter(new["id"])["status"] == "ready" and any(b["type"] == "notes" for b in store.blocks(new["id"]))
    v = engine.undo_proposal(nid, pid)
    assert v["status"] == "undone" and [c["id"] for c in store.chapters(nid)] == sorted(ready_before, key=lambda i: store.get_chapter(i)["position"])
    assert store.get_chapter(new["id"]) is None and [c["position"] for c in store.chapters(nid)] == list(range(1, n + 1))
    assert any(m["content"].startswith("Applied:") for m in store.messages(nid)) and any(m["content"].startswith("Undone:") for m in store.messages(nid))


def test_remove_then_undo_restores_the_chapter_exactly(engine, worker, store):
    nid = built(engine, worker)
    victim = store.chapters(nid)[2]
    blocks = [(b["type"], b["content"]) for b in store.blocks(victim["id"])]
    before = [(c["id"], c["position"], list(c["prereqs"])) for c in store.chapters(nid)]
    pid = ask(engine, nid, "remove chapter 3")["proposal"]["id"]
    engine.apply_proposal(nid, pid)
    assert store.get_chapter(victim["id"]) is None and len(store.chapters(nid)) == len(before) - 1
    assert all(victim["id"] not in c["prereqs"] for c in store.chapters(nid))
    engine.undo_proposal(nid, pid)
    after = [(c["id"], c["position"], list(c["prereqs"])) for c in store.chapters(nid)]
    assert after == before and [(b["type"], b["content"]) for b in store.blocks(victim["id"])] == blocks


def test_move_and_set_level_undo(engine, worker, store):
    nid = built(engine, worker)
    order = [c["id"] for c in store.chapters(nid)]
    for c in store.chapters(nid):
        store.set_chapter_prereqs(c["id"], [])                                   # nothing depends on anything, so any move is allowed
    pid = ask(engine, nid, "move chapter 2 after chapter 4")["proposal"]["id"]
    engine.apply_proposal(nid, pid)
    assert [c["id"] for c in store.chapters(nid)] == [order[0], order[2], order[3], order[1]] + order[4:]
    engine.undo_proposal(nid, pid)
    assert [c["id"] for c in store.chapters(nid)] == order
    lv = store.get_notebook(nid)["level"]
    pid = ask(engine, nid, "make it advanced")["proposal"]["id"]
    engine.apply_proposal(nid, pid)
    assert store.get_notebook(nid)["level"] == "advanced"
    engine.undo_proposal(nid, pid)
    assert store.get_notebook(nid)["level"] == lv


def test_regenerate_rebuilds_only_that_chapter_and_undo_restores_the_old_text(engine, worker, store):
    nid = built(engine, worker)
    ch = store.chapters(nid)[1]
    old = [(b["type"], b["content"]) for b in store.blocks(ch["id"])]
    others = {c["id"]: [(b["type"], b["content"]) for b in store.blocks(c["id"])] for c in store.chapters(nid) if c["id"] != ch["id"]}
    pid = ask(engine, nid, "rewrite chapter 2 with a focus on worked examples")["proposal"]["id"]
    engine.apply_proposal(nid, pid)
    assert store.get_chapter(ch["id"])["status"] == "queued" and "Focus: worked examples" in store.get_chapter(ch["id"])["objectives"]
    worker.run_until_idle()
    assert store.get_chapter(ch["id"])["status"] == "ready"
    assert {i: [(b["type"], b["content"]) for b in store.blocks(i)] for i in others} == others           # nothing else was touched
    engine.undo_proposal(nid, pid)
    assert [(b["type"], b["content"]) for b in store.blocks(ch["id"])] == old and "Focus: worked examples" not in store.get_chapter(ch["id"])["objectives"]


def test_undo_waits_while_a_chapter_from_the_change_is_being_written(engine, worker, store):
    nid = built(engine, worker)
    pid = ask(engine, nid, "add a chapter on Delta Lake after chapter 2")["proposal"]["id"]
    engine.apply_proposal(nid, pid)
    new = next(c for c in store.chapters(nid) if c["title"] == "Delta Lake")
    store.set_chapter_status(new["id"], "drafting")
    with pytest.raises(UserError, match="being written"):
        engine.undo_proposal(nid, pid)


def test_only_the_latest_change_can_be_undone_and_at_least_ten_are_kept(engine, worker, store):
    nid = built(engine, worker)
    pids = []
    for i in range(11):
        pid = ask(engine, nid, f"add a chapter on Topic {i} after chapter 1")["proposal"]["id"]
        engine.apply_proposal(nid, pid)
        pids.append(pid)
    with pytest.raises(UserError, match="most recent"):
        engine.undo_proposal(nid, pids[5])                                              # not the latest
    assert store.get_proposal(pids[0])["undo"] is None and store.revisions(pids[0]) == []   # the 11th back has aged out
    for pid in reversed(pids[1:]):                                                       # the latest ten undo one after another
        engine.undo_proposal(nid, pid)
    assert len([c for c in store.chapters(nid) if c["title"].startswith("Topic")]) == 1   # only the aged-out one remains


def test_a_stale_proposal_is_refused_instead_of_applied_blindly(engine, worker, store):
    nid = built(engine, worker)
    pid = ask(engine, nid, "remove chapter 3")["proposal"]["id"]
    store.delete_chapter(store.chapters(nid)[2]["id"])                                  # the course changed meanwhile
    with pytest.raises(UserError, match="changed since"):
        engine.apply_proposal(nid, pid)


def test_dismiss_forgets_a_pending_proposal_and_applied_ones_cannot_be_reapplied(engine, worker, store):
    nid = built(engine, worker)
    pid = ask(engine, nid, "add a chapter on Delta Lake")["proposal"]["id"]
    assert engine.dismiss_proposal(nid, pid)["status"] == "dismissed"
    with pytest.raises(UserError):
        engine.apply_proposal(nid, pid)
    pid2 = ask(engine, nid, "add a chapter on Spark")["proposal"]["id"]
    engine.apply_proposal(nid, pid2)
    with pytest.raises(UserError):
        engine.apply_proposal(nid, pid2)


def test_nothing_proposed_when_the_request_cannot_be_an_edit(engine, worker, store):
    nid = built(engine, worker)
    res = ask(engine, nid, "rewrite everything somehow")
    assert res["type"] == "text" and "couldn't turn that into a change" in res["text"]
    assert store.proposals(nid) == []


# ------------------------------------------------------------------------------------------ intents
@pytest.mark.parametrize("text,intent", [
    ("add a chapter on Delta Lake", "edit_course"), ("remove chapter 3", "edit_course"), ("make it harder", "edit_course"), ("make the course more advanced", "edit_course"),
    ("quiz me on chapter 2", "quiz_me"), ("explain this again, simpler", "explain"), ("what is a heap?", "ask_doubt"), ("add this pdf to my notes", "add_resource")])
def test_keyword_rules_route_when_no_decision_provider_answers(router, text, intent):
    assert gates.route_intent(router, text, {})["intent"] == intent and gates.route_intent(router, text, {})["by"] == "rules"


def test_decision_chain_routes_and_low_confidence_asks_a_clarifying_question(engine, worker, store, monkeypatch):
    nid = built(engine, worker)
    seen = {}
    monkeypatch.setattr(engine.router, "decide", lambda g, s, q, **kw: (seen.update(kw=kw, gate=g), {"intent": {"type": "choice", "choice": "ask_doubt", "confidence": 0.3, "unsure": True, "_by": "gemma_26b"}})[1])
    res = ask(engine, nid, "hmm, chapter two?")
    assert res["type"] == "text" and "answer a question about your course, or change the course" in res["text"]
    assert seen["gate"] == "intent" and seen["kw"]["student_data"] is True                    # student messages never reach the free-tier model


def test_quiz_intent_opens_the_chapter_quiz_and_other_is_a_polite_redirect(engine, worker, store, monkeypatch):
    nid = built(engine, worker)
    res = ask(engine, nid, "quiz me on chapter 2")
    assert res["refs"][0]["quiz"] is True and res["refs"][0]["id"] == store.chapters(nid)[1]["id"] and "chapter 2" in res["text"]
    monkeypatch.setattr(engine.router, "decide", lambda g, s, q, **kw: {"intent": {"type": "choice", "choice": "other", "confidence": 0.9, "_by": "jev"}} if g == "intent" else None)
    assert "ask me a question about it" in ask(engine, nid, "how is the weather")["text"]


def test_explain_uses_the_explain_style(engine, worker, store):
    nid = built(engine, worker)
    seen = {}
    orig = engine.router.call
    engine.router.call = lambda role, prompt, *a, **k: (seen.update(prompt=prompt), orig(role, prompt, *a, **k))[1]
    ask(engine, nid, "explain this again, simpler")
    assert "EXPLAINED AGAIN" in seen["prompt"]


# ------------------------------------------------------------------------------------------ resources propose, never silently change
def test_a_resource_covering_new_topics_gets_a_proposal_and_a_covered_one_does_not(engine, worker, store):
    nid = built(engine, worker)
    src_id = store.add_source(nid, "text", "pasted", "streaming.pdf", "text", [])
    src = next(s for s in store.sources(nid) if s["id"] == src_id)
    n = len(store.chapters(nid))
    engine._propose_for_resource(nid, src, ["Streaming pipelines", "Watermarks"])
    p = store.proposals(nid)[0]
    assert p["source"] == "resource" and p["status"] == "pending" and len(store.chapters(nid)) == n
    assert any("also covers Streaming pipelines" in m["content"] for m in store.messages(nid))
    first = store.chapters(nid)[0]["title"]
    store.update_proposal(p["id"], status="dismissed")
    engine._propose_for_resource(nid, {**src, "id": "other"}, [first])
    assert len(store.proposals(nid)) == 1 and any("fits your current course" in m["content"] for m in store.messages(nid))


def test_endpoints_apply_undo_dismiss(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app(db_path=str(tmp_path / "e.db"), provider_override=FakeProvider(), start_worker=False)
    with TestClient(app) as cl:
        lid = cl.post("/api/learners").json()["learner_id"]
        nid = cl.post("/api/notebooks", data={"learner_id": lid, "text": "Teach me binary search", "language": "en"}).json()["notebook_id"]
        eng, store = app.state.engine, app.state.store
        while store.pending_jobs():
            eng.process_job(store.pending_jobs()[0]["id"])
        r = cl.post(f"/api/notebooks/{nid}/chat", data={"learner_id": lid, "text": "add a chapter on Delta Lake after chapter 2"})
        assert "event: proposal" in r.text
        pid = store.proposals(nid)[0]["id"]
        assert cl.get(f"/api/notebooks/{nid}/proposals/{pid}").json()["status"] == "pending"
        assert cl.post(f"/api/notebooks/{nid}/proposals/{pid}/apply").json()["can_undo"] is True
        assert cl.post(f"/api/notebooks/{nid}/proposals/{pid}/apply").status_code == 400
        assert cl.post(f"/api/notebooks/{nid}/proposals/{pid}/undo").json()["status"] == "undone"
        assert cl.post(f"/api/notebooks/{nid}/proposals/999/apply").status_code == 400
        msgs = cl.get(f"/api/notebooks/{nid}/messages").json()["messages"]
        assert [m for m in msgs if m["kind"] == "proposal"][0]["proposal"]["status"] == "undone"
