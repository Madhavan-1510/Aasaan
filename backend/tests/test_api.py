from fastapi.testclient import TestClient

from app.main import create_app


def test_api_roundtrip(sample_text, tmp_path):
    c = TestClient(create_app(db_path=str(tmp_path / "t.db"), mode="mock"))
    assert c.get("/api/health").json()["mode"] == "mock"
    lid = c.post("/api/learners", json={"grade": 9, "language": "en"}).json()["learner_id"]
    st = c.post("/api/start", data={"learner_id": lid, "text": sample_text, "language": "en"}).json()
    pid, nid, sid = st["path"]["path_id"], st["path"]["current"], st["session_id"]
    assert c.post("/api/lesson", json={"learner_id": lid, "path_id": pid, "node_id": nid}).json()["lesson"]
    q = c.post("/api/quiz", json={"learner_id": lid, "path_id": pid, "node_id": nid}).json()["questions"][0]
    r = c.post("/api/answer", json={"learner_id": lid, "path_id": pid, "node_id": nid, "session_id": sid,
                                    "question_id": q["id"], "answer": q["options"][-1]}).json()
    assert r["result"] == "wrong"
    assert c.get("/api/events").json()
    assert c.post("/api/admin/kill", json={"model": "gemma_big"}).json()["killed"] == ["gemma_big"]
    assert c.post("/api/start", data={"learner_id": lid}).status_code == 400  # nothing uploaded
