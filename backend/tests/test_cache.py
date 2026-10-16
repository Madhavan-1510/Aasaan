"""Pre-warmed demo cache: keyed by source content, so it still hits on a fresh upload."""
from app.agents.common import fingerprint
from app.db import Store
from app.orchestrator import Engine
from app.router import ModelRouter


def test_fingerprint_ignores_chunk_ids():
    a = [{"chunk_id": "s1-p1-c1", "page": 1, "text": "Same text."}]
    b = [{"chunk_id": "s9-p1-c1", "page": 1, "text": "Same text."}]
    assert fingerprint(a) == fingerprint(b)
    assert fingerprint(a) != fingerprint([{"chunk_id": "x", "page": 1, "text": "Other."}])


def test_cached_lesson_served_on_fresh_upload_when_all_models_dead(sample_text):
    # warm: approved lesson and quiz land in the cache
    s1 = Store(":memory:")
    sw1 = Engine(s1, ModelRouter(store=s1, mode="mock"))
    lid = sw1.create_learner(9, "en", None)
    out = sw1.start(lid, None, sample_text, None, None, "en", None)
    pid, nid = out["path"]["path_id"], out["path"]["current"]
    warm = sw1.lesson(lid, pid, nid)["lesson"]
    sw1.quiz(lid, pid, nid)
    dump = s1.cache_dump()
    assert any(k.startswith("lesson:") for k in dump) and any(k.startswith("quiz:") for k in dump)

    # demo: brand new store/source id, cache loaded, every model dead
    s2 = Store(":memory:")
    s2.cache_load(dump)
    r2 = ModelRouter(store=s2, mode="mock")
    sw2 = Engine(s2, r2)
    for m in r2.cfg["models"]:
        r2.kill(m)
    lid2 = sw2.create_learner(9, "en", None)
    out2 = sw2.start(lid2, None, sample_text, None, None, "en", None)
    got = sw2.lesson(lid2, out2["path"]["path_id"], out2["path"]["current"])["lesson"]
    assert got["steps"] == warm["steps"]
