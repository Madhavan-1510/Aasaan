"""Real-world figures (photos, anatomy, maps, diagrams that must be accurate). Never drawn by a model.

Order: Wikimedia Commons (SVG preferred, licence allow-list) -> Openverse (anonymous, or OAuth when OPENVERSE_CLIENT_ID/SECRET are set)
-> nothing. Images are hotlinked, never stored; attribution is kept with the figure. Every call goes through the ToolRunner.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from .. import web
from ..tools import ToolFailure

# Licences we are willing to show with attribution. Anything not in this list (or unknown) is dropped.
ALLOWED_LICENSES = ("cc0", "public domain", "pd", "cc by", "cc-by", "cc by-sa", "cc-by-sa", "attribution", "pdm", "by", "by-sa")
BLOCKED_LICENSE_WORDS = ("nc", "nd", "fair use", "non-free", "all rights")


@dataclass
class Figure:
    url: str
    page_url: str
    title: str
    author: str
    license: str
    source: str                       # "commons" | "openverse"
    is_svg: bool = False
    width: int = 0
    extra: dict = field(default_factory=dict)


def license_ok(lic: str) -> bool:
    low = (lic or "").lower().replace("_", " ").strip()
    if not low:
        return False
    if any(re.search(rf"(^|[\s-]){w}($|[\s-])", low) for w in BLOCKED_LICENSE_WORDS):
        return False
    return any(low == a or low.startswith(a + " ") or low.startswith(a + "-") or a in low for a in ALLOWED_LICENSES)


def _tokens(text: str) -> set[str]:
    from ..agents.curator import topic_tokens
    return topic_tokens(text)


def on_topic(topic: str, fig: Figure) -> bool:
    """Cheap guard: the file title or description must share a meaningful word with the topic."""
    want = _tokens(topic)
    return not want or bool(want & _tokens(f"{fig.title} {fig.extra.get('desc', '')}"))


# ----------------------------------------------------------------------------- Commons
def search_commons(query: str, n: int = 6) -> list[Figure]:
    def search():
        r = web.http_get("https://commons.wikimedia.org/w/api.php", params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": n * 2,
            "prop": "imageinfo", "iiprop": "url|extmetadata|mime|size", "iiurlwidth": 900})
        pages = (web._json(r, "commons figure search").get("query") or {}).get("pages") or {}
        out = []
        for pg in sorted(pages.values(), key=lambda p: p.get("index", 0)):
            ii = (pg.get("imageinfo") or [{}])[0]
            mime = str(ii.get("mime", ""))
            if not mime.startswith("image/") or not ii.get("thumburl"):
                continue
            md = ii.get("extmetadata") or {}
            out.append({"title": pg.get("title", "").removeprefix("File:").rsplit(".", 1)[0].replace("_", " "),
                        "url": ii["thumburl"] if mime != "image/svg+xml" else (ii.get("thumburl") or ii["url"]), "page_url": ii.get("descriptionurl", ""),
                        "author": web._strip_tags((md.get("Artist") or {}).get("value", "")) or "Unknown author",
                        "license": (md.get("LicenseShortName") or {}).get("value", ""), "svg": mime == "image/svg+xml",
                        "width": ii.get("width", 0), "desc": web._strip_tags((md.get("ImageDescription") or {}).get("value", ""))[:200]})
        return {"figures": out}

    res = web.tool("commons", search, cache_key=f"figures:{n}:{query.lower()}", summary=f"commons figure search '{query[:40]}'")
    return [Figure(f["url"], f["page_url"], f["title"], f["author"], f["license"], "commons", f["svg"], f["width"], {"desc": f["desc"]})
            for f in res.value_or({}).get("figures", [])], res


# --------------------------------------------------------------------------- Openverse
_token: dict = {"value": None, "exp": 0.0}


def _openverse_headers() -> dict:
    cid, secret = os.environ.get("OPENVERSE_CLIENT_ID"), os.environ.get("OPENVERSE_CLIENT_SECRET")
    h = {"Accept": "application/json", "User-Agent": f"Aasaan/1.0 ({os.environ.get('AASAAN_CONTACT', 'learning-app')})"}
    if not (cid and secret):
        return h   # anonymous: 20 requests/min, 200/day
    if not _token["value"] or _token["exp"] < time.time() + 30:
        import httpx
        resp = httpx.post("https://api.openverse.org/v1/auth_tokens/token/", data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
                          timeout=15)
        if resp.status_code in (401, 403):
            raise ToolFailure("auth", "Openverse rejected the client id/secret", False, status=resp.status_code)
        resp.raise_for_status()
        d = resp.json()
        _token["value"], _token["exp"] = d.get("access_token"), time.time() + float(d.get("expires_in", 3600))
    return {**h, "Authorization": f"Bearer {_token['value']}"}


def search_openverse(query: str, n: int = 6):
    def search():
        r = web.http_get("https://api.openverse.org/v1/images/", headers=_openverse_headers(), params={
            "q": query, "page_size": n * 2, "license_type": "commercial,modification", "mature": "false", "category": "illustration,digitized_artwork,photograph"})
        results = web._json(r, "openverse search").get("results") or []
        out = []
        for it in results:
            url = it.get("url") or it.get("thumbnail")
            if not url:
                continue
            lic = (it.get("license") or "").lower()
            ver = (it.get("license_version") or "").strip()
            out.append({"title": it.get("title") or "", "url": url, "page_url": it.get("foreign_landing_url") or "", "author": it.get("creator") or "Unknown author",
                        "license": f"{lic.upper()} {ver}".strip() if lic else "", "width": it.get("width") or 0, "desc": " ".join(t.get("name", "") for t in (it.get("tags") or [])[:6])})
        return {"figures": out}

    res = web.tool("openverse", search, cache_key=f"figures:{n}:{query.lower()}", summary=f"openverse figure search '{query[:40]}'")
    return [Figure(f["url"], f["page_url"], f["title"], f["author"], f["license"], "openverse", False, f["width"], {"desc": f["desc"]})
            for f in res.value_or({}).get("figures", [])], res


# ----------------------------------------------------------------------------- choose
@dataclass
class FigureResult:
    figure: Optional[Figure]
    tried: list[str]                  # human-readable trail: "commons: failed [rate_limit] ...", "openverse: no usable result"


def find_figure(query: str, topic: str) -> FigureResult:
    """Best verified figure or None, plus the trail of why each source did or did not work (it goes into events)."""
    trail: list[str] = []

    def commons(q: str):
        # SVG drawings first (they scale and suit both themes); photos/bitmaps only when there is no usable drawing
        figs, res = search_commons(f"{q} filetype:drawing")
        if res.ok and any(license_ok(f.license) and on_topic(topic, f) for f in figs):
            return figs, res
        figs2, res2 = search_commons(q)
        return (figs2, res2) if res2.ok else (figs, res2 if not res.ok else res)

    for name, search in (("commons", commons), ("openverse", search_openverse)):
        figs, res = search(query)
        if not res.ok:
            trail.append(f"{name}: unavailable [{res.error.kind}] {res.error.message[:80]}")
            continue
        cands = [f for f in figs if license_ok(f.license) and on_topic(topic, f) and (f.width == 0 or f.width >= 300)]
        cands.sort(key=lambda f: (not f.is_svg, -min(f.width, 1200)))
        for f in cands[:3]:
            checked = web.verify_image(web.Candidate("f0", "image", f.url, f.title, f.source))
            if checked.verified:
                trail.append(f"{name}: found '{f.title[:40]}' ({f.license})")
                return FigureResult(f, trail)
        trail.append(f"{name}: no usable figure (licence, topic or availability)")
    return FigureResult(None, trail)
