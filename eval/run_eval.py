"""Quality metrics for Aasaan (PRD section 11), run against the REAL models (needs keys, uses quota).

    python eval/run_eval.py                 # needs GEMINI_API_KEY in .env

The expected labels in eval/data/graded_answers.json are DRAFTS until a teacher confirms them.
Writes eval/report.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agents import critic, diagnoser, intake  # noqa: E402
from app.db import Store  # noqa: E402
from app.engine import Engine  # noqa: E402
from app.main import load_env  # noqa: E402
from app.router import ModelRouter  # noqa: E402
from app.schemas import ChapterContent, LearnerProfile, Question  # noqa: E402
from app.worker import Worker  # noqa: E402

DATA = ROOT / "eval" / "data"
SAMPLE = (ROOT / "samples" / "motion_chapter.txt").read_text(encoding="utf-8")
LEARNER = "eval-learner"


def pct(n: int, d: int) -> float:
    return 100.0 * n / d if d else 0.0


def fresh(mode=None):
    store = Store(":memory:")
    router = ModelRouter(store=store)
    engine = Engine(store, router, upload_dir=tempfile.mkdtemp(), retry_delay_s=0)
    return store, router, engine, Worker(engine)


def build(engine, worker, text=SAMPLE):
    nid = engine.create_notebook(LEARNER, text, "en", [])
    worker.run_until_idle()
    return nid


def wrong_answer_flow(engine, store, nid):
    ch = engine.notebook_view(nid)["chapters"][0]
    qb = next(b for b in store.blocks(ch["id"]) if b["type"] == "quiz")
    q = qb["content"]["questions"][0]
    sid = engine.open_session(LEARNER)
    engine.answer(LEARNER, sid, ch["id"], q["id"], next(o for o in q["options"] if o != q["answer_key"]))
    for other in qb["content"]["questions"][1:]:
        engine.answer(LEARNER, sid, ch["id"], other["id"], other["answer_key"])
    return ch, sid, engine.finish_chapter(LEARNER, sid, ch["id"])


# ------------------------------------------------------------------ metrics
def m_grading(router):
    d = json.loads((DATA / "graded_answers.json").read_text(encoding="utf-8"))
    qs = {q["id"]: Question.model_validate(q) for q in d["questions"]}
    agree = sum(int((diagnoser.run(router, qs[a["question_id"]], a["answer"], []).value.result == "correct") == (a["expected"] == "correct"))
                for a in d["answers"])
    n = len(d["answers"])
    return f"{agree}/{n} = {pct(agree, n):.0f}%", pct(agree, n) >= 80, "target >= 80% (labels are drafts until a teacher confirms)"


def m_critic(router, mode):
    items = json.loads((DATA / "seeded_lessons.json").read_text(encoding="utf-8"))["lessons"]
    chunks_all, _ = intake.chunk_pages("s1", intake.pages_from_text(SAMPLE))
    prof = LearnerProfile(learner_id="eval")
    caught = flawed = approved = good = 0
    used = Counter()
    for it in items:
        chunks = [c for c in chunks_all if c["page"] == it["page"]]
        notes = "## Seeded chapter\n\n" + "\n\n".join(it["steps"])
        content = ChapterContent.model_validate({
            "notes_md": notes.ljust(130, "."), "summary": it["steps"][0], "key_points": it["steps"][:2], "source_refs": [chunks[0]["chunk_id"]],
            "slides": [{"title": f"Slide {i}", "points": [s, "See the notes"]} for i, s in enumerate(it["steps"], 1)]})
        res = critic.run(router, content, chunks, prof)
        used[res.model_used] += 1
        if it["flaw"]:
            flawed += 1
            caught += int(res.value.verdict != "approve")
        else:
            good += 1
            approved += int(res.value.verdict == "approve")
    ok = pct(caught, flawed) >= 80 and pct(approved, good) >= 80
    return f"caught {caught}/{flawed}, approved {approved}/{good} (models: {dict(used)})", ok, "target >= 8/10 each; rule_checks cannot catch factual errors"


def m_source_refs(mode):
    store, router, engine, worker = fresh(mode)
    nid = build(engine, worker)
    known = {c["chunk_id"] for s in store.sources(nid) for c in s["chunks"]}
    chs = engine.notebook_view(nid)["chapters"]
    good = 0
    for c in chs:
        notes = next(b for b in store.blocks(c["id"]) if b["type"] == "notes")
        refs = notes["meta"].get("refs", [])
        good += int(bool(refs) and all(r in known or r.startswith("url:") or r.startswith("w") for r in refs))
    return f"{good}/{len(chs)} chapters", good == len(chs), "target 100%"


def m_fallback(mode):
    out = {}
    for label, kill in [("primary dead", ["gemma_31b"]), ("all models dead", None)]:
        _, router, engine, worker = fresh(mode)
        for m in (kill if kill else list(router.cfg["models"])):
            router.kill(m)
        try:
            nid = build(engine, worker)
            v = engine.notebook_view(nid)
            out[label] = "pass" if v["open_ready"] else f"FAIL ({v['status']}: {v['error']})"
        except Exception as e:
            out[label] = f"FAIL ({type(e).__name__}: {e})"
    return "; ".join(f"{k}: {v}" for k, v in out.items()), all(v == "pass" for v in out.values()), "notebook opens (with source material, code-only fallbacks allowed)"


def m_review(mode):
    store, router, engine, worker = fresh(mode)
    nid = build(engine, worker)
    before = len(engine.notebook_view(nid)["chapters"])
    _, _, fin = wrong_answer_flow(engine, store, nid)
    after = len(engine.notebook_view(nid)["chapters"])
    return f"{before} -> {after} chapters ({[a['title'][:40] for a in fin['added']]})", after > before, "course gains a review item after a wrong answer"


def m_memory(mode):
    store, router, engine, worker = fresh(mode)
    nid = build(engine, worker)
    _, sid, _ = wrong_answer_flow(engine, store, nid)
    engine.close_session(LEARNER, sid, nid)
    engine.open_session(LEARNER)
    msg = engine.companion_open(LEARNER, nid)["message"]
    label = (store.mistake_labels(LEARNER) or [None])[0]
    return f'"{msg[:70]}"', bool(label) and label in msg, "second session references an earlier mistake"


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.parse_args()
    store, router, engine, worker = fresh()
    if not router.model_ready():
        print("No AI model is connected: set GEMINI_API_KEY in .env first.")
        return 2
    mode = router.mode
    rows = [("Grading agreement (20 answers)", *m_grading(router)), ("Critic catches seeded errors", *m_critic(router, mode)),
            ("Chapters with valid source refs", *m_source_refs(mode)), ("Fallback: kill models, notebook still opens", *m_fallback(mode)),
            ("Course gains a review item after a wrong answer", *m_review(mode)), ("Second session mentions earlier mistake", *m_memory(mode))]
    lines = ["# Evaluation report", "", f"Run: {datetime.now():%Y-%m-%d %H:%M}  |  mode: **{mode}**", "",
             "| Metric | Result | Status | Note |", "|---|---|---|---|"]
    failed = 0
    for name, result, ok, note in rows:
        status = "SKIPPED" if ok is None else ("PASS" if ok else "FAIL")
        failed += int(ok is False)
        lines.append(f"| {name} | {result} | {status} | {note} |")
    report = "\n".join(lines)
    (ROOT / "eval" / "report.md").write_text(report + "\n", encoding="utf-8")
    print(report)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
