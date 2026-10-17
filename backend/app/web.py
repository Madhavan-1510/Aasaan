"""Web access for Aasaan: reading URLs the student gives, and finding verified resources for chapters.

Reading a URL (`fetch_page`), cheapest tier first, behind ONE function so a headless option can be added later:
  Tier 0  httpx + trafilatura                       (no external service)
  Tier 1  Jina Reader  GET https://r.jina.ai/<url>  (works without a key, rate limited; JINA_API_KEY raises the quota)
  Tier 2  Firecrawl    POST https://api.firecrawl.dev/v2/scrape   (only if FIRECRAWL_API_KEY is set)
If every tier yields under ~300 characters we say so and log it. Pages are cached by URL hash.

Rules: http/https only, public hosts only (SSRF guard), robots.txt respected, a clear User-Agent, 8-10 s timeouts,
a small per-domain concurrency limit. Third-party content is untrusted data: we store titles, short snippets and
links, never whole pages in the student's notes.
"""
from __future__ import annotations

import hashlib
import html as htmllib
import ipaddress
import logging
import os
import re
import socket
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote, unquote, urlparse

import httpx

log = logging.getLogger("aasaan.web")

MIN_TEXT_CHARS = 300
TIMEOUT_S = 9.0
CONTACT = os.environ.get("AASAAN_CONTACT", "https://aasaan.local")
USER_AGENT = f"AasaanBot/1.0 (+{CONTACT}; educational tutor; respects robots.txt)"
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


# ------------------------------------------------------------------ http (patchable in tests)
def http_get(url: str, *, headers: Optional[dict] = None, params: Optional[dict] = None, timeout: float = TIMEOUT_S):
    h = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5"}
    h.update(headers or {})
    return httpx.get(url, headers=h, params=params, timeout=timeout, follow_redirects=True)


def http_post(url: str, *, headers: Optional[dict] = None, json: Any = None, timeout: float = 30.0):
    h = {"User-Agent": USER_AGENT}
    h.update(headers or {})
    return httpx.post(url, headers=h, json=json, timeout=timeout)


_resolve = socket.getaddrinfo  # patchable in tests


def extract_urls(text: str) -> list[str]:
    seen, out = set(), []
    for m in URL_RE.findall(text or ""):
        u = m.rstrip(".,;:!?")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def is_safe_url(url: str) -> tuple[bool, str]:
    """SSRF guard: only http(s) to public addresses."""
    try:
        p = urlparse(url)
    except ValueError:
        return False, "malformed URL"
    if p.scheme not in ("http", "https") or not p.hostname:
        return False, "only http(s) URLs are allowed"
    host = p.hostname.lower()
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".internal") or host.endswith(".local"):
        return False, "local addresses are not allowed"
    try:
        infos = _resolve(host, p.port or (443 if p.scheme == "https" else 80))
    except OSError as e:
        return False, f"cannot resolve host: {e}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False, "address is not public"
    return True, ""


# ------------------------------------------------------------------ politeness
_domain_sems: dict[str, threading.Semaphore] = {}
_domain_lock = threading.Lock()
_robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser]] = {}


def _domain_sem(host: str) -> threading.Semaphore:
    with _domain_lock:
        return _domain_sems.setdefault(host, threading.Semaphore(2))


def allowed_by_robots(url: str) -> bool:
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    now = time.time()
    cached = _robots.get(origin)
    if not cached or now - cached[0] > 3600:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = http_get(origin + "/robots.txt", timeout=5)
            if r.status_code == 200 and "text" in r.headers.get("content-type", "text/plain"):
                rp.parse(r.text.splitlines())
            else:
                rp.parse([])  # no usable robots.txt: allowed
        except Exception:
            rp.parse([])
        _robots[origin] = (now, rp)
        cached = _robots[origin]
    return cached[1].can_fetch("AasaanBot", url)


# --------------------------------------------------------------------- reading
@dataclass
class PageResult:
    ok: bool
    url: str
    title: str = ""
    text: str = ""
    description: str = ""
    tier: int = -1
    error: str = ""


