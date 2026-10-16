"""End-to-end learning loop in mock mode (no API keys)."""


def start(swarm, text, lid=None):
    lid = lid or swarm.create_learner(9, "en", "exam on Friday")
    return lid, swarm.start(lid, None, text, None, None, "en", "exam on Friday")


def test_start_builds_valid_path(swarm, sample_text):
    lid, out = start(swarm, sample_text)
    nodes = out["path"]["nodes"]
    assert 1 <= len(nodes) <= 8 and out["path"]["current"] == nodes[0]["id"]
    assert all(n["source_refs"] for n in nodes)


def test_lesson_critic_loop_revises_then_approves(swarm, sample_text):
    lid, out = start(swarm, sample_text)
    pid, nid = out["path"]["path_id"], out["path"]["current"]
    issue = {"verdict": "revise", "issues": [{"step": 1, "problem": "unsupported claim"}], "confidence": 0.7}
    swarm.router.mock.script("critic", [issue])  # first review sends it back, then default approve
    res = swarm.lesson(lid, pid, nid)
    assert res["verified"] and res["revisions"] == 1
    assert 3 <= len(res["lesson"]["steps"]) <= 5


def test_lesson_ships_unverified_after_two_revisions(swarm, sample_text):
    lid, out = start(swarm, sample_text)
    pid, nid = out["path"]["path_id"], out["path"]["current"]
    issue = {"verdict": "revise", "issues": [{"step": 1, "problem": "bad"}], "confidence": 0.7}
    swarm.router.mock.script("critic", [issue, issue, issue])
    res = swarm.lesson(lid, pid, nid)
    assert not res["verified"] and res["revisions"] == 2


def test_wrong_answer_adds_review_step_and_session_two_remembers(swarm, sample_text):
    lid, out = start(swarm, sample_text)
    pid, nid, sid = out["path"]["path_id"], out["path"]["current"], out["session_id"]
    swarm.lesson(lid, pid, nid)
    quiz = swarm.quiz(lid, pid, nid)
    assert "answer_key" not in quiz["questions"][0]
    before = len(out["path"]["nodes"])
    q0 = quiz["questions"][0]
    fb = swarm.answer(lid, sid, pid, nid, q0["id"], q0["options"][1])  # wrong option
    assert fb["result"] == "wrong" and fb["misconception"] and fb["hint"]
    fin = swarm.finish_node(lid, sid, pid, nid)
    assert len(fin["path"]["nodes"]) == before + 1
    assert any(n["title"].startswith("Review:") for n in fin["path"]["nodes"])
    swarm.close_session(lid, sid, pid)
    s2 = swarm.open_session(lid)  # session 2
    label = swarm.store.mistake_labels(lid)[0]
    assert s2["returning"] and label in s2["message"]


def test_correct_answer_needs_no_model_and_no_rewrite(swarm, sample_text):
    lid, out = start(swarm, sample_text)
    pid, nid, sid = out["path"]["path_id"], out["path"]["current"], out["session_id"]
    swarm.lesson(lid, pid, nid)
    swarm.quiz(lid, pid, nid)
    full = swarm.store.get_quiz(pid, nid)
    for q in full["questions"]:
        r = swarm.answer(lid, sid, pid, nid, q["id"], q["answer_key"])
        assert r["result"] == "correct"
    n_nodes = len(out["path"]["nodes"])
    assert len(swarm.finish_node(lid, sid, pid, nid)["path"]["nodes"]) == n_nodes
    assert not any(r == "diagnoser" for r, _ in swarm.router.mock.calls)


def test_whole_flow_survives_dead_primary_and_total_model_outage(swarm, sample_text):
    swarm.router.kill("gemma_big")
    lid, out = start(swarm, sample_text)  # served by gemma_small
    swarm.router.kill("gemma_small")
    swarm.router.kill("jev_fast")  # every model dead: only code-based last resorts remain
    pid, nid, sid = out["path"]["path_id"], out["path"]["current"], out["session_id"]
    les = swarm.lesson(lid, pid, nid)
    assert les["lesson"]["steps"]
    quiz = swarm.quiz(lid, pid, nid)
    q = quiz["questions"][0]
    fb = swarm.answer(lid, sid, pid, nid, q["id"], q["options"][-1])
    assert fb["result"] == "wrong"
    fin = swarm.finish_node(lid, sid, pid, nid)
    assert fin["next_node"]
    assert swarm.close_session(lid, sid, pid)["message"]
    models = {e["model_used"] for e in swarm.store.events()}
    assert {"cached_lesson", "cached_questions", "exact_match", "template_message"} <= models


def test_new_path_gets_review_node_from_memory(swarm, sample_text):
    lid = swarm.create_learner(9, "en", None)
    swarm.store.add_mistake(lid, "confuses speed and velocity")
    _, out = start(swarm, sample_text, lid)
    assert out["path"]["nodes"][0]["title"].startswith("Review: confuses speed")


def test_doubt_text_only_without_voice(swarm, sample_text):
    lid, out = start(swarm, sample_text)
    r = swarm.doubt(lid, out["path"]["path_id"], None, "What is velocity?", None)
    assert r["answer"] and r["audio_b64"] is None
    v = swarm.doubt(lid, out["path"]["path_id"], None, None, b"fakeaudio")
    assert v.get("needs_text")


def test_wrong_then_right_after_hint_still_counts_as_a_gap(swarm, sample_text):
    lid, out = start(swarm, sample_text)
    pid, nid, sid = out["path"]["path_id"], out["path"]["current"], out["session_id"]
    swarm.lesson(lid, pid, nid)
    swarm.quiz(lid, pid, nid)
    full = swarm.store.get_quiz(pid, nid)
    q0 = full["questions"][0]
    swarm.answer(lid, sid, pid, nid, q0["id"], q0["options"][-1], attempt=1)     # wrong
    swarm.answer(lid, sid, pid, nid, q0["id"], q0["answer_key"], attempt=2)      # right after the hint
    for q in full["questions"][1:]:
        swarm.answer(lid, sid, pid, nid, q["id"], q["answer_key"])
    fin = swarm.finish_node(lid, sid, pid, nid)
    assert fin["score"] == {"correct": len(full["questions"]) - 1, "total": len(full["questions"])}
    assert len(fin["path"]["nodes"]) == len(out["path"]["nodes"]) + 1


def test_course_meta_and_courses_list(swarm, sample_text):
    lid, out = start(swarm, sample_text)
    p = out["path"]
    assert p["course"]["title"] == "exam on Friday" and p["course"]["description"]  # goal passed to start()
    lid2 = swarm.create_learner(9, "en", "old goal")
    out2 = swarm.start(lid2, None, sample_text, None, None, "en", None)
    assert out2["path"]["course"]["title"] != "old goal"  # a stale profile goal must not title a new course
    assert p["progress"] == {"done": 0, "total": len(p["nodes"])}
    lst = swarm.list_courses(lid)
    assert len(lst) == 1 and lst[0]["path_id"] == p["path_id"] and lst[0]["current"] == p["current"]
