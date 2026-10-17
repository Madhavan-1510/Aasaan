"""Chat thread: code-written narration, streamed answers with source chips, resources added from chat, resumable delivery."""
import json

import pytest
from fastapi.testclient import TestClient

from app import web
from app.main import create_app
from fake_provider import FakeProvider


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(db_path=str(tmp_path / "t.db"), provider_override=FakeProvider(), start_worker=False, upload_dir=str(tmp_path / "up"))) as c:
        yield c


def events(resp_text: str) -> list[tuple[str, dict]]:
    out = []
    for block in resp_text.strip().split("\n\n"):
        lines = block.split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


def build(engine, worker, text="Teach me binary search"):
    nid = engine.create_notebook("L1", text, "en", [])
    worker.run_until_idle()
    return nid


def test_narration_is_fixed_wording_from_real_state_and_never_repeats(engine, worker, store):
    nid = build(engine, worker)
    msgs = store.messages(nid)
    texts = [m["content"] for m in msgs]
    assert any(t.startswith("Planning your course") for t in texts) and any("outline is ready" in t for t in texts)
    assert any(t.startswith("Chapter 1 is ready") and "start reading" in t for t in texts) and texts[-1].startswith("Your whole notebook is ready")
    assert all(m["role"] == "assistant" and m["kind"] == "system" for m in msgs)
    ch1 = [m for m in msgs if m["content"].startswith("Chapter 1 is ready")][0]
    assert ch1["refs"][0]["kind"] == "chapter"
    n = len(msgs)
    engine._refresh_status(nid)                          # a retried or repeated step does not narrate twice
    engine._announce_chapter(engine.store.chapters(nid)[0], True)
    assert len(store.messages(nid)) == n


def test_messages_are_delivered_by_cursor_so_a_reconnect_loses_nothing(engine, worker, store, client):
    nid = build(engine, worker)
    all_ = store.messages(nid)
    mid = all_[2]["id"]
    assert [m["id"] for m in store.messages(nid, after=mid)] == [m["id"] for m in all_ if m["id"] > mid]
    assert engine.notebook_view(nid)["message_seq"] == all_[-1]["id"]


def test_chat_answer_streams_validated_text_and_stores_it_with_chips(tmp_path, sample_text):
    app = create_app(db_path=str(tmp_path / "y.db"), provider_override=FakeProvider(), start_worker=False)
    with TestClient(app) as cl:
        lid = cl.post("/api/learners").json()["learner_id"]
        nid = cl.post("/api/notebooks", data={"learner_id": lid, "text": sample_text, "language": "en"}).json()["notebook_id"]
        store, engine = app.state.store, app.state.engine
        engine.process_job(store.pending_jobs()[0]["id"])
        r = cl.post(f"/api/notebooks/{nid}/chat", data={"learner_id": lid, "text": "What is velocity?"})
        ev = events(r.text)
        names = [e[0] for e in ev]
        assert names[0] == "user" and "status" in names and names.count("delta") >= 1 and names[-1] == "done"
        streamed = "".join(p["text"] for n, p in ev if n == "delta")
        stored = store.messages(nid)
        assert stored[-1]["role"] == "assistant" and stored[-1]["kind"] == "chat" and stored[-1]["content"].strip() == streamed.strip()
        assert stored[-2]["role"] == "user" and stored[-2]["content"] == "What is velocity?"
        refs = ev[-1][1]["refs"]
        assert refs and {r["kind"] for r in refs} <= {"chapter", "resource", "link"} and all(r["label"] for r in refs)
        assert any(r["kind"] == "resource" and r["id"] in {s["id"] for s in store.sources(nid)} for r in refs)
        # a message that is only a link or a file adds a resource and does not ask the answerer anything
        calls = len([c for c in engine.router.fake.calls if c[0] == "doubt"])
        r = cl.post(f"/api/notebooks/{nid}/chat", data={"learner_id": lid, "text": "https://example.com/streaming-guide"})
        assert [e[0] for e in events(r.text)] == ["user", "done"]
        assert len([c for c in engine.router.fake.calls if c[0] == "doubt"]) == calls
        assert store.sources(nid)[-1]["kind"] == "url" and store.sources(nid)[-1]["meta"]["status"] == "reading"
        r = cl.post(f"/api/notebooks/{nid}/chat", data={"learner_id": lid, "text": ""}, files={"files": ("x.exe", b"1", "application/octet-stream")})
        assert r.status_code == 400 and "only PDFs" in r.json()["detail"]


def test_block_context_reaches_the_answerer(engine, worker, store):
    nid = build(engine, worker)
    ch = engine.store.chapters(nid)[0]
    notes = next(b for b in store.blocks(ch["id"]) if b["type"] == "notes")
    seen = {}
    orig = engine.router.call
    engine.router.call = lambda role, prompt, *a, **k: (seen.update(prompt=prompt), orig(role, prompt, *a, **k))[1]
    engine.chat_reply(nid, "L1", "Explain this part", ch["id"], [notes["id"]])
    assert "asking about this part of the course" in seen["prompt"]


def test_links_and_files_in_chat_become_resources_and_are_read_in_the_background(engine, worker, store, tmp_path):
    nid = build(engine, worker)
    out = engine.add_resources(nid, [("notes.txt", "text/plain", b"Streaming pipelines process events as they arrive. " * 20)], [])
    assert out[0]["status"] == "reading" and store.sources(nid)[-1]["meta"]["status"] == "reading"
    assert any(m["content"].startswith('Got "notes.txt"') for m in store.messages(nid))
    chapters_before = [c["id"] for c in store.chapters(nid)]
    worker.run_until_idle()
    src = store.sources(nid)[-1]
    assert src["meta"]["status"] == "ready" and src["chunks"]
    assert any(m["content"].startswith("Finished reading") for m in store.messages(nid))
    assert [c["id"] for c in store.chapters(nid)] == chapters_before        # reading a resource never changes the course by itself


def test_bad_resources_are_refused_before_anything_is_stored(engine, worker, store, monkeypatch):
    from app.engine import UserError
    nid = build(engine, worker)
    n = len(store.messages(nid))
    for files in ([("x.exe", "application/octet-stream", b"1")], [("big.pdf", "application/pdf", b"0" * (16 * 1024 * 1024))]):
        with pytest.raises(UserError):
            engine.validate_resources(nid, files, [])
    monkeypatch.setattr(web, "_resolve", lambda host, port: [(2, 1, 6, "", ("127.0.0.1", port))])
    with pytest.raises(UserError):
        engine.validate_resources(nid, [], ["http://internal.example/admin"])
    assert len(store.messages(nid)) == n and len(store.sources(nid)) == 0
