"""Curator: discovery -> code verification -> Jev relevance gate (Gemma fallback, then search order)."""
import pytest

from app import web
from app.agents import curator
from app.providers.base import ProviderError
from app.router import ModelRouter
from conftest import FakeResp
from test_web import ARTICLE


def cands(*urls):
    return [web.Candidate(f"l{i}", "link", u, f"Title {i}", "ex.com", "about it") for i, u in enumerate(urls)]


@pytest.fixture
def live(store, monkeypatch):
    return ModelRouter(store=store, mode="live")


def test_jev_gate_is_one_batched_call_keeping_probable_yes(live, monkeypatch):
    seen = {}

    def fake(model, state, questions, timeout_s=15):
        seen.update(state=state, questions=questions, n=seen.get("n", 0) + 1)
        return {"l0": {"type": "noul", "noul": 0.92}, "l1": {"type": "noul", "noul": 0.31}, "l2": {"type": "noul", "noul": 0.6}}

    monkeypatch.setattr(live.jev, "systemone", fake)
    kept = curator.gate(live, "Binary search", ["Explain halving"], cands("https://a/1", "https://a/2", "https://a/3"))
    assert [c.id for c in kept] == ["l0", "l2"]  # 0.6 passes the threshold, 0.31 does not
    assert seen["n"] == 1 and set(seen["questions"]) == {"l0", "l1", "l2"}
    assert all(q["type"] == "noul" for q in seen["questions"].values())
    assert seen["state"]["chapter"] == "Binary search" and set(seen["state"]["resources"]) == {"l0", "l1", "l2"}


def test_gate_falls_back_to_gemma_when_jev_is_down(live, store, monkeypatch):
    monkeypatch.setattr(live.jev, "systemone", lambda *a, **k: (_ for _ in ()).throw(ProviderError("jev HTTP 529")))
    monkeypatch.setattr(live.providers["gemini_api"], "generate", lambda *a, **k: '{"relevant": ["l1", "zzz"]}')
    kept = curator.gate(live, "Binary search", [], cands("https://a/1", "https://a/2"))
    assert [c.id for c in kept] == ["l1"]
    assert any("529" in e["error"] for e in store.events())  # why Jev was skipped is recorded


def test_gate_last_resort_keeps_search_order(live, monkeypatch):
    monkeypatch.setattr(live.jev, "systemone", lambda *a, **k: (_ for _ in ()).throw(ProviderError("down")))
    for m in live.cfg["models"]:
        if m != "jev":
            live.kill(m)
    kept = curator.gate(live, "x", [], cands("https://a/1", "https://a/2"))
    assert [c.id for c in kept] == ["l0", "l1"]


def test_curate_drops_dead_links_caps_results_and_returns_grounding_text(router, store, fake_web):
    urls = [f"https://good{i}.example/page" for i in range(6)]
    fake_web.get(lambda u, p: FakeResp(200, data=["q", [f"Binary search page {i}" for i in range(6)], [""] * 6, urls + ["https://dead.example/x"]])
                 if "wikipedia.org/w/api.php" in u else None)
    fake_web.get(lambda u, p: FakeResp(200, ARTICLE) if u.startswith("https://good") else FakeResp(404, "no") if "dead.example" in u else None)
    out = curator.curate(router, store, "Binary search", ["Explain halving"], "algorithms")
    assert 0 < len(out.links) <= curator.MAX_LINKS
    assert all(c.verified and "dead.example" not in c.url for c in out.links)
    assert out.pages and out.pages[0]["chunk_id"].startswith("w") and len(out.pages[0]["text"]) > 300
    assert out.videos == [] and out.notes  # explained, not silent


def test_grounded_search_results_that_are_youtube_become_verified_videos(live, store, fake_web, monkeypatch):
    monkeypatch.setattr(live.providers["gemini_api"], "grounded_search", lambda model, prompt, timeout_s=30: [
        {"uri": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "title": "youtube.com"},
        {"uri": "https://good.example/bs", "title": "good.example"}])
    monkeypatch.setattr(live.jev, "systemone", lambda model, state, q, timeout_s=15: {k: {"type": "noul", "noul": 0.9} for k in q})
    fake_web.get(lambda u, p: FakeResp(200, data={"title": "Binary Search in 10 min", "author_name": "CS Channel"})
                 if u == "https://www.youtube.com/oembed" else None)
    fake_web.get(lambda u, p: FakeResp(200, ARTICLE) if u == "https://good.example/bs" else None)
    out = curator.curate(live, store, "Binary search", ["x"], "algorithms")
    assert [v.extra["video_id"] for v in out.videos] == ["dQw4w9WgXcQ"] and out.videos[0].title == "Binary Search in 10 min"
    assert [l.url for l in out.links] == ["https://good.example/bs"]


