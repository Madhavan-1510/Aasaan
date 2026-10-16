"""Pre-warm the demo cache: run the sample chapter through every node with the real models and
export the approved lessons and quizzes to data/demo_cache.json. The app loads that file at startup,
so the Teacher/Quizzer last resorts return pre-checked content if a model fails during the demo.

    python scripts/warm_cache.py --mode live
    python scripts/warm_cache.py --mode mock --allow-mock    # plumbing test only, do not ship
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.db import Store  # noqa: E402
from app.main import load_env  # noqa: E402
from app.orchestrator import Swarm  # noqa: E402
from app.router import ModelRouter  # noqa: E402

OUT = ROOT / "data" / "demo_cache.json"


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mock", "live"], default=None)
    ap.add_argument("--allow-mock", action="store_true")
    ap.add_argument("--language", default="en")
    ap.add_argument("--chapter", default=str(ROOT / "samples" / "motion_chapter.txt"))
    args = ap.parse_args()

    store = Store(":memory:")
    router = ModelRouter(store=store, mode=args.mode)
    if router.mode == "mock" and not args.allow_mock:
        print("Refusing to build a demo cache from mock output. Add keys and use --mode live (or --allow-mock).")
        return 2
    swarm = Swarm(store, router)
    text = Path(args.chapter).read_text(encoding="utf-8")
    lid = swarm.create_learner(9, args.language, None)
    out = swarm.start(lid, None, text, None, None, args.language, None)
    pid = out["path"]["path_id"]
    unverified = []
    for n in out["path"]["nodes"]:
        les = swarm.lesson(lid, pid, n["id"])
        swarm.quiz(lid, pid, n["id"])
        status = "verified" if les["verified"] else "UNVERIFIED (not cached)"
        print(f"{n['id']}: {n['title'][:50]:50} {status}")
        if not les["verified"]:
            unverified.append(n["id"])
    cache = store.cache_dump()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {len(cache)} cache entries to {OUT.relative_to(ROOT)} (mode: {router.mode})")
    if unverified:
        print("Warning: some lessons were not approved by the Critic and were not cached:", unverified)
    return 0


if __name__ == "__main__":
    sys.exit(main())
