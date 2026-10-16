"""Web access: SSRF guard, tiered page reading, robots, resource discovery and verification (all offline)."""
import pytest

from app import web
from conftest import FakeResp

PARAS = [
    "Binary search finds an item in a sorted list by repeatedly halving the range that could contain it.",
    "At every step the algorithm compares the middle element with the target and discards half of the remaining elements.",
    "Because the range halves each time, the number of comparisons grows with the logarithm of the list size.",
    "The list must already be sorted, otherwise discarding half of the range could throw away the target by mistake.",
    "A common bug is computing the midpoint with an addition that overflows for very large indices in fixed-width integers.",
    "Binary search is the foundation for many other techniques, such as searching on the answer in optimisation problems.",
]
ARTICLE = ("<html><head><title>Binary search explained</title><meta name='description' content='Halve the range each step.'></head>"
           "<body><nav>Home | About</nav><article><h1>Binary search</h1>" + "".join(f"<p>{p}</p>" for p in PARAS) +
           "</article><footer>copyright</footer></body></html>")


def page_route(fake, url, html=ARTICLE, status=200, ctype="text/html"):
    fake.get(lambda u, p: FakeResp(status, html, headers={"content-type": ctype}) if u == url else None)


def test_extract_urls():
    t = "see https://a.org/x, and http://b.com/y?q=1). Also https://a.org/x again"
    assert web.extract_urls(t) == ["https://a.org/x", "http://b.com/y?q=1"]


def test_ssrf_guard(monkeypatch):
    assert not web.is_safe_url("ftp://example.com/x")[0]
    assert not web.is_safe_url("http://localhost:8000/x")[0]
    assert not web.is_safe_url("http://intranet.internal/x")[0]
    monkeypatch.setattr(web, "_resolve", lambda h, p: [(2, 1, 6, "", ("10.0.0.5", p))])
    assert "not public" in web.is_safe_url("http://sneaky.example.com/")[1]
    monkeypatch.setattr(web, "_resolve", lambda h, p: [(2, 1, 6, "", ("169.254.169.254", p))])
    assert not web.is_safe_url("http://metadata.example.com/")[0]
    monkeypatch.setattr(web, "_resolve", lambda h, p: [(2, 1, 6, "", ("93.184.216.34", p))])
    assert web.is_safe_url("https://example.com/")[0]


def test_tier0_reads_a_static_page(fake_web, store):
    page_route(fake_web, "https://ex.com/bs")
    r = web.fetch_page("https://ex.com/bs", store)
    assert r.ok and r.tier == 0 and "halving the range" in r.text and r.title == "Binary search explained"
    assert "Home | About" not in r.text  # boilerplate removed
    n = len(fake_web.calls)
    assert web.fetch_page("https://ex.com/bs", store).ok and len(fake_web.calls) == n  # second read is cached


def test_falls_back_to_jina_when_page_is_js_only(fake_web):
    page_route(fake_web, "https://spa.example/app", "<html><body><div id=root></div></body></html>")
    body = "Title: Binary search\n\nURL Source: https://spa.example/app\n\nMarkdown Content:\n" + " ".join(PARAS)
    fake_web.get(lambda u, p: FakeResp(200, body, headers={"content-type": "text/plain"}) if u.startswith("https://r.jina.ai/") else None)
    r = web.fetch_page("https://spa.example/app")
    assert r.ok and r.tier == 1 and r.title == "Binary search" and r.text.startswith("Binary search finds")


def test_every_tier_fails_gives_a_clear_message(fake_web):
    page_route(fake_web, "https://tiny.example/", "<html><body><p>hi</p></body></html>")
    fake_web.get(lambda u, p: FakeResp(200, "short", headers={"content-type": "text/plain"}) if u.startswith("https://r.jina.ai/") else None)
    r = web.fetch_page("https://tiny.example/")
    assert not r.ok and "please paste the text" in r.error


def test_robots_txt_is_respected(fake_web):
    fake_web.get(lambda u, p: FakeResp(200, "User-agent: *\nDisallow: /private", headers={"content-type": "text/plain"}) if u.endswith("/robots.txt") else None)
    page_route(fake_web, "https://ex.com/private/page")
    r = web.fetch_page("https://ex.com/private/page")
    assert not r.ok and "robots.txt" in r.error


def test_unsafe_url_is_refused_before_any_request(fake_web):
    r = web.fetch_page("http://localhost/admin")
    assert not r.ok and fake_web.calls == []


# ------------------------------------------------------------ discovery / verification
def test_discover_wikipedia(fake_web):
    fake_web.get(lambda u, p: FakeResp(200, data=["bs", ["Binary search"], [""], ["https://en.wikipedia.org/wiki/Binary_search"]])
                 if "wikipedia.org/w/api.php" in u else None)
    c = web.discover_wikipedia("binary search")
    assert [x.url for x in c] == ["https://en.wikipedia.org/wiki/Binary_search"] and c[0].kind == "link"