def clean_text(s: str) -> str:
    """Titles/descriptions shown to students: decode HTML entities, replace broken-encoding characters, squash whitespace."""
    return re.sub(r"\s+", " ", htmllib.unescape(s or "").replace("\ufffd", "-")).strip()


def _html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return clean_text(m.group(1)) if m else ""


def _meta_description(html: str) -> str:
    for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']'):
        m = re.search(pat, html, re.I | re.S)
        if m:
            return clean_text(m.group(1))
    return ""


def extract_text(html: str) -> str:
    try:
        import trafilatura
        return (trafilatura.extract(html, include_comments=False, include_tables=False, favor_recall=True) or "").replace("\ufffd", "").strip()
    except Exception as e:  # extraction must never break a build
        log.warning("[aasaan] trafilatura failed: %s", e)
        return ""


def _cache_key(url: str) -> str:
    return "page:" + hashlib.sha1(url.encode()).hexdigest()


def fetch_page(url: str, store=None) -> PageResult:
    """Read one URL into text. See module docstring for tiers. Never raises."""
    if store:
        hit = store.cache_get(_cache_key(url))
        if hit:
            return PageResult(**hit)
    ok, why = is_safe_url(url)
    if not ok:
        log.warning("[aasaan] fetch_page refused %s: %s", url, why)
        return PageResult(False, url, error=why)
    if not allowed_by_robots(url):
        log.warning("[aasaan] fetch_page %s disallowed by robots.txt", url)
        return PageResult(False, url, error="robots.txt does not allow reading this page")

    host = urlparse(url).hostname or ""
    errors: list[str] = []
    with _domain_sem(host):
        # Tier 0
        try:
            r = http_get(url)
            ctype = r.headers.get("content-type", "")
            if r.status_code < 400 and ("html" in ctype or "text" in ctype):
                text = extract_text(r.text)
                if len(text) >= MIN_TEXT_CHARS:
                    return _cache(store, PageResult(True, str(getattr(r, "url", url)), _html_title(r.text), text[:20000],
                                                    _meta_description(r.text), 0))
                errors.append(f"tier0: only {len(text)} chars")
            else:
                errors.append(f"tier0: HTTP {r.status_code} {ctype}")
        except Exception as e:
            errors.append(f"tier0: {type(e).__name__}: {e}")
        # Tier 1: Jina Reader
        try:
            headers = {"Accept": "text/plain"}
            if os.environ.get("JINA_API_KEY"):
                headers["Authorization"] = f"Bearer {os.environ['JINA_API_KEY']}"
            r = http_get("https://r.jina.ai/" + url, headers=headers, timeout=15)
            if r.status_code == 200:
                title, body = _parse_jina(r.text)
                if len(body) >= MIN_TEXT_CHARS:
                    return _cache(store, PageResult(True, url, title, body[:20000], "", 1))
                errors.append(f"tier1: only {len(body)} chars")
            else:
                errors.append(f"tier1: HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"tier1: {type(e).__name__}: {e}")
        # Tier 2: Firecrawl (only with a key)
        if os.environ.get("FIRECRAWL_API_KEY"):
            try:
                r = http_post("https://api.firecrawl.dev/v2/scrape",
                              headers={"Authorization": f"Bearer {os.environ['FIRECRAWL_API_KEY']}"},
                              json={"url": url, "formats": ["markdown"], "onlyMainContent": True}, timeout=40)
                data = (r.json() or {}).get("data", {}) if r.status_code == 200 else {}
                md = (data.get("markdown") or "").strip()
                if len(md) >= MIN_TEXT_CHARS:
                    return _cache(store, PageResult(True, url, (data.get("metadata") or {}).get("title", ""), md[:20000], "", 2))
                errors.append(f"tier2: HTTP {r.status_code}, {len(md)} chars")
            except Exception as e:
                errors.append(f"tier2: {type(e).__name__}: {e}")
    msg = "I couldn't read that page, please paste the text"
    log.warning("[aasaan] fetch_page failed for %s: %s", url, "; ".join(errors))
    return PageResult(False, url, error=f"{msg} ({'; '.join(errors)[:300]})")


def _parse_jina(text: str) -> tuple[str, str]:
    title = ""
    m = re.match(r"\s*Title:\s*(.*?)\n", text)
    if m:
        title = m.group(1).strip()
    if "Markdown Content:" in text:
        text = text.split("Markdown Content:", 1)[1]
    return title, text.strip()


def _cache(store, res: PageResult) -> PageResult:
    if store:
        store.cache_set(_cache_key(res.url), res.__dict__)
    return res


# ================================================================== resources
@dataclass
class Candidate:
    id: str
    kind: str                       # link | video | image
    url: str
    title: str = ""
    source: str = ""                # domain or provider
    reason: str = ""                # one short line (page description), never a copy of the page
    extra: dict = field(default_factory=dict)
    verified: bool = False


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def snippet(text: str, n: int = 160) -> str:
    """A short one-line reason: cut at a word boundary, add an ellipsis. Never a copy of the page."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


# -------------------------------------------------------------------- discovery
def discover_wikipedia(query: str, n: int = 3) -> list[Candidate]:
    try:
        r = http_get("https://en.wikipedia.org/w/api.php",
                     params={"action": "opensearch", "search": query, "limit": n, "namespace": 0, "format": "json"})
        data = r.json()
        return [Candidate(f"w{i}", "link", u, t, "wikipedia.org") for i, (t, u) in enumerate(zip(data[1], data[3]))]
    except Exception as e:
        log.warning("[aasaan] wikipedia discovery failed for %r: %s", query, e)
        return []


def discover_youtube(query: str, n: int = 4) -> list[Candidate]:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        return []
    try:
        r = http_get("https://www.googleapis.com/youtube/v3/search",
                     params={"part": "snippet", "q": query, "type": "video", "maxResults": n, "videoEmbeddable": "true",
                             "safeSearch": "strict", "key": key})
        if r.status_code != 200:
            log.warning("[aasaan] youtube search HTTP %s: %s", r.status_code, r.text[:200])
            return []
        out = []
        for i, it in enumerate(r.json().get("items", [])):
            vid = it["id"]["videoId"]
            out.append(Candidate(f"y{i}", "video", f"https://www.youtube.com/watch?v={vid}", it["snippet"]["title"],
                                 it["snippet"].get("channelTitle", "YouTube"), extra={"video_id": vid}))
        return out
    except Exception as e:
        log.warning("[aasaan] youtube discovery failed for %r: %s", query, e)
        return []


def youtube_id(url: str) -> Optional[str]:
    p = urlparse(url)
    host = (p.hostname or "").removeprefix("www.")
    if host == "youtu.be":
        return p.path.strip("/").split("/")[0] or None
    if host in ("youtube.com", "m.youtube.com", "music.youtube.com") and p.path == "/watch":
        m = re.search(r"[?&]v=([\w-]{11})", "?" + p.query)
        return m.group(1) if m else None
    return None


def discover_images(query: str, n: int = 4) -> list[Candidate]:
    """Wikimedia Commons first (no key), then Pexels if a key exists. Hotlinked thumbnails + attribution only."""
    out: list[Candidate] = []
    try:
        r = http_get("https://commons.wikimedia.org/w/api.php", params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
            "gsrnamespace": 6, "gsrlimit": n * 2, "prop": "imageinfo", "iiprop": "url|extmetadata|mime|size", "iiurlwidth": 800})
        pages = (r.json().get("query") or {}).get("pages") or {}
        for i, pg in enumerate(sorted(pages.values(), key=lambda p: p.get("index", 0))):
            ii = (pg.get("imageinfo") or [{}])[0]
            if not ii.get("thumburl") or not str(ii.get("mime", "")).startswith("image/") or ii.get("width", 0) < 400:
                continue
            md = ii.get("extmetadata") or {}
            artist = _strip_tags((md.get("Artist") or {}).get("value", "")) or "Unknown author"
            lic = (md.get("LicenseShortName") or {}).get("value", "see source")
            desc = _strip_tags((md.get("ImageDescription") or {}).get("value", ""))[:200]
            out.append(Candidate(f"i{i}", "image", ii["thumburl"], pg.get("title", "").removeprefix("File:"),
                                 "Wikimedia Commons", reason=desc, extra={"page_url": ii.get("descriptionurl", ""), "author": artist,
                                                                          "license": lic}))
            if len(out) >= n:
                break
    except Exception as e:
        log.warning("[aasaan] commons discovery failed for %r: %s", query, e)
    key = os.environ.get("PEXELS_API_KEY")
    if key and len(out) < n:
        try:
            r = http_get("https://api.pexels.com/v1/search", headers={"Authorization": key},
                         params={"query": query, "per_page": n - len(out)})
            for i, ph in enumerate((r.json() or {}).get("photos", [])):
                out.append(Candidate(f"p{i}", "image", ph["src"]["large"], ph.get("alt") or query, "Pexels",
                                     extra={"page_url": ph.get("url", ""), "author": ph.get("photographer", ""), "license": "Pexels license"}))
        except Exception as e:
            log.warning("[aasaan] pexels discovery failed for %r: %s", query, e)
    return out


def wikipedia_lead_image(page_url: str) -> Optional[Candidate]:
    """The lead image of a Wikipedia article we already linked, with its author and licence from Commons.
    Only Commons files (free licences) are used; images hosted locally on Wikipedia (fair use) are skipped."""
    p = urlparse(page_url)
    if not (p.hostname or "").endswith("wikipedia.org") or not p.path.startswith("/wiki/"):
        return None
    try:
        r = http_get(f"https://{p.hostname}/api/rest_v1/page/summary/{p.path.removeprefix('/wiki/')}")
        if r.status_code != 200:
            return None
        src = ((r.json().get("originalimage") or r.json().get("thumbnail") or {}).get("source")) or ""
        if "/wikipedia/commons/" not in src:
            return None
        segs = urlparse(src).path.split("/")
        name = unquote(segs[-2] if "/thumb/" in src else segs[-1])
        ci = http_get("https://commons.wikimedia.org/w/api.php", params={
            "action": "query", "format": "json", "titles": f"File:{name}", "prop": "imageinfo",
            "iiprop": "url|extmetadata|mime|size", "iiurlwidth": 800})
        pages = (ci.json().get("query") or {}).get("pages") or {}
        ii = (next(iter(pages.values()), {}).get("imageinfo") or [{}])[0]
        if not ii.get("thumburl") or not str(ii.get("mime", "")).startswith("image/") or ii.get("width", 0) < 300:
            return None
        md = ii.get("extmetadata") or {}
        return Candidate("li0", "image", ii["thumburl"], name.rsplit(".", 1)[0].replace("_", " "), "Wikimedia Commons",
                         reason=_strip_tags((md.get("ImageDescription") or {}).get("value", ""))[:200],
                         extra={"page_url": ii.get("descriptionurl", ""), "author": _strip_tags((md.get("Artist") or {}).get("value", "")) or "Unknown author",
                                "license": (md.get("LicenseShortName") or {}).get("value", "see source"), "lead_of": page_url})
    except Exception as e:
        log.warning("[aasaan] lead image lookup failed for %s: %s", page_url, e)
        return None


def discover_grounded(provider_search, model_id: str, query: str, n: int = 6) -> list[Candidate]:
    """Gemini API Google Search grounding with a Gemma 4 model (no extra key). Results are untrusted candidates."""
    try:
        hits = provider_search(model_id, f"Find the best free web pages and YouTube videos for learning: {query}")
    except Exception as e:
        log.warning("[aasaan] grounded search failed for %r: %s", query, e)
        return []
    out = []
    for i, h in enumerate(hits[:n]):
        uri = h.get("uri", "")
        vid = None
        # grounding links are redirect URLs; the final URL is resolved during verification
        out.append(Candidate(f"g{i}", "link", uri, h.get("title", ""), _domain(uri), extra={"grounded": True}))
    return out


def seed_candidates(seed_cfg: dict, text: str) -> list[Candidate]:
    low = text.lower()
    out: list[Candidate] = []
    for entry in (seed_cfg or {}).get("seeds", []):
        if any(k in low for k in entry.get("keywords", [])):
            for j, (title, url) in enumerate(entry.get("links", {}).items()):
                out.append(Candidate(f"s{len(out)}", "link", url, title, _domain(url)))
    return out


# ----------------------------------------------------------------- verification
def verify_link(c: Candidate) -> Candidate:
    """Code, not a model: the link must answer successfully with a page. Records title and a short reason."""
    ok, why = is_safe_url(c.url)
    if not ok:
        c.extra["drop"] = why
        return c
    try:
        r = http_get(c.url, timeout=TIMEOUT_S)
    except Exception as e:
        c.extra["drop"] = f"{type(e).__name__}: {e}"
        return c
    final = str(getattr(r, "url", c.url))
    ctype = r.headers.get("content-type", "")
    if r.status_code >= 400 or not ("html" in ctype or "pdf" in ctype or "text" in ctype):
        c.extra["drop"] = f"HTTP {r.status_code} {ctype}"
        return c
    if not allowed_by_robots(final):
        c.extra["drop"] = "robots.txt"
        return c
    vid = youtube_id(final)
    if vid:  # a grounded link that really is a YouTube video: verify it as a video (oEmbed), embed with the official player
        c.kind, c.extra["video_id"], c.url = "video", vid, final
        return verify_video(c)
    c.url, c.source = final, _domain(final)
    if "html" in ctype:
        html = r.text
        title = _html_title(html)
        if title and (not c.title or len(c.title) < 4 or "." in c.title.split(" ")[0] and " " not in c.title):
            c.title = title
        c.title = c.title or title or c.source
        desc = _meta_description(html)
        text = extract_text(html)
        c.reason = snippet(desc or " ".join(text.split()[:30]))
        c.extra["text"] = text[:3000]
    else:
        c.reason = c.reason or "Reference document"
    c.verified = True
    return c


def verify_video(c: Candidate) -> Candidate:
    """The video id must pass YouTube's oEmbed endpoint (exists and embeddable). Official embed only."""
    vid = c.extra.get("video_id") or youtube_id(c.url)
    if not vid:
        c.extra["drop"] = "not a YouTube watch URL"
        return c
    try:
        r = http_get("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"}, timeout=6)
        if r.status_code != 200:
            c.extra["drop"] = f"oEmbed HTTP {r.status_code}"
            return c
        data = r.json()
        c.title, c.source = data.get("title", c.title), data.get("author_name", c.source or "YouTube")
        c.extra["video_id"], c.url = vid, f"https://www.youtube.com/watch?v={vid}"
        c.verified = True
    except Exception as e:
        c.extra["drop"] = f"{type(e).__name__}: {e}"
    return c


def verify_image(c: Candidate) -> Candidate:
    try:
        r = http_get(c.url, headers={"Range": "bytes=0-2047", "Accept": "image/*"}, timeout=TIMEOUT_S)
        if r.status_code in (200, 206) and r.headers.get("content-type", "").startswith("image/"):
            c.verified = True
        else:
            c.extra["drop"] = f"HTTP {r.status_code} {r.headers.get('content-type', '')}"
    except Exception as e:
        c.extra["drop"] = f"{type(e).__name__}: {e}"
    return c


def classify(c: Candidate) -> Candidate:
    """A grounded or seeded link that is really a YouTube video becomes a video candidate."""
    vid = youtube_id(c.url)
    if vid:
        c.kind, c.extra["video_id"] = "video", vid
    return c


def verify_all(cands: list[Candidate], max_workers: int = 6) -> list[Candidate]:
    from concurrent.futures import ThreadPoolExecutor
    fn = {"link": verify_link, "video": verify_video, "image": verify_image}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        done = list(pool.map(lambda c: fn[c.kind](c), cands))
    for c in done:
        if not c.verified:
            log.info("[aasaan] dropped %s %s: %s", c.kind, c.url[:80], c.extra.get("drop", ""))
    return [c for c in done if c.verified]
