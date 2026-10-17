"""Baseline for the orchestrator migration: FIVE fixed topics, the first TWO chapters of each, built with the REAL models.

    python eval/baseline.py --label old            # writes eval/baseline/old.json and old.md
    python eval/baseline.py --label orchestrated   # same set, same metrics, after the migration

THE SET IS FROZEN (do not edit TOPICS after eval/baseline/old.json exists): the whole point is to compare like with like.
Everything runs in a throw-away in-memory database: it never touches data/aasaan.db.

Per chapter it records
  readability   average sentence length, longest paragraph (words), reading time (minutes), headings, bullet share,
                a table written on one line, the notes repeating the chapter title as their first heading
  claims        share of the notes' claim sentences the sources support (decision chain: Jev, Jev via OpenRouter, Mercury, Gemma);
                null when the chapter has no sources (written from general knowledge)
  unverified    did the chapter ship without the Critic's approval
  build_s       wall-clock seconds to build the chapter
  fallback      share of model/tool events in that chapter's window that were retries, fallbacks or failures (from `events`)
  blocks        what the chapter contains (notes, slides, video, image, link, visual, quiz, summary counts)
Model output varies from run to run, so treat single numbers as indicative; compare the aggregates.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agents import gates  # noqa: E402
from app.db import Store  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.main import load_env  # noqa: E402
from app.router import ModelRouter  # noqa: E402
from app.tools import ToolRunner  # noqa: E402

CHAPTERS_PER_TOPIC = 2
PDF = ROOT / "eval" / "data" / "TOC-Cloud_Data_Engineer.pdf"
TOPICS = [   # FROZEN
    {"id": "cloud-de-pdf", "text": "Build me a course from this syllabus", "pdf": PDF},
    {"id": "binary-search", "text": "Teach me binary search from scratch"},
    {"id": "photosynthesis", "text": "Teach me how photosynthesis works"},
    {"id": "french-revolution", "text": "Teach me the French Revolution"},
    {"id": "sql-joins", "text": "Teach me SQL joins"},
]
OUT = ROOT / "eval" / "baseline"


# ---------------------------------------------------------------------------------------- readability
def readability(notes_md: str, title: str) -> dict:
    body = re.sub(r"```.*?```", " ", notes_md or "", flags=re.S)
    lines = body.splitlines()
    prose = [re.sub(r"^[\s>*\-#\d.]+", "", l).strip() for l in lines if l.strip() and not l.strip().startswith("|")]
    sentences = [s for l in prose for s in re.split(r"(?<=[.!?])\s+", l) if len(s.split()) >= 3]
    words = re.findall(r"\w+", body)
    paras = [p for p in re.split(r"\n\s*\n", body) if p.strip()]
    heads = [re.sub(r"^#+\s*", "", l).strip() for l in lines if l.lstrip().startswith("#")]
    bullets = sum(1 for l in lines if re.match(r"^\s*([-*]|\d+[.)])\s+", l))
    table_one_line = any(len(re.findall(r"\|\s*:?-{3,}", l)) >= 1 and l.count("|") > 8 for l in lines)
    first_head = heads[0].lower().strip(" :") if heads else ""
    return {
        "words": len(words), "sentences": len(sentences),
        "avg_sentence_words": round(sum(len(s.split()) for s in sentences) / len(sentences), 1) if sentences else None,
        "longest_paragraph_words": max((len(p.split()) for p in paras), default=0),
        "reading_minutes": round(len(words) / 200, 1), "headings": len(heads),
        "bullet_share": round(bullets / max(1, len([l for l in lines if l.strip()])), 2),
        "table_on_one_line": table_one_line, "repeats_title_heading": bool(first_head) and first_head == title.lower().strip(" :"),
    }


def claims_supported(router, notes_md: str, chunks: list[dict]) -> dict:
    claims = gates.claim_sentences(notes_md)
    if not chunks or not claims:
        return {"claims": len(claims), "supported_share": None, "note": "no sources" if not chunks else "no claims found"}
    flagged = gates.claim_support(router, notes_md, chunks)
    if flagged is None:
        return {"claims": len(claims), "supported_share": None, "note": "no decision provider answered"}
    return {"claims": len(claims), "flagged": len(flagged), "supported_share": round(1 - len(flagged) / len(claims), 2)}


# --------------------------------------------------------------------------------------------- run
def event_window(store, first_id: int) -> dict:
    rows = [e for e in store.events(5000) if e["id"] > first_id]
    bad = [e for e in rows if e["status"] in ("retry", "fallback", "failed")]
    by = Counter(e["agent"].split(":")[0] for e in bad)
    return {"events": len(rows), "retry_fallback_failed": len(bad), "fallback_rate": round(len(bad) / len(rows), 2) if rows else 0.0, "worst_agents": dict(by.most_common(3))}


def run_topic(topic: dict, router, store, engine) -> dict:
    t_topic = time.perf_counter()
    files = [(topic["pdf"].name, "application/pdf", topic["pdf"].read_bytes())] if topic.get("pdf") else []
    nid = engine.create_notebook("baseline-learner", topic["text"], "en", files)
    res = {"topic": topic["id"], "notebook_id": nid, "chapters": []}
    first = max([e["id"] for e in store.events(1)] or [0])
    t0 = time.perf_counter()
    try:
        engine.phase0(nid)                         # outline (the same step the old path runs first)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"outline failed: {type(e).__name__}: {e}"
        return res
    res["outline_s"] = round(time.perf_counter() - t0, 1)
    res["outline_events"] = event_window(store, first)
    chapters = store.chapters(nid)
    res["outline_chapters"] = len(chapters)
    all_chunks = engine._all_chunks(nid)
    for ch in chapters[:CHAPTERS_PER_TOPIC]:
        mark = max([e["id"] for e in store.events(1)] or [0])
        t1 = time.perf_counter()
        row = {"position": ch["position"], "title": ch["title"]}
        try:
            engine.build_chapter(ch["id"])
        except Exception as e:  # noqa: BLE001
            row.update(status="failed", error=f"{type(e).__name__}: {e}"[:300], build_s=round(time.perf_counter() - t1, 1))
            res["chapters"].append(row)
            continue
        row["build_s"] = round(time.perf_counter() - t1, 1)
        row.update(event_window(store, mark))
        done = store.get_chapter(ch["id"])
        blocks = store.blocks(ch["id"])
        notes = next((b["content"].get("markdown", "") for b in blocks if b["type"] == "notes"), "")
        row.update(status=done["status"], unverified=bool(done["meta"].get("unverified")), revisions=done["meta"].get("revisions"),
                   blocks=dict(Counter(b["type"] for b in blocks)), readability=readability(notes, ch["title"]),
                   claims=claims_supported(router, notes, engine._chapter_chunks(done, all_chunks) + (done["meta"].get("page_chunks") or [])))
        res["chapters"].append(row)
    res["total_s"] = round(time.perf_counter() - t_topic, 1)
    return res


def mean(vals):
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 2) if v else None


def summarise(topics: list[dict]) -> dict:
    rows = [c for t in topics for c in t["chapters"]]
    ok = [c for c in rows if c.get("status") == "ready"]
    return {
        "topics": len(topics), "chapters_built": len(rows), "chapters_ready": len(ok), "chapters_failed": len(rows) - len(ok),
        "shipped_unverified_rate": round(sum(1 for c in ok if c["unverified"]) / len(ok), 2) if ok else None,
        "avg_build_s": mean([c["build_s"] for c in ok]), "avg_fallback_rate": mean([c["fallback_rate"] for c in rows if "fallback_rate" in c]),
        "avg_claims_supported": mean([c["claims"]["supported_share"] for c in ok]),
        "avg_sentence_words": mean([c["readability"]["avg_sentence_words"] for c in ok]),
        "avg_longest_paragraph_words": mean([c["readability"]["longest_paragraph_words"] for c in ok]),
        "avg_reading_minutes": mean([c["readability"]["reading_minutes"] for c in ok]),
        "tables_on_one_line": sum(1 for c in ok if c["readability"]["table_on_one_line"]),
        "title_repeated_as_heading": sum(1 for c in ok if c["readability"]["repeats_title_heading"]),
        "avg_outline_s": mean([t.get("outline_s") for t in topics]),
    }


def markdown(label: str, topics: list[dict], summary: dict) -> str:
    out = [f"# Baseline: {label}", "", f"Run: {datetime.now():%Y-%m-%d %H:%M}. Five frozen topics, {CHAPTERS_PER_TOPIC} chapters each, real models.", "",
           "## Summary", ""] + [f"- **{k}**: {v}" for k, v in summary.items()] + ["", "## Per chapter", "",
           "| topic | ch | status | unverified | build s | fallback | claims ok | sent words | longest para | min | table 1-line | title repeat |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in topics:
        for c in t["chapters"]:
            r, cl = c.get("readability", {}), c.get("claims", {})
            out.append(f"| {t['topic']} | {c['position']} | {c.get('status')} | {c.get('unverified')} | {c.get('build_s')} | {c.get('fallback_rate')} | {cl.get('supported_share')} | "
                       f"{r.get('avg_sentence_words')} | {r.get('longest_paragraph_words')} | {r.get('reading_minutes')} | {r.get('table_on_one_line')} | {r.get('repeats_title_heading')} |")
        if t.get("error"):
            out.append(f"| {t['topic']} | - | ERROR | | | | | | | | | {t['error']} |")
    return "\n".join(out) + "\n"


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--only", help="comma-separated topic ids (for a quick trial; the real baseline runs all five)")
    args = ap.parse_args()
    store = Store(":memory:")
    router = ModelRouter(store=store)
    if not router.model_ready():
        print("No AI model is connected: set GEMINI_API_KEY in .env first.")
        return 2
    engine = Engine(store, router, upload_dir=str(ROOT / "eval" / "baseline" / "_uploads"), retry_delay_s=0, tools=ToolRunner(store, router.sink, router.chaos))
    wanted = set(args.only.split(",")) if args.only else None
    results = []
    for topic in TOPICS:
        if wanted and topic["id"] not in wanted:
            continue
        print(f"[baseline] {topic['id']} ...", flush=True)
        try:
            r = run_topic(topic, router, store, engine)
        except Exception as e:  # noqa: BLE001
            r = {"topic": topic["id"], "chapters": [], "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-800:]}
        results.append(r)
        print(f"[baseline] {topic['id']}: {len(r['chapters'])} chapters, {r.get('total_s')}s {r.get('error', '')}", flush=True)
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / f"{args.label}.partial.json").write_text(json.dumps({"topics": results}, indent=1, default=str), encoding="utf-8")
    summary = summarise(results)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{args.label}.json").write_text(json.dumps({"label": args.label, "summary": summary, "topics": results}, indent=1, default=str), encoding="utf-8")
    (OUT / f"{args.label}.md").write_text(markdown(args.label, results, summary), encoding="utf-8")
    (OUT / f"{args.label}.partial.json").unlink(missing_ok=True)
    print(markdown(args.label, results, summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
