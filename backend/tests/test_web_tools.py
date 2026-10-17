"""The web tools now run through the ToolRunner: events, retries, breakers, classification (no behaviour change otherwise)."""
import pytest

from app import web
from app.db import Store
from app.resilience import EventSink
from app.tools import Chaos, ToolRunner
from conftest import FakeResp
from test_web import PARAS


@pytest.fixture
def runner(monkeypatch):
    store = Store(":memory:")
    r = ToolRunner(store, EventSink(store), Chaos(""))
    r.sleep = lambda s: None
    monkeypatch.setattr(web, "RUNNER", r)
    r.store_ = store
    return r


WIKI = lambda hits: FakeResp(200, data=["q", [h[0] for h in hits], [""] * len(hits), [h[1] for h in hits]])


def test_wikipedia_search_is_logged_cached_and_retried(runner, fake_web):
    replies = [FakeResp(503, "unavailable"), WIKI([("Binary search", "https://en.wikipedia.org/wiki/Binary_search")])]
    fake_web.get(lambda u, p: replies.pop(0) if "wikipedia.org/w/api.php" in u else None)
    cands = web.discover_wikipedia("binary search")
    assert [c.url for c in cands] == ["https://en.wikipedia.org/wiki/Binary_search"]
    ev = [e for e in runner.store_.events() if e["agent"] == "tool:wikipedia"]
    assert [e["status"] for e in reversed(ev)] == ["retry", "ok"] and "[server]" in ev[-1]["error"]
    calls = len(fake_web.calls)
    assert [c.url for c in web.discover_wikipedia("binary search")] == [cands[0].url] and len(fake_web.calls) == calls  # cache hit
    assert any("cache hit" in e["summary"] for e in runner.store_.events())


def test_failing_commons_degrades_to_no_images_with_a_reason(runner, fake_web):
    fake_web.get(lambda u, p: FakeResp(500, "boom") if "commons.wikimedia.org" in u else None)
    assert web.discover_images("binary tree") == []
    ev = [e for e in runner.store_.events() if e["agent"] == "tool:commons"]
    assert ev and all("[server]" in e["error"] for e in ev if e["status"] != "ok")
    assert runner.health()["commons"]["state"] == "degraded"


def test_a_404_video_is_dropped_without_blaming_the_oembed_tool(runner, fake_web):
    fake_web.get(lambda u, p: FakeResp(404, "not found") if u == "https://www.youtube.com/oembed" else None)
    for vid in ("AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC", "DDDDDDDDDDD"):
        c = web.verify_video(web.Candidate("v", "video", f"https://youtu.be/{vid}"))
        assert not c.verified and "oEmbed [invalid]" in c.extra["drop"]
    assert runner.health()["oembed"]["state"] == "ok"      # content problems are not an outage: breaker stays closed


def test_jina_is_the_second_tier_and_a_thin_page_is_not_an_outage(runner, fake_web, monkeypatch):
    fake_web.get(lambda u, p: FakeResp(200, "<html><body><p>hi</p></body></html>") if u == "https://spa.example/x" else None)
    fake_web.get(lambda u, p: FakeResp(200, "Title: T\n\nMarkdown Content:\n" + " ".join(PARAS), headers={"content-type": "text/plain"})
                 if u.startswith("https://r.jina.ai/") else None)
    r = web.fetch_page("https://spa.example/x")
    assert r.ok and r.tier == 1
    assert any(e["agent"] == "tool:jina" and e["status"] == "ok" for e in runner.store_.events())
    fake_web.get(lambda u, p: FakeResp(200, "short", headers={"content-type": "text/plain"}) if u.startswith("https://r.jina.ai/") else None)
    bad = web.fetch_page("https://spa.example/y")
    assert not bad.ok and "tier1 jina: [thin]" in bad.error and runner.health()["jina"]["state"] == "ok"


def test_jina_outage_trips_its_own_breaker_only(runner, fake_web):
    fake_web.get(lambda u, p: FakeResp(200, "<html><body><p>hi</p></body></html>") if u.startswith("https://spa") else None)
    fake_web.get(lambda u, p: FakeResp(503, "down") if u.startswith("https://r.jina.ai/") else None)
    for i in range(4):
        web.fetch_page(f"https://spa.example/{i}")
    assert runner.health()["jina"]["state"] == "open_circuit"
    last = web.fetch_page("https://spa.example/last")
    assert "circuit_open" in last.error


def test_chaos_can_kill_a_tool_for_acceptance_checks(runner, fake_web):
    runner.chaos.set("tool:wikipedia=fail")
    assert web.discover_wikipedia("anything") == []
    assert fake_web.calls == []                                            # never reached the network
    assert any("chaos" in e["error"] for e in runner.store_.events())
