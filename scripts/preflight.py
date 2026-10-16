"""Pre-demo check against a RUNNING server: walks the whole demo flow over HTTP, then shows the
fallback by killing the primary model. Prints PASS/FAIL per step.

    python scripts/preflight.py [--url http://localhost:8000]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = (ROOT / "samples" / "motion_chapter.txt").read_text(encoding="utf-8")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    c = httpx.Client(base_url=ap.parse_args().url, timeout=120)
    try:
        h = c.get("/api/health").json()
    except httpx.HTTPError as e:
        print(f"Server not reachable: {e}")
        return 2
    check("server up", h["ok"], f"(mode: {h['mode']})")
    c.post("/api/admin/revive", json={})

    lid = c.post("/api/learners", json={"grade": 9, "language": "en"}).json()["learner_id"]
    t0 = time.time()
    st = c.post("/api/start", data={"learner_id": lid, "text": SAMPLE, "language": "en", "goal": "exam on Friday"}).json()
    p = st["path"]
    check("path built", 1 <= len(p["nodes"]) <= 8, f"({len(p['nodes'])} steps, {time.time() - t0:.1f}s, target <= 60s)")
    pid, nid, sid = p["path_id"], p["current"], st["session_id"]

    t0 = time.time()
    les = c.post("/api/lesson", json={"learner_id": lid, "path_id": pid, "node_id": nid}).json()
    check("lesson ready", 3 <= len(les["lesson"]["steps"]) <= 5,
          f"(verified={les['verified']}, {time.time() - t0:.1f}s, target <= 20s)")

    doubt = c.post("/api/doubt", data={"learner_id": lid, "path_id": pid, "node_id": nid, "text": "What is velocity?"}).json()
    check("doubt answered", bool(doubt.get("answer")), "(audio: " + ("yes" if doubt.get("audio_b64") else "no, text only") + ")")

    q = c.post("/api/quiz", json={"learner_id": lid, "path_id": pid, "node_id": nid}).json()["questions"]
    check("quiz hides answer keys", all("answer_key" not in x and "hint" not in x for x in q))
    r = c.post("/api/answer", json={"learner_id": lid, "path_id": pid, "node_id": nid, "session_id": sid,
                                    "question_id": q[0]["id"], "answer": q[0]["options"][-1]}).json()
    check("wrong answer names a misconception", r["result"] == "wrong" and bool(r["misconception"]), f"({r['misconception']})")
    fin = c.post("/api/finish-node", json={"learner_id": lid, "path_id": pid, "node_id": nid, "session_id": sid}).json()
    check("path updated with a review step", len(fin["path"]["nodes"]) == len(p["nodes"]) + 1, f"({fin['reason']})")
    c.post("/api/session/close", json={"learner_id": lid, "session_id": sid, "path_id": pid})
    s2 = c.post("/api/session/open", json={"learner_id": lid}).json()
    check("session 2 mentions earlier mistake", s2["returning"] and any(m in s2["message"] for m in s2["mistake_labels"]),
          f'("{s2["message"][:60]}")')

    # fallback demo: kill the primary and run one more node
    c.post("/api/admin/kill", json={"model": "gemma_31b"})
    nxt = fin["next_node"]
    try:
        les2 = c.post("/api/lesson", json={"learner_id": lid, "path_id": pid, "node_id": nxt}).json()
        ev = c.get("/api/events", params={"limit": 8}).json()
        used = {e["model_used"] for e in ev if e["agent"] in ("teacher", "critic")}
        check("fallback with primary dead", bool(les2["lesson"]["steps"]), f"(served by {sorted(used)})")
    finally:
        c.post("/api/admin/revive", json={})
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
