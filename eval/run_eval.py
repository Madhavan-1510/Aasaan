"""Phase 6 evaluation: runs the PRD section 11 metrics and writes eval/report.md.

    python eval/run_eval.py                 # mode from env (mock if no GEMINI_API_KEY)
    python eval/run_eval.py --mode live     # real models; needs keys

In mock mode the Critic always approves, so the Critic metric is reported as SKIPPED (not meaningful).
The expected labels in eval/data/graded_answers.json are DRAFTS until a teacher confirms them.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.agents import critic, diagnoser, intake  # noqa: E402
from app.db import Store  # noqa: E402
from app.main import load_env  # noqa: E402
from app.orchestrator import Engine  # noqa: E402
from app.router import AllFailed, ModelRouter  # noqa: E402
from app.schemas import LearnerProfile, LessonOut, Question  # noqa: E402

DATA = ROOT / "eval" / "data"
SAMPLE = (ROOT / "samples" / "motion_chapter.txt").read_text(encoding="utf-8")


def pct(n: int, d: int) -> float:
    return 100.0 * n / d if d else 0.0


def fresh(mode: str | None) -> tuple[Store, ModelRouter, Engine]:
    store = Store(":memory:")
    router = ModelRouter(store=store, mode=mode)
    return store, router, Engine(store, router)


def run_flow(engine: Engine, wrong_first: bool = True) -> dict:
    """One full pass over the first node. Returns facts used by several metrics."""
    lid = engine.create_learner(9, "en", "exam on Friday")
    out = engine.start(lid, None, SAMPLE, None, None, "en", "exam on Friday")
    pid, nid, sid = out["path"]["path_id"], out["path"]["current"], out["session_id"]
    les = engine.lesson(lid, pid, nid)
    engine.quiz(lid, pid, nid)
    quiz = engine.store.get_quiz(pid, nid)
    n_before = len(out["path"]["nodes"])
    for i, q in enumerate(quiz["questions"]):
        ans = q["options"][-1] if (wrong_first and i == 0) else q["answer_key"]
        engine.answer(lid, sid, pid, nid, q["id"], ans)
    fin = engine.finish_node(lid, sid, pid, nid)
    engine.close_session(lid, sid, pid)
    return {"lid": lid, "pid": pid, "nid": nid, "lesson": les, "n_before": n_before,
            "n_after": len(fin["path"]["nodes"]), "patch": fin["patch"]}


# ------------------------------------------------------------------ metrics
def m_grading(router: ModelRouter) -> tuple[str, bool, str]:
    d = json.loads((DATA / "graded_answers.json").read_text(encoding="utf-8"))
    qs = {q["id"]: Question.model_validate(q) for q in d["questions"]}
    agree = 0
    for a in d["answers"]:
        got = diagnoser.run(router, qs[a["question_id"]], a["answer"], []).value.result
        agree += int((got == "correct") == (a["expected"] == "correct"))
    n = len(d["answers"])
    return f"{agree}/{n} = {pct(agree, n):.0f}%", pct(agree, n) >= 80, "target >= 80% (labels are drafts until a teacher confirms)"


def m_critic(router: ModelRouter, mode: str) -> tuple[str, bool | None, str]:
    if mode == "mock":
        return "SKIPPED", None, "mock Critic always approves; run with --mode live"
    d = json.loads((DATA / "seeded_lessons.json").read_text(encoding="utf-8"))["lessons"]
    pages_text = intake.pages_from_text(SAMPLE)
    chunks_all, _ = intake.chunk_pages("s1", pages_text)
    prof = LearnerProfile(learner_id="eval", grade=9, language="en")
    caught = flawed = approved = good = 0
    used = Counter()
    for item in d:
        chunks = [c for c in chunks_all if c["page"] == item["page"]]
        lesson = LessonOut.model_validate({
            "node_id": "n1", "language": "en",
            "lesson": {"title": "Seeded", "steps": [{"type": t, "text": s} for t, s in
                       zip(["explain", "example", "check"], item["steps"])],
                       "key_points": [], "source_refs": [{"source_id": "s1", "page": item["page"],
                                                           "chunk_id": chunks[0]["chunk_id"]}]}})
        res = critic.run(router, lesson, chunks, prof)
        used[res.model_used] += 1
        if item["flaw"]:
            flawed += 1
            caught += int(res.value.verdict != "approve")
        else:
            good += 1
            approved += int(res.value.verdict == "approve")
    ok = pct(caught, flawed) >= 80 and pct(approved, good) >= 80
    return (f"caught {caught}/{flawed}, approved {approved}/{good} (models: {dict(used)})", ok,
            "target >= 8/10 each; rule_checks cannot catch factual errors")


def m_source_refs(engine: Engine) -> tuple[str, bool, str]:
    lid = engine.create_learner(9, "en", None)
    out = engine.start(lid, None, SAMPLE, None, None, "en", None)
    pid = out["path"]["path_id"]
    src = engine.store.get_source(engine.store.get_path(pid)["source_id"])
    known = {c["chunk_id"] for c in src["chunks"]}
    total = good = 0
    for n in out["path"]["nodes"]:
        les = engine.lesson(lid, pid, n["id"])["lesson"]
        total += 1
        good += int(bool(les["source_refs"]) and all(r["chunk_id"] in known for r in les["source_refs"]))
    return f"{good}/{total} lessons", good == total, "target 100%"


def m_fallback(mode: str) -> tuple[str, bool, str]:
    results = {}
    for label, kills in [("primary dead", ["gemma_31b"]),
                         ("all models dead", list(fresh(mode)[1].cfg["models"]))]:
        _, router, engine = fresh(mode)
        for k in kills:
            router.kill(k)
        try:
            run_flow(engine)
            results[label] = "pass"
        except (AllFailed, Exception) as e:  # any crash is a failure of the fallback promise
            results[label] = f"FAIL ({type(e).__name__}: {e})"
    return "; ".join(f"{k}: {v}" for k, v in results.items()), all(v == "pass" for v in results.values()), "flow completes"


def m_path_changes(engine: Engine) -> tuple[str, bool, str]:
    r = run_flow(engine, wrong_first=True)
    ok = r["n_after"] > r["n_before"]
    return f"{r['n_before']} -> {r['n_after']} nodes", ok, "path visibly changes after a wrong answer"


def m_memory(engine: Engine) -> tuple[str, bool, str]:
    r = run_flow(engine, wrong_first=True)
    s2 = engine.open_session(r["lid"])
    label = (engine.store.mistake_labels(r["lid"]) or [None])[0]
    ok = bool(label) and label in s2["message"]
    return f'"{s2["message"][:70]}"', ok, "second session references an earlier mistake"


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mock", "live"], default=None)
    args = ap.parse_args()
    store, router, engine = fresh(args.mode)
    mode = router.mode
    rows = [
        ("Grading agreement (20 answers)", *m_grading(router)),
        ("Critic catches seeded errors", *m_critic(router, mode)),
        ("Lessons with valid source refs", *m_source_refs(engine)),
        ("Fallback: kill models, flow completes", *m_fallback(mode)),
        ("Path changes after a wrong answer", *m_path_changes(fresh(mode)[2])),
        ("Second session mentions earlier mistake", *m_memory(fresh(mode)[2])),
    ]
    lines = [f"# Evaluation report", "", f"Run: {datetime.now():%Y-%m-%d %H:%M}  |  mode: **{mode}**", "",
             "| Metric | Result | Status | Note |", "|---|---|---|---|"]
    failed = 0
    for name, result, ok, note in rows:
        status = "SKIPPED" if ok is None else ("PASS" if ok else "FAIL")
        failed += int(ok is False)
        lines.append(f"| {name} | {result} | {status} | {note} |")
    if mode == "mock":
        lines += ["", "> Mock mode: this proves the plumbing only. Model-quality metrics need `--mode live`."]
    report = "\n".join(lines)
    (ROOT / "eval" / "report.md").write_text(report + "\n", encoding="utf-8")
    print(report)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