def test_off_topic_wikipedia_hits_are_dropped_before_the_gate(router, store, fake_web):
    fake_web.get(lambda u, p: FakeResp(200, data=["q", ["Introduction to quantum mechanics", "Data structure"], ["", ""],
                 ["https://en.wikipedia.org/wiki/Introduction_to_quantum_mechanics", "https://en.wikipedia.org/wiki/Data_structure"]])
                 if "wikipedia.org/w/api.php" in u else None)
    fake_web.get(lambda u, p: FakeResp(200, ARTICLE) if u.startswith("https://en.wikipedia.org/wiki/") else None)
    out = curator.curate(router, store, "Introduction to data structures", ["x"], "data structures")
    assert [l.url for l in out.links] == ["https://en.wikipedia.org/wiki/Data_structure"]
    assert curator.topic_tokens("Introduction to DSA") == {"dsa"}


def test_off_topic_images_are_dropped(router, store, fake_web):
    def img(i, title, desc):
        return {"index": i, "title": f"File:{title}", "imageinfo": [{"thumburl": f"https://upload.wikimedia.org/{i}.png", "mime": "image/png", "width": 900,
                "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{i}", "extmetadata": {
                    "Artist": {"value": "Ann"}, "LicenseShortName": {"value": "CC0"}, "ImageDescription": {"value": desc}}}]}
    data = {"query": {"pages": {"1": img(1, "Predator drone.jpg", "A military aircraft"),
                                 "2": img(2, "Binary tree.png", "A binary search tree data structure")}}}
    fake_web.get(lambda u, p: FakeResp(200, data=data) if "commons.wikimedia.org/w/api.php" in u else None)
    fake_web.get(lambda u, p: FakeResp(206, "", headers={"content-type": "image/png"}) if u.startswith("https://upload.wikimedia.org/") else None)
    out = curator.curate(router, store, "Binary search trees", ["x"], "data structures")
    assert [i.title for i in out.images] == ["Binary tree.png"]


def test_lead_image_of_a_linked_wikipedia_page_with_commons_attribution(router, store, fake_web):
    page = "https://en.wikipedia.org/wiki/Binary_search"
    fake_web.get(lambda u, p: FakeResp(200, data=["q", ["Binary search"], [""], [page]]) if "wikipedia.org/w/api.php" in u else None)
    fake_web.get(lambda u, p: FakeResp(200, ARTICLE) if u == page else None)
    fake_web.get(lambda u, p: FakeResp(200, data={"originalimage": {"source": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Binary_Search_Depiction.svg.png"}})
                 if "/api/rest_v1/page/summary/Binary_search" in u else None)
    fake_web.get(lambda u, p: FakeResp(200, data={"query": {"pages": {"9": {"imageinfo": [{
        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/x.png/800px-x.png", "mime": "image/png", "width": 900,
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Binary_Search_Depiction.svg.png",
        "extmetadata": {"Artist": {"value": "<a>Ann Author</a>"}, "LicenseShortName": {"value": "CC BY-SA 4.0"}}}]}}}})
        if "commons.wikimedia.org/w/api.php" in u and (p or {}).get("titles") else None)
    fake_web.get(lambda u, p: FakeResp(206, "", headers={"content-type": "image/png"}) if u.startswith("https://upload.wikimedia.org/") else None)
    out = curator.curate(router, store, "Binary search", ["x"], "algorithms")
    lead = [i for i in out.images if i.extra.get("lead_of") == page]
    assert lead and lead[0].extra["author"] == "Ann Author" and lead[0].extra["license"] == "CC BY-SA 4.0"
    assert lead[0].extra["page_url"].endswith("File:Binary_Search_Depiction.svg.png")


def test_local_wikipedia_images_without_a_free_licence_are_skipped(fake_web):
    fake_web.get(lambda u, p: FakeResp(200, data={"originalimage": {"source": "https://upload.wikimedia.org/wikipedia/en/a/ab/Logo.png"}})
                 if "rest_v1/page/summary" in u else None)
    assert web.wikipedia_lead_image("https://en.wikipedia.org/wiki/Something") is None
    assert web.wikipedia_lead_image("https://example.com/wiki/Something") is None


def test_grounded_redirect_that_lands_on_youtube_becomes_a_verified_video(live, store, fake_web, monkeypatch):
    monkeypatch.setattr(live.providers["gemini_api"], "grounded_search", lambda model, prompt, timeout_s=30: [
        {"uri": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ1", "title": "youtube.com"}])
    monkeypatch.setattr(live.jev, "systemone", lambda model, state, q, timeout_s=15: {k: {"type": "noul", "noul": 0.9} for k in q})
    fake_web.get(lambda u, p: FakeResp(200, "<html><title>video</title></html>", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")
                 if u.startswith("https://vertexaisearch.cloud.google.com/") else None)
    fake_web.get(lambda u, p: FakeResp(200, data={"title": "Binary Search", "author_name": "CS"}) if u == "https://www.youtube.com/oembed" else None)
    out = curator.curate(live, store, "Binary search", ["x"], "algorithms")
    assert [v.extra["video_id"] for v in out.videos] == ["dQw4w9WgXcQ"] and out.links == []
