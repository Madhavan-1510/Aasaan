"""Curator: find, verify and rank links, videos and images for one chapter.

Discovery is untrusted. Verification is CODE (HTTP status, oEmbed, robots.txt). Relevance is a Jev gate: one batched
call with a Noul question per candidate; Gemma is the fallback; last resort keeps the verified results in search order.
Notes are our own writing: we keep only titles, a one-line reason, links and attributions.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .. import web
from ..providers.typesafe import noul
from ..router import ModelRouter
from ..schemas import CuratorOut
from .common import build_prompt

log = logging.getLogger("aasaan.curator")
RELEVANCE_THRESHOLD = 0.6
MAX_LINKS, MAX_VIDEOS, MAX_IMAGES = 4, 2, 3
GENERIC = {"introduction", "intro", "basics", "basic", "concepts", "concept", "core", "key", "terms", "term", "notation", "examples",
           "example", "common", "mistakes", "mistake", "avoid", "techniques", "technique", "practice", "next", "steps", "step", "overview",
           "putting", "together", "worked", "intermediate", "advanced", "guide", "learn", "learning", "review", "about", "with", "from",
           "your", "into", "that", "this", "what", "how", "and", "the", "for", "are", "you", "can"}
SEED_PATH = Path(__file__).resolve().parents[3] / "config" / "seed_resources.yaml"


@dataclass
class Curated:
    links: list = field(default_factory=list)
    videos: list = field(default_factory=list)
    images: list = field(default_factory=list)
    pages: list = field(default_factory=list)      # grounding text from verified third-party pages (chunk-like dicts)
    notes: list = field(default_factory=list)      # human-readable warnings (e.g. no YouTube key)


def topic_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", text.lower()) if t not in GENERIC}


def on_topic(c: web.Candidate, topic: set[str]) -> bool:
    """Cheap code guard before the relevance gate: a fuzzy Wikipedia hit must share a meaningful word with the topic."""
    if not topic:
        return True
    return bool(topic & topic_tokens(f"{c.title} {c.reason if c.kind == 'image' else ''} " + c.url.rsplit("/", 1)[-1].replace("_", " ")))


def _seed_cfg() -> dict:
    try:
        return yaml.safe_load(SEED_PATH.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}


def gate(router: ModelRouter, chapter_title: str, objectives: list[str], cands: list[web.Candidate], language: str = "en") -> list[web.Candidate]:
    """Keep only relevant links/videos. Jev first (one call), Gemma next, search order last."""
    if not cands:
        return []
    by_id = {c.id: c for c in cands}
    lang_name = {"en": "English", "hi": "Hindi", "kn": "Kannada", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam", "bn": "Bengali",
                 "mr": "Marathi"}.get(language, language)
    state = {"chapter": chapter_title, "objectives": objectives, "student_language": lang_name,
             "resources": {c.id: {"kind": c.kind, "title": c.title, "source": c.source, "about": c.reason} for c in cands}}
    questions = {c.id: noul(f"Is this resource useful for a student learning the chapter '{chapter_title}'?",
                            f"Directly relevant, accurate, suitable for learning, and in {lang_name} (or clearly language-independent)",
                            f"Off-topic, spam, too advanced, or in a language other than {lang_name}")
                 for c in cands}
    answers = router.decide("curator", state, questions, summary=f"Jev relevance for '{chapter_title}'")
    if answers:
        keep = []
        for cid, ans in answers.items():
            try:
                if cid in by_id and float(ans["noul"]) >= RELEVANCE_THRESHOLD:
                    keep.append(by_id[cid])
            except (KeyError, TypeError, ValueError):
                continue
        return keep

    ctx = {"candidates": [{"id": c.id, "kind": c.kind, "title": c.title, "source": c.source, "reason": c.reason} for c in cands]}

    def validate(raw: dict) -> CuratorOut:
        out = CuratorOut.model_validate(raw)
        return CuratorOut(relevant=[i for i in out.relevant if i in by_id])

    prompt = build_prompt(
        "You are Curator. From the candidate resources, return the ids that are directly relevant, accurate and suitable "
        f"for a student learning this chapter. Drop spam and off-topic items, and prefer resources in {lang_name}.",
        {"relevant": ["w0", "y1"]}, {"chapter": chapter_title, "objectives": objectives, "candidates": ctx["candidates"]})
    res = router.call("curator", prompt, ctx, validate, last_resort=lambda c: CuratorOut(relevant=[x.id for x in cands]),
                      summary=f"Curator relevance for '{chapter_title}'")
    return [by_id[i] for i in res.value.relevant]


def curate(router: ModelRouter, store, chapter_title: str, objectives: list[str], goal: str = "",
           language: str = "en", max_pages: int = 2) -> Curated:
    """Discovery -> verification (code) -> relevance gate -> capped, ordered results."""
    out = Curated()
    query = f"{chapter_title} {goal}".strip()[:120]
    gem = router.cfg["models"]["gemma_26b"]["id"]
    live = router.mode == "live"

    wait = float(os.environ.get("AASAAN_GROUNDED_TIMEOUT_S", "20"))  # grounded search takes 20-40 s live; never stall a chapter longer

    def grounded():
        if not live:
            return []
        return web.discover_grounded(lambda m, q: router.providers["gemini_api"].grounded_search(m, q, wait), gem, query)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {"wiki": pool.submit(web.discover_wikipedia, chapter_title),
                "grounded": pool.submit(grounded),
                "youtube": pool.submit(web.discover_youtube, query),
                "images": pool.submit(web.discover_images, chapter_title if len(chapter_title) > 3 else goal)}
        found = {k: f.result() for k, f in futs.items()}
    topic = topic_tokens(f"{chapter_title} {goal}")
    wiki = [c for c in found["wiki"] if on_topic(c, topic)]
    if len(wiki) < len(found["wiki"]):
        log.info("[aasaan] dropped %d off-topic Wikipedia hit(s) for '%s'", len(found["wiki"]) - len(wiki), chapter_title)
    cands = [web.classify(c) for c in found["grounded"] + wiki]
    cands += found["youtube"]
    if len(cands) < 3:  # too little found: add the curated seed list for this subject
        cands += [web.classify(c) for c in web.seed_candidates(_seed_cfg(), f"{chapter_title} {goal}")]
    if not os.environ.get("YOUTUBE_API_KEY") and not live:
        out.notes.append("No YouTube key or Google Search grounding: videos are skipped.")
    # de-duplicate by URL, unique ids across providers
    seen, uniq = set(), []
    for c in cands + found["images"]:
        key = c.url.split("#")[0]
        if key not in seen:
            seen.add(key)
            c.id = f"{c.kind[0]}{len(uniq)}"
            uniq.append(c)

    verified = web.verify_all(uniq)
    textual = [c for c in verified if c.kind in ("link", "video")]
    images = [c for c in verified if c.kind == "image" and on_topic(c, topic)]  # same code guard as for Wikipedia hits
    kept = gate(router, chapter_title, objectives, textual, language)
    out.links = [c for c in kept if c.kind == "link"][:MAX_LINKS]
    out.videos = [c for c in kept if c.kind == "video"][:MAX_VIDEOS]
    lead = [wikipedia_img for wikipedia_img in (web.wikipedia_lead_image(c.url) for c in out.links) if wikipedia_img]
    lead = web.verify_all(lead)  # same code verification as every other image
    for c in lead:
        c.id = f"i-lead{len(images)}"
    out.images = (lead + [i for i in images if all(i.url != l.url for l in lead)])[:MAX_IMAGES]

    for c in out.links:  # grounding text for the Teacher/Critic (third-party: used to check, never copied)
        text = c.extra.get("text", "")
        if len(text) >= 300 and len(out.pages) < max_pages:
            cid = "w" + hashlib.sha1(c.url.encode()).hexdigest()[:6]
            out.pages.append({"chunk_id": f"{cid}-p1-c1", "page": 1, "text": text[:1800], "url": c.url, "title": c.title})
    log.info("[aasaan] curated '%s': %d links, %d videos, %d images (from %d candidates)", chapter_title,
             len(out.links), len(out.videos), len(out.images), len(uniq))
    return out
