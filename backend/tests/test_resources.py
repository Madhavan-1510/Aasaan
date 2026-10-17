"""Resources view: what the student gave, what Aasaan found, where each is used; preview, re-process, remove with impact."""
import pytest
from fastapi.testclient import TestClient

from app.engine import UserError
from app.main import create_app
from fake_provider import FakeProvider


def notebook_with_material(engine, worker, sample_text):
    nid = engine.create_notebook("L1", sample_text, "en", [])
    worker.run_until_idle()
    return nid


def test_resources_view_lists_mine_with_summary_and_used_in(engine, worker, store, sample_text):
    nid = notebook_with_material(engine, worker, sample_text)
    v = engine.resources_view(nid)
    mine = v["mine"][0]
    assert mine["kind"] == "pasted" and mine["status"] == "ready" and mine["length"] == len(sample_text) and mine["summary"].startswith("Motion is the change")
    assert mine["used_in"] and {"id", "position", "title"} <= set(mine["used_in"][0])          # chapters built from it say so


def test_found_sources_are_separate_and_carry_licence_and_attribution(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    ch = store.chapters(nid)[0]
    blocks = store.blocks(ch["id"])
    store.replace_blocks(ch["id"], [b for b in blocks if b["type"] != "link"] + [
        {"type": "image", "verified": True, "content": {"url": "https://upload.wikimedia.org/a.png", "title": "A diagram", "attribution": {"author": "Jane", "license": "CC BY 4.0", "source": "Wikimedia Commons", "page_url": "https://commons.wikimedia.org/wiki/File:A.png"}}},
        {"type": "video", "verified": True, "content": {"video_id": "abc123", "title": "Binary search explained", "channel": "Some Channel", "url": "https://www.youtube.com/watch?v=abc123"}}], "ready", ch["meta"])
    found = engine.resources_view(nid)["found"]
    img = next(f for f in found if f["kind"] == "image")
    assert img["license"] == "CC BY 4.0" and img["author"] == "Jane" and img["used_in"][0]["id"] == ch["id"]
    assert next(f for f in found if f["kind"] == "video")["source"] == "Some Channel"
    assert engine.resources_view(nid)["mine"] == []                                          # a goal-only notebook has no student resources


def test_removing_warns_which_chapters_cite_it_and_can_rebuild_them(engine, worker, store, sample_text):
    nid = notebook_with_material(engine, worker, sample_text)
    sid = engine.resources_view(nid)["mine"][0]["id"]
    impact = engine.resource_impact(nid, sid)["cited_by"]
    assert impact
    out = engine.remove_resource(nid, sid, regenerate=True)
    assert out["regenerating"] == [c["id"] for c in impact] and store.sources(nid) == []
    assert all(store.get_chapter(c["id"])["status"] == "queued" for c in impact)
    assert not any(str(r).startswith(sid) for c in store.chapters(nid) for r in c["source_refs"])
    assert any(m["content"].startswith("Removed") and "Rebuilding" in m["content"] for m in store.messages(nid))
    worker.run_until_idle()
    assert all(store.get_chapter(c["id"])["status"] == "ready" for c in impact)


def test_removing_without_regenerate_leaves_chapters_alone(engine, worker, store, sample_text):
    nid = notebook_with_material(engine, worker, sample_text)
    sid = engine.resources_view(nid)["mine"][0]["id"]
    out = engine.remove_resource(nid, sid)
    assert out["regenerating"] == [] and all(c["status"] == "ready" for c in store.chapters(nid))


def test_reprocess_requeues_reading_and_unknown_ids_are_refused(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    [r] = engine.add_resources(nid, [("n.txt", "text/plain", b"Binary search halves the range each step. " * 20)], [])
    worker.run_until_idle()
    assert store.sources(nid)[-1]["meta"]["status"] == "ready"
    engine.reprocess_resource(nid, r["id"])
    assert store.sources(nid)[-1]["meta"]["status"] == "reading"
    worker.run_until_idle()
    assert store.sources(nid)[-1]["meta"]["status"] == "ready"
    with pytest.raises(UserError):
        engine.remove_resource(nid, "nope")


def test_pasted_text_becomes_a_resource(engine, worker, store):
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    out = engine.add_pasted_text(nid, "Notes about hashing. " * 40)
    worker.run_until_idle()
    mine = engine.resources_view(nid)["mine"][0]
    assert mine["kind"] == "pasted" and mine["status"] == "ready" and mine["id"] == out["id"]


def test_files_are_served_only_from_the_upload_folder(tmp_path, sample_text):
    app = create_app(db_path=str(tmp_path / "t.db"), provider_override=FakeProvider(), start_worker=False, upload_dir=str(tmp_path / "up"))
    with TestClient(app) as cl:
        lid = cl.post("/api/learners").json()["learner_id"]
        nid = cl.post("/api/notebooks", data={"learner_id": lid, "text": sample_text, "language": "en"}).json()["notebook_id"]
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
        r = cl.post(f"/api/notebooks/{nid}/resources", files={"files": ("pic.png", png, "image/png")})
        sid = r.json()["resources"][0]["id"]
        assert cl.get(f"/api/notebooks/{nid}/resources/{sid}/file").content == png
        assert cl.get(f"/api/notebooks/{nid}/resources/{sid}/thumb").status_code == 200
        # a source whose stored path points outside the upload folder is never served
        app.state.store._run("UPDATE sources SET raw_ref=? WHERE id=?", (str(tmp_path / "t.db"), sid))
        assert cl.get(f"/api/notebooks/{nid}/resources/{sid}/file").status_code == 404
        assert cl.get(f"/api/notebooks/{nid}/resources/zzz/preview").status_code == 404
        assert cl.get(f"/api/notebooks/{nid}/resources").json()["mine"][-1]["has_file"] is True
        assert cl.delete(f"/api/notebooks/{nid}/resources/{sid}").json()["removed"] == sid


def test_every_notebook_gets_one_of_the_four_thumbnails_and_it_never_changes(engine, worker, store):
    from pathlib import Path
    from app.engine import COVER_IMAGES, cover_image
    folder = Path(__file__).resolve().parents[2] / "frontend" / "resources"
    assert all((folder / n).is_file() for n in COVER_IMAGES) and len(COVER_IMAGES) == 4
    seen = {cover_image(f"nb_{i:04d}") for i in range(200)}
    assert seen == set(COVER_IMAGES)                                              # all four get used
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    first = engine.notebook_view(nid)["cover"]
    worker.run_until_idle()                                                       # the title changes when the outline arrives; the cover must not
    assert engine.notebook_view(nid)["cover"] == first == cover_image(nid)
    assert engine.list_notebooks("L1")[0]["cover"] == first


def test_mascot_files_exist_are_transparent_and_are_used_by_the_ui():
    from pathlib import Path
    from PIL import Image
    fe = Path(__file__).resolve().parents[2] / "frontend"
    for name in ("aasaan-mascot.png", "aasaan-avatar.png"):
        im = Image.open(fe / "mascot" / name)
        assert im.mode == "RGBA" and im.getpixel((2, 2))[3] == 0, name          # transparent corners: it sits on both themes
    js = (fe / "util.js").read_text(encoding="utf-8") + (fe / "chat.js").read_text(encoding="utf-8")
    assert "mascot/aasaan-avatar.png" in js and "mascot/aasaan-mascot.png" in js
    assert "mascot/aasaan-avatar.png" in (fe / "index.html").read_text(encoding="utf-8")   # favicon
