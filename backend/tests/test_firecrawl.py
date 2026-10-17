"""Firecrawl: last page tier, discovery fallback, per-notebook cap, switched off after 401/402."""
import pytest

from app import web
from app.agents import curator
from app.db import Store
from app.resilience import EventSink
from app.tools import Chaos, ToolRunner
from conftest import FakeResp
from test_web import ARTICLE, PARAS

BODY = " ".join(PARAS)


@pytest.fixture
def fc(monkeypatch):
    """A ToolRunner with events, FIRECRAWL key set, and a recording fake for POST https://api.firecrawl.dev."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test-key-123456")
    store = Store(":memory:")
    runner = ToolRunner(store, EventSink(store), Chaos(""), rate_scale=1000)
    runner.sleep = lambda s: None
    monkeypatch.setattr(web, "RUNNER", runner)
    posts, replies = [], []

    def post(url, *, headers=None, json=None, timeout=0):
        posts.append({"url": url, "headers": headers, "json": json})
        return replies.pop(0) if replies else FakeResp(200, data={"success": True, "data": {"markdown": BODY, "metadata": {"title": "T"}}})

    monkeypatch.setattr(web, "http_post", post)
    runner.posts, runner.replies, runner.store_ = posts, replies, store
    return runner


def js_only(fake_web, host="https://spa.example"):
    fake_web.get(lambda u, p: FakeResp(200, "<html><body><p>hi</p></body></html>") if u.startswith(host) else None)
    fake_web.get(lambda u, p: FakeResp(503, "down") if u.startswith("https://r.jina.ai/") else None)


def test_firecrawl_is_not_called_when_cheaper_tiers_work(fc, fake_web):
    fake_web.get(lambda u, p: FakeResp(200, ARTICLE) if u == "https://ex.com/a" else None)
    assert web.fetch_page("https://ex.com/a").tier == 0
    fake_web.get(lambda u, p: FakeResp(200, "<html><body><p>hi</p></body></html>") if u == "https://spa.example/b" else None)
    fake_web.get(lambda u, p: FakeResp(200, "Title: T\n\nMarkdown Content:\n" + BODY, headers={"content-type": "text/plain"})
                 if u.startswith("https://r.jina.ai/") else None)
    assert web.fetch_page("https://spa.example/b").tier == 1
    assert fc.posts == []                                            # tier 2 never touched


def test_firecrawl_is_the_last_tier_and_sends_the_documented_request(fc, fake_web):
    js_only(fake_web)
    r = web.fetch_page("https://spa.example/page")
    assert r.ok and r.tier == 2 and r.title == "T" and r.text.startswith("Binary search")
    p = fc.posts[0]
    assert p["url"] == "https://api.firecrawl.dev/v2/scrape" and p["headers"] == {"Authorization": "Bearer fc-test-key-123456"}
    assert p["json"] == {"url": "https://spa.example/page", "formats": ["markdown"], "onlyMainContent": True}


@pytest.mark.parametrize("code", [401, 402])
def test_firecrawl_stops_for_the_session_after_401_or_402(fc, fake_web, code):
    js_only(fake_web)
    fc.replies.append(FakeResp(code, "nope"))
    first = web.fetch_page("https://spa.example/1")
    assert not first.ok and "[auth]" in first.error
    web.fetch_page("https://spa.example/2")
    web.discover_firecrawl("binary search")
    assert len(fc.posts) == 1                                        # never called again, for scraping or searching
    assert fc.health()["firecrawl"]["state"] == "disabled" and f"HTTP {code}" in fc.health()["firecrawl"]["reason"]
    assert any("disabled for this session" in e["error"] for e in fc.store_.events())


def test_firecrawl_never_exceeds_the_per_notebook_cap(fc, fake_web, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_MAX_CALLS_PER_NOTEBOOK", "3")
    js_only(fake_web)
    with fc.notebook_scope("nb-1"):
        for i in range(6):
            web.fetch_page(f"https://spa.example/{i}")
        web.discover_firecrawl("binary search")
    assert len(fc.posts) == 3
    assert any("per-notebook limit of 3" in e["error"] for e in fc.store_.events())
    with fc.notebook_scope("nb-2"):                                   # a different notebook has its own allowance
        web.fetch_page("https://spa.example/other")
    assert len(fc.posts) == 4


def test_default_cap_is_20(fc, fake_web):
    js_only(fake_web)
    with fc.notebook_scope("nb"):
        for i in range(25):
            web.fetch_page(f"https://spa.example/{i}")
    assert len(fc.posts) == 20


def test_discovery_fallback_uses_v2_search_and_parses_data_web(fc):
    fc.replies.append(FakeResp(200, data={"success": True, "data": {"web": [
        {"url": "https://ex.com/guide", "title": "Binary search guide", "description": "How binary search halves the range."},
        {"url": "https://ex.com/two", "title": "Two"}, {"title": "no url"}]}}))
    cands = web.discover_firecrawl("binary search", n=5)
    assert [c.url for c in cands] == ["https://ex.com/guide", "https://ex.com/two"] and cands[0].reason == "How binary search halves the range."
    p = fc.posts[0]
    assert p["url"] == "https://api.firecrawl.dev/v2/search" and p["json"] == {"query": "binary search", "limit": 5, "sources": [{"type": "web"}]}
    web.discover_firecrawl("binary search", n=5)                      # cached: no second POST
    assert len(fc.posts) == 1


def test_curator_uses_firecrawl_search_only_when_discovery_found_too_little(fc, router, store, fake_web):
    fc.replies.append(FakeResp(200, data={"data": {"web": [{"url": "https://ex.com/guide", "title": "Binary search guide", "description": "d"}]}}))
    fake_web.get(lambda u, p: FakeResp(200, ARTICLE) if u == "https://ex.com/guide" else None)
    out = curator.curate(router, store, "Binary search", ["x"], "algorithms")        # nothing else found (offline fake) -> fallback fires
    assert [x.url for x in out.links] == ["https://ex.com/guide"] and len(fc.posts) == 1
    fc.posts.clear()
    fake_web.get(lambda u, p: FakeResp(200, data=["q", [f"Binary search {i}" for i in range(4)], [""] * 4,
                                                  [f"https://en.wikipedia.org/wiki/B{i}" for i in range(4)]])
                 if "wikipedia.org/w/api.php" in u else None)
    fake_web.get(lambda u, p: FakeResp(200, ARTICLE) if u.startswith("https://en.wikipedia.org/wiki/") else None)
    curator.curate(router, store, "Binary search trees", ["x"], "algorithms")        # plenty found: Firecrawl untouched
    assert fc.posts == []


def test_firecrawl_without_a_key_is_never_called(monkeypatch, fake_web):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    js_only(fake_web)
    r = web.fetch_page("https://spa.example/x")
    assert not r.ok and "firecrawl" not in r.error            # unconfigured is silent in the user-facing message, visible in events and health