def test_discover_images_with_attribution(fake_web):
    data = {"query": {"pages": {"1": {"index": 1, "title": "File:Binary.png", "imageinfo": [{
        "thumburl": "https://upload.wikimedia.org/t.png", "mime": "image/png", "width": 800, "descriptionurl": "https://commons.wikimedia.org/wiki/File:Binary.png",
        "extmetadata": {"Artist": {"value": "<a href='x'>Jane Doe</a>"}, "LicenseShortName": {"value": "CC BY-SA 4.0"}}}]}}}}
    fake_web.get(lambda u, p: FakeResp(200, data=data) if "commons.wikimedia.org/w/api.php" in u else None)
    imgs = web.discover_images("binary search")
    assert imgs and imgs[0].extra["author"] == "Jane Doe" and imgs[0].extra["license"] == "CC BY-SA 4.0"
    assert imgs[0].extra["page_url"].endswith("File:Binary.png")


def test_verify_link_keeps_good_and_drops_dead(fake_web):
    page_route(fake_web, "https://ex.com/ok")
    page_route(fake_web, "https://ex.com/dead", "gone", status=404)
    good = web.verify_link(web.Candidate("l0", "link", "https://ex.com/ok", "", "ex.com"))
    dead = web.verify_link(web.Candidate("l1", "link", "https://ex.com/dead", "x", "ex.com"))
    assert good.verified and good.title == "Binary search explained" and good.reason == "Halve the range each step."
    assert len(good.extra["text"]) > 300
    assert not dead.verified and "404" in dead.extra["drop"]


def test_verify_link_follows_redirect_and_records_final_url(fake_web):
    fake_web.get(lambda u, p: FakeResp(200, ARTICLE, url="https://final.example/bs") if u.startswith("https://redirect.example/") else None)
    c = web.verify_link(web.Candidate("g0", "link", "https://redirect.example/abc", "final.example", "x"))
    assert c.verified and c.url == "https://final.example/bs" and c.source == "final.example"


def test_youtube_ids_and_oembed(fake_web):
    assert web.youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3") == "dQw4w9WgXcQ"
    assert web.youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert web.youtube_id("https://example.com/watch?v=dQw4w9WgXcQ") is None
    fake_web.get(lambda u, p: FakeResp(200, data={"title": "Binary Search", "author_name": "CS Channel"}) if u == "https://www.youtube.com/oembed" and "dQw4w9WgXcQ" in p.get("url", "") else
                 FakeResp(404, "not found") if u == "https://www.youtube.com/oembed" else None)
    ok = web.verify_video(web.classify(web.Candidate("v0", "link", "https://youtu.be/dQw4w9WgXcQ")))
    bad = web.verify_video(web.classify(web.Candidate("v1", "link", "https://youtu.be/AAAAAAAAAAA")))
    assert ok.verified and ok.title == "Binary Search" and ok.source == "CS Channel" and ok.extra["video_id"] == "dQw4w9WgXcQ"
    assert not bad.verified


def test_verify_image(fake_web):
    fake_web.get(lambda u, p: FakeResp(206, "", headers={"content-type": "image/png"}) if u == "https://img.example/a.png" else
                 FakeResp(200, "<html>", headers={"content-type": "text/html"}) if u == "https://img.example/b.png" else None)
    assert web.verify_image(web.Candidate("i0", "image", "https://img.example/a.png")).verified
    assert not web.verify_image(web.Candidate("i1", "image", "https://img.example/b.png")).verified


def test_discover_youtube_needs_a_key(fake_web, monkeypatch):
    assert web.discover_youtube("binary search") == []
    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    fake_web.get(lambda u, p: FakeResp(200, data={"items": [{"id": {"videoId": "dQw4w9WgXcQ"}, "snippet": {"title": "BS", "channelTitle": "Ch"}}]})
                 if "youtube/v3/search" in u else None)
    c = web.discover_youtube("binary search")
    assert c[0].kind == "video" and c[0].extra["video_id"] == "dQw4w9WgXcQ"


def test_seed_candidates_match_by_keyword():
    cfg = {"seeds": [{"keywords": ["python"], "links": {"Tutorial": "https://docs.python.org/3/tutorial/"}}]}
    assert web.seed_candidates(cfg, "Learn Python loops")[0].url == "https://docs.python.org/3/tutorial/"
    assert web.seed_candidates(cfg, "Cooking") == []


def test_snippet_cuts_on_a_word_boundary():
    assert web.snippet("short text") == "short text"
    long = "word " * 80
    out = web.snippet(long, 40)
    assert out.endswith("…") and len(out) <= 41 and not out.rstrip("…").endswith("wor")


def test_replacement_characters_are_stripped_from_extracted_text():
    html = "<html><body><article>" + "".join(f"<p>{p}</p>" for p in PARAS) + "<p>broken \ufffd char sequence in a sentence here.</p></article></body></html>"
    assert "\ufffd" not in web.extract_text(html)
