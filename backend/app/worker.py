"""Background job worker: a simple in-process asyncio loop over the `jobs` table (no Redis).

- Picks due jobs, most urgent first (chapter priority, then notebook order), up to `concurrency` at a time
  (default 2) so model rate limits are respected.
- Each job runs in a thread (`Engine.process_job`), which retries with backoff and marks failures visibly.
- On startup, jobs/chapters caught mid-flight by a restart are put back in the queue (resumable).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from .engine import Engine

log = logging.getLogger("aasaan.worker")


class Worker:
    def __init__(self, engine: Engine, concurrency: Optional[int] = None, poll_s: float = 0.4):
        self.engine = engine
        self.concurrency = max(1, concurrency or int(os.environ.get("AASAAN_CHAPTER_CONCURRENCY", "2")))
        self.notebook_lane = max(1, int(os.environ.get("AASAAN_NOTEBOOK_CONCURRENCY", "2")))
        self.poll_s = poll_s
        self.active: dict[int, str] = {}  # job id -> kind
        self._stop = False

    def recover(self) -> int:
        n = self.engine.store.recover_after_restart()
        if n:
            log.info("[aasaan] re-queued %d job(s) interrupted by a restart", n)
        return n

    async def run(self) -> None:
        self.recover()
        log.info("[aasaan] worker started (concurrency %d)", self.concurrency)
        tasks: set[asyncio.Task] = set()
        while not self._stop:
            # two lanes: chapters (default 2 at a time) and notebooks (outline + first chapter). A student's new notebook
            # must not wait behind background chapters of an old one.
            counts = {"notebook": 0, "chapter": 0}
            for kind in self.active.values():
                counts[kind] += 1
            limits = {"notebook": self.notebook_lane, "chapter": self.concurrency}
            for job in self.engine.store.pending_jobs():
                kind = "notebook" if job["kind"] == "notebook" else "chapter"
                if job["id"] in self.active or counts[kind] >= limits[kind]:
                    continue
                self.active[job["id"]] = kind
                counts[kind] += 1
                t = asyncio.create_task(self._run_one(job["id"]))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            await asyncio.sleep(self.poll_s)
        for t in list(tasks):
            t.cancel()

    async def _run_one(self, jid: int) -> None:
        try:
            await asyncio.to_thread(self.engine.process_job, jid)
        except Exception:  # process_job records failures itself; this is a last guard
            log.exception("[aasaan] job %s crashed outside process_job", jid)
        finally:
            self.active.pop(jid, None)

    def stop(self) -> None:
        self._stop = True

    # ---- synchronous helper for tests and scripts
    def run_until_idle(self, max_jobs: int = 500) -> int:
        """Process due jobs one by one until none are left. Returns how many ran."""
        n = 0
        while n < max_jobs:
            due = self.engine.store.pending_jobs()
            if not due:
                break
            self.engine.process_job(due[0]["id"])
            n += 1
        return n
