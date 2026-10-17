"""Aasaan engine: notebooks built progressively by a swarm of agents.

Phase 0  outline      Intake reads the inputs, Architect plans 8-20 chapters (roadmap + graph).
Phase 1  chapter 1    Curator -> Teacher <-> Critic (max 2 revisions) -> Quizzer; the notebook opens when it is ready.
Phase 2  background   the remaining chapters are built by the worker (jobs table), student-priority first.

State lives in SQLite, so everything is resumable after a restart. The UI never sees agent names.
"""
from __future__ import annotations

import base64
import contextvars
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from . import web
from .agents import architect, companion, critic, curator, diagnoser, doubt, gates, illustrator, intake, quizzer, rewriter, teacher
from .db import Store
from .tools import ToolRunner
from .router import AllFailed, ModelRouter
from .schemas import (MAX_INSERTS_PER_SESSION, ChapterContent, CriticOut, IntakeOut, LearnerProfile, OutlineOut,
                      Question, QuizOut, resolve_key)
from .voice import Voice

log = logging.getLogger("aasaan.engine")

MAX_REVISIONS = 2          # Teacher <-> Critic loops before shipping the best draft as unverified
MAX_JOB_ATTEMPTS = 3
MAX_SOURCES = 6
MAX_FILE_BYTES = 15 * 1024 * 1024
MATERIAL_MIN_CHARS = 600   # pasted text longer than this is treated as study material, not as a goal
READINESS_WEIGHT = {"queued": 0.0, "drafting": 0.4, "checking": 0.8, "ready": 1.0, "failed": 0.0}


def friendly_error(e: Exception, notebook: bool = True) -> str:
    """What the student sees when a build fails. The technical detail stays in the job row, the log and `events`."""
    text = str(e)
    if isinstance(e, AllFailed) or "cannot plan" in text or "every step in the chain failed" in text:
        return ("I couldn't reach any AI model to plan your course. Please try again in a moment. "
                "(If this keeps happening, the server's model keys need checking: /api/health/models.)")
    if "couldn't read any of your material" in text:
        return "I couldn't read any of your material. Please paste the text or attach a clearer photo or PDF."
    return "Something went wrong while building this. Please try again."


class UserError(Exception):
    """Bad request or missing data; surfaced as HTTP 400/404."""


def split_request(text: str) -> tuple[Optional[str], str, list[str]]:
    """The chat box holds a goal, pasted material, links, or a mix. Returns (goal, material_text, urls)."""
    text = (text or "").strip()
    urls = web.extract_urls(text)
    rest = web.URL_RE.sub("", text).strip()
    if len(rest) >= MATERIAL_MIN_CHARS:
        return None, rest, urls
    return (rest or None), "", urls


def search_topic(nb: dict) -> str:
    """Short subject for web searches. A long personal goal ("I'm part of the training for ... i need a roadmap") would poison
    every query, so past a short single line the notebook title (the Architect's name for the subject) is used instead."""
    goal = (nb.get("goal") or "").strip()
    return goal if goal and len(goal) <= 60 and "\n" not in goal else (nb.get("title") or goal)[:60]


class Engine:
    def __init__(self, store: Store, router: ModelRouter, upload_dir: str | Path = "data/uploads",
                 retry_delay_s: float = 4.0, tools: Optional[ToolRunner] = None):
        self.store, self.router = store, router
        self.tools = tools
        if tools is not None:
            web.set_runner(tools)
        web.set_page_gate(lambda url, title, text: gates.page_health(router, url, title, text))   # page_health after each fetch tier
        self.voice = Voice(router)
        self.upload_dir = Path(upload_dir)
        self.retry_delay_s = retry_delay_s

    # ================================================================= helpers
    def profile(self, learner_id: str, nb: Optional[dict] = None) -> LearnerProfile:
        return LearnerProfile(learner_id=learner_id, language=(nb or {}).get("language") or "en",
                              level=(nb or {}).get("level") or "beginner", goal=(nb or {}).get("goal"),
                              mistake_labels=self.store.mistake_labels(learner_id))

    def _nb(self, nid: str) -> dict:
        nb = self.store.get_notebook(nid)
        if not nb:
            raise UserError("unknown notebook")
        return nb

    def _chapter(self, cid: int) -> dict:
        ch = self.store.get_chapter(cid)
        if not ch:
            raise UserError("unknown chapter")
        return ch

    def _all_chunks(self, nid: str) -> list[dict]:
        out = []
        for s in self.store.sources(nid):
            for c in s["chunks"]:
                out.append({**c, "source_id": s["id"]})
        return out

    def _chapter_chunks(self, ch: dict, all_chunks: list[dict], k: int = 6) -> list[dict]:
        """The student's own material most relevant to a chapter: cited chunks first, then keyword overlap."""
        by_id = {c["chunk_id"]: c for c in all_chunks}
        picked = [by_id[r] for r in ch.get("source_refs", []) if r in by_id][:8]
        if len(picked) < 3 and all_chunks:
            words = set(re.findall(r"\w{4,}", (ch["title"] + " " + " ".join(ch.get("objectives", [])) + " " + ch.get("summary", "")).lower()))
            ranked = sorted((c for c in all_chunks if c not in picked),
                            key=lambda c: -len(words & set(re.findall(r"\w{4,}", c["text"].lower()))))
            for c in ranked:
                if len(picked) >= k:
                    break
                if words & set(re.findall(r"\w{4,}", c["text"].lower())):
                    picked.append(c)
        return picked

    # ============================================================= notebooks
    def create_notebook(self, learner_id: str, text: str, language: str, files: list[tuple[str, str, bytes]]) -> str:
        """Validate the chat-box request, store the inputs, queue phase 0+1. Returns the notebook id immediately."""
        goal, material, urls = split_request(text)
        if not (goal or material or urls or files):
            raise UserError("Type a goal, paste notes or links, or attach a photo or PDF.")
        if len(files) > MAX_SOURCES:
            raise UserError(f"Please attach at most {MAX_SOURCES} files.")
        for name, ctype, data in files:
            if len(data) > MAX_FILE_BYTES:
                raise UserError(f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
            if not (ctype == "application/pdf" or ctype.startswith("image/") or name.lower().endswith(".pdf")):
                raise UserError(f"{name}: only photos and PDFs can be attached.")
        if goal and not (material or urls or files):   # input_gate: only a confident "unsafe" or "not a request" stops a plain goal
            refusal = gates.input_gate(self.router, goal)
            if refusal:
                raise UserError(refusal)
        title = (goal or (urls[0] if urls else None) or (files[0][0] if files else None) or "New notebook")[:60]
        nid = self.store.create_notebook(learner_id, goal, language or "en", title)
        if material:
            self.store.add_source(nid, "text", "pasted", "Pasted notes", material, [])
        for u in urls[:MAX_SOURCES]:
            self.store.add_source(nid, "url", u, u, "", [])
        folder = self.upload_dir / nid
        for name, ctype, data in files:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / re.sub(r"[^\w.\-]", "_", name)[:80]
            path.write_bytes(data)
            is_pdf = ctype == "application/pdf" or name.lower().endswith(".pdf")
            self.store.add_source(nid, "pdf" if is_pdf else "image", str(path), name, "", [])
        self.store.add_job(nid, None, "notebook")
        return nid

    def delete_notebook(self, nid: str) -> None:
        self._nb(nid)
        self.store.delete_notebook(nid)

    # ================================================================== jobs
    def process_job(self, jid: int) -> None:
        """Run one job. Failures are retried with backoff; after MAX_JOB_ATTEMPTS the chapter/notebook is marked failed
        with the reason (never left stuck in drafting)."""
        job = self.store.get_job(jid)
        if not job or job["state"] not in ("queued", "running"):
            return
        attempts = job["attempts"] + 1
        self.store.set_job(jid, "running", attempts=attempts)
        try:
            if job["kind"] == "notebook":
                self._run_notebook_job(job["notebook_id"])
            elif job["kind"] == "chapter":
                self.build_chapter(job["chapter_id"])
            self.store.set_job(jid, "done")
        except Exception as e:  # any failure is recorded, never swallowed
            msg = f"{type(e).__name__}: {e}"[:500]
            log.warning("[aasaan] job %s (%s) attempt %d failed: %s", jid, job["kind"], attempts, msg)
            if attempts < MAX_JOB_ATTEMPTS:
                self.store.set_job(jid, "queued", msg, attempts=attempts, delay_s=self.retry_delay_s * attempts)
                if job["chapter_id"]:
                    self.store.set_chapter_status(job["chapter_id"], "queued", msg)
            else:
                self.store.set_job(jid, "failed", msg, attempts=attempts)
                if job["chapter_id"]:
                    self.store.set_chapter_status(job["chapter_id"], "failed", friendly_error(e, notebook=False))
                if job["kind"] == "notebook":
                    chs = self.store.chapters(job["notebook_id"])
                    first = chs[0] if chs else None
                    if first:
                        self.store.set_chapter_status(first["id"], "failed", friendly_error(e))
                    self.store.update_notebook(job["notebook_id"], status="failed", error=friendly_error(e))
        self._refresh_status(job["notebook_id"])

    def _run_notebook_job(self, nid: str) -> None:
        nb = self._nb(nid)
        if not self.store.chapters(nid):
            self.store.update_notebook(nid, status="outlining", error="")
            self.phase0(nid)
        chapters = self.store.chapters(nid)
        first = chapters[0]
        self.store.update_notebook(nid, status="drafting_first", error="")
        if first["status"] != "ready":
            self.build_chapter(first["id"])
        # phase 2: queue the rest (idempotent: skip chapters that already have a live job or are ready)
        for ch in self.store.chapters(nid)[1:]:
            j = self.store.job_for_chapter(ch["id"])
            if ch["status"] != "ready" and not (j and j["state"] in ("queued", "running")):
                self.store.add_job(nid, ch["id"], "chapter")
        self._refresh_status(nid)

    # ============================================================== phase 0
    def _scope(self, nid: str):
        """Tool calls made inside count against this notebook's per-tool budgets (e.g. Firecrawl: 20)."""
        import contextlib
        return self.tools.notebook_scope(nid) if self.tools else contextlib.nullcontext()

    def phase0(self, nid: str) -> None:
        with self._scope(nid):
            return self._phase0(nid)

    def _phase0(self, nid: str) -> None:
        nb = self._nb(nid)
        prof = self.profile(nb["learner_id"], nb)
        warnings: list[str] = []
        topics: list[dict] = []
        for src in self.store.sources(nid)[:MAX_SOURCES]:
            try:
                got = self._read_source(src, nb, warnings)
            except AllFailed:
                warnings.append(f"Could not read {src['title'][:60]}")
                continue
            if not got:
                continue
            res: IntakeOut = got
            self.store.update_source_chunks(src["id"], [c.model_dump() for c in res.chunks])
            for t in res.topics:
                topics.append({**t.model_dump(), "source_id": src["id"]})
            warnings += [w for w in res.warnings if not re.match(r"page \d+ unreadable or empty$", w)]   # shown once per file in Resources instead
        chunks = self._all_chunks(nid)
        self.store.cache_set(f"nbwarn:{nid}", warnings)
        if not (nb["goal"] or topics):
            raise ValueError("I couldn't read any of your material. Please paste the text or attach a clearer file.")
        scope = gates.scoper(self.router, nb["goal"] or "", topics)
        if scope:
            self.router.note("scoper", "ok", f"scoped as {scope.get('track', '?')} / {scope.get('level', '?')}", f"decided by {scope['by']}")
            if scope.get("level") and not nb.get("level"):
                prof.level = scope["level"]
        res = architect.run(self.router, nb["goal"], topics, chunks, prof, nb["title"], (scope or {}).get("track"))
        out: OutlineOut = res.value
        key_to_id: dict[str, int] = {}
        for i, c in enumerate(out.chapters, start=1):
            key_to_id[c.key] = self.store.add_chapter(nid, i, c.title, c.summary, c.objectives, [], c.source_refs,
                                                      priority=100 if i == 1 else 0)
        for c in out.chapters:
            self.store.set_chapter_prereqs(key_to_id[c.key], [key_to_id[p] for p in c.prereqs])
        self.store.update_notebook(nid, title=out.title[:80], description=out.description, level=out.level,
                                   goal=nb["goal"] or out.title)

    def _read_source(self, src: dict, nb: dict, warnings: list[str]) -> Optional[IntakeOut]:
        """Turn one source into chunks and topics. Text/PDF/URL chunking is code; photos go to the vision model."""
        kind, sid, lang = src["kind"], src["id"], nb["language"]
        pages: list[str] = []
        images = None
        if kind == "text":
            pages = intake.pages_from_text(src["extracted_text"])
        elif kind == "url":
            page = web.fetch_page(src["raw_ref"], self.store)
            if not page.ok:
                warnings.append(page.error)
                return None
            self.store.set_source_text(src["id"], page.title or src["title"], page.text)
            pages = intake.pages_from_text(page.text)
        elif kind == "pdf":
            try:
                data = Path(src["raw_ref"]).read_bytes()
                pages, empties = intake.analyze_pdf(data)
            except Exception as e:
                warnings.append(f"Could not read PDF {src['title']}: {e}")
                self.store.set_source_meta(sid, {"status": "unreadable", "issues": [{"kind": "unreadable", "pages": [], "reason": str(e)[:200]}]})
                return None
            pages, meta = self._recover_pages(data, pages, empties, lang, src["title"])
            in_file = intake.pdf_page_count(data)
            if in_file > len(pages):   # intake reads a bounded number of pages: say so instead of silently ignoring the rest
                meta["pages_total"] = in_file
                meta["issues"].append({"kind": "over_limit", "pages": list(range(len(pages) + 1, in_file + 1)), "checked_with_vision": False,
                                       "reason": f"Aasaan reads the first {len(pages)} pages of a file. The rest were not read."})
                meta["status"] = "partly_readable" if meta["status"] == "ready" else meta["status"]
            self.store.set_source_meta(sid, meta)
            if not any(p.strip() for p in pages):
                warnings.append(f"{src['title']} has no readable text. Attach clearer photos of the pages instead.")
                return None
        elif kind == "image":
            ext = Path(src["raw_ref"]).suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")
            images = [(mime, Path(src["raw_ref"]).read_bytes())]
        res = intake.run(self.router, sid, pages, images, lang, nb["goal"])
        return res.value

    def _recover_pages(self, data: bytes, pages: list[str], empties: list[dict], language: str, title: str) -> tuple[list[str], dict]:
        """Pages with no text layer: scans (images) and pages that might hold outlined text go to a vision model; drawing-only pages are probed
        once per kind and, if the probe finds no text, reported as such without spending a call on each. Returns (pages, source meta)."""
        pages = list(pages)
        total, recovered = len(pages), []
        todo, probe_groups = [], {}
        for e in empties:
            if e["images"] > 0 or (e["fonts"] == 0 and e["content_bytes"] >= intake.VECTOR_TEXT_BYTES):
                todo.append(e)
            elif e["content_bytes"] > 0:
                probe_groups.setdefault(e["content_bytes"] // 1000, []).append(e)
        no_text: dict[str, list[int]] = {"blank": [e["page"] for e in empties if e["content_bytes"] == 0 and e["images"] == 0], "drawing_only": [], "failed": []}
        vision_checked = False
        budget = intake.OCR_MAX_PAGES
        for group in list(probe_groups.values())[:2]:          # one vision probe per kind of drawing-only page
            png = intake.render_page_png(data, group[0]["page"])
            if png is None or budget <= 0:
                no_text["drawing_only"] += [e["page"] for e in group]
                continue
            budget -= 1
            got = intake.ocr_page(self.router, png, language, f"{title[:30]} p{group[0]['page']}")
            vision_checked = True
            if got["failed"]:
                no_text["failed"] += [e["page"] for e in group]
            elif got["empty"]:
                no_text["drawing_only"] += [e["page"] for e in group]
            else:                                              # the probe found text: this kind of page is worth reading in full
                pages[group[0]["page"] - 1] = got["text"]
                recovered.append(group[0]["page"])
                todo += group[1:]
        for g in list(probe_groups.values())[2:]:              # more than two kinds of drawing-only page: not worth more probes
            no_text["drawing_only"] += [e["page"] for e in g]
        for e in todo:
            if budget <= 0:
                no_text["failed"].append(e["page"])
                continue
            png = intake.render_page_png(data, e["page"])
            if png is None:
                no_text["failed"].append(e["page"])
                continue
            budget -= 1
            got = intake.ocr_page(self.router, png, language, f"{title[:30]} p{e['page']}")
            vision_checked = True
            if got["failed"]:
                no_text["failed"].append(e["page"])
            elif got["empty"]:
                no_text["drawing_only" if e["images"] == 0 else "blank"].append(e["page"])
            else:
                pages[e["page"] - 1] = got["text"]
                recovered.append(e["page"])
        reasons = {"blank": "Nothing is printed on these pages.",
                   "drawing_only": "These pages have only lines or shapes, for example an empty table grid, and no text" +
                                   (" (checked with a vision model)." if vision_checked else "."),
                   "failed": "These pages may hold text, but it could not be read automatically. A clearer scan or photo would help."}
        issues = [{"kind": k, "pages": sorted(set(v)), "reason": reasons[k], "checked_with_vision": vision_checked and k != "failed"} for k, v in no_text.items() if v]
        read = sum(1 for p in pages if p.strip())
        status = "unreadable" if read == 0 else ("partly_readable" if issues else "ready")
        if recovered:
            self.router.note("intake", "ok", f"read {len(recovered)} page(s) of '{title[:40]}' with a vision model", f"pages {recovered[:12]}: no text layer")
        for i in issues:
            log.info("[aasaan] %s: %d page(s) with no text (%s): %s", title, len(i["pages"]), i["kind"], i["reason"])
        return pages, {"status": status, "pages_total": total, "pages_read": read, "recovered_with_vision": sorted(recovered), "issues": issues}

    # ============================================================== chapters
    def build_chapter(self, cid: int) -> None:
        with self._scope(self._chapter(cid)["notebook_id"]):
            return self._build_chapter(cid)

    def _build_chapter(self, cid: int) -> None:
        """Curator -> Teacher <-> Critic -> Quizzer, then write all blocks and flip to ready in one transaction."""
        ch = self._chapter(cid)
        nb = self._nb(ch["notebook_id"])
        prof = self.profile(nb["learner_id"], nb)
        self.store.set_chapter_status(cid, "drafting")
        all_chunks = self._all_chunks(nb["id"])
        own = self._chapter_chunks(ch, all_chunks)

        res = curator.curate(self.router, self.store, ch["title"], ch["objectives"], search_topic(nb), nb["language"])
        page_chunks = [{"chunk_id": p["chunk_id"], "page": 1, "text": p["text"], "source_id": "web"} for p in res.pages]
        chunks = own + page_chunks
        link_refs = [{"ref": f"url:{c.url}", "title": c.title, "source": c.source, "kind": c.kind} for c in res.links + res.videos]
        allowed_extra = {l["ref"] for l in link_refs}

        issues, previous, content, verified, revisions, open_points = None, None, None, False, 0, []
        quiz_future = None
        with ThreadPoolExecutor(max_workers=2) as pool:
            for attempt in range(MAX_REVISIONS + 1):
                content = teacher.run(self.router, ch, chunks, link_refs, prof, issues, previous, self.store).value
                self.store.set_chapter_status(cid, "checking")
                # the quiz is written while the Critic checks this draft (saves a full round trip on the happy path)
                quiz_future = pool.submit(quizzer.run, self.router, ch, content, chunks, prof, self.store)
                review: CriticOut = critic.run(self.router, content, chunks, prof, allowed_extra).value
                if review.verdict == "approve":
                    verified = True
                    break
                open_points = [i.model_dump() for i in review.issues]
                if attempt == MAX_REVISIONS:
                    self.router.note("critic", "fallback", f"Chapter '{ch['title'][:40]}' shipped unverified",
                                     f"Critic still had {len(review.issues)} issue(s) after {MAX_REVISIONS} revisions")
                    break
                revisions += 1
                issues = [i.model_dump() for i in review.issues]
                previous = content.model_dump()
                self.store.set_chapter_status(cid, "drafting")
            assert content is not None and quiz_future is not None
            # visuals are made from the final notes while the quiz finishes; they can only add to a chapter, never block it
            illo_pool = ThreadPoolExecutor(max_workers=1)
            illo_future = illo_pool.submit(contextvars.copy_context().run, illustrator.run, self.router, self.store, ch["title"],
                                           content.notes_md, prof.language)
            quiz: QuizOut = quiz_future.result().value  # the quiz of the FINAL draft (earlier drafts' quizzes are discarded)
            quiz, quiz_notes = gates.quiz_sanity(self.router, quiz, content.notes_md)
            for n in quiz_notes:
                self.router.note("quizzer", "fallback", f"quiz_sanity on '{ch['title'][:40]}'", n)
        visuals = []
        try:
            visuals = illo_future.result(timeout=float(os.environ.get("AASAAN_ILLUSTRATOR_TIMEOUT_S", "90"))).blocks
        except FutureTimeout:
            self.router.note("illustrator", "failed", "Illustrator timed out; chapter ships without visuals", "AASAAN_ILLUSTRATOR_TIMEOUT_S exceeded")
        except Exception as e:  # noqa: BLE001
            self.router.note("illustrator", "failed", "Illustrator failed; chapter ships without visuals", f"{type(e).__name__}: {e}")
        illo_pool.shutdown(wait=False, cancel_futures=True)

        blocks = [
            {"type": "notes", "content": {"markdown": content.notes_md}, "meta": {"refs": content.source_refs}},
            {"type": "slides", "content": {"cards": [s.model_dump(exclude={"image_query"}) for s in content.slides]}},
        ]
        blocks[1:1] = visuals   # pictures sit right after the notes they illustrate
        for v in res.videos:
            blocks.append({"type": "video", "verified": True, "content": {"video_id": v.extra["video_id"], "title": v.title, "channel": v.source,
                                                                        "url": v.url, "start": v.extra.get("start", 0)}})
        for im in res.images:
            blocks.append({"type": "image", "verified": True, "content": {
                "url": im.url, "title": im.title, "attribution": {"author": im.extra.get("author", ""), "license": im.extra.get("license", ""),
                                                                  "source": im.source, "page_url": im.extra.get("page_url", "")}}})
        for ln in res.links:
            blocks.append({"type": "link", "verified": True, "content": {"url": ln.url, "title": ln.title, "source": ln.source, "reason": ln.reason}})
        blocks.append({"type": "quiz", "content": {"questions": [q.model_dump() for q in quiz.questions]}})
        blocks.append({"type": "summary", "content": {"text": content.summary, "key_points": content.key_points}})
        meta = {**ch["meta"], "unverified": not verified, "grounded": bool(chunks), "revisions": revisions,
                "page_chunks": page_chunks, "curator_notes": res.notes,
                "review_issues": [] if verified else self._claim_rows(open_points, chunks, allowed_extra)}
        self.store.replace_blocks(cid, blocks, "ready", meta)
        if verified:
            self.store.cache_set(teacher.content_cache_key(ch, prof.language), content.model_dump())
            self.store.cache_set(quizzer.quiz_cache_key(ch, prof.language), quiz.model_dump())
        self._refresh_status(nb["id"])

    def _refresh_status(self, nid: str) -> None:
        """generating once chapter 1 is ready; complete when every chapter is ready. Outlining, first-chapter and
        failed states are owned by the job code."""
        nb = self.store.get_notebook(nid)
        chs = self.store.chapters(nid)
        if not nb or not chs:
            return
        if all(c["status"] == "ready" for c in chs):
            status = "complete"
        elif chs[0]["status"] == "ready":
            status = "generating"
        else:
            return
        if status != nb["status"] or nb.get("error"):
            self.store.update_notebook(nid, status=status, error="")

    def prioritize(self, cid: int) -> dict:
        """The student opened a chapter that is not ready: jump the queue."""
        ch = self._chapter(cid)
        if ch["status"] in ("queued", "drafting", "checking"):
            self.store.update_chapter(cid, priority=50)
        return {"id": cid, "priority": 50, "status": ch["status"]}

    def retry_chapter(self, cid: int) -> None:
        ch = self._chapter(cid)
        nb = self._nb(ch["notebook_id"])
        if ch["status"] != "failed":
            return
        if nb["status"] == "failed" and ch["position"] == 1:
            return self.retry_notebook(nb["id"])
        self.store.set_chapter_status(cid, "queued")
        self.store.update_chapter(cid, priority=60)
        self.store.add_job(nb["id"], cid, "chapter")

    def recheck_chapter(self, cid: int) -> dict:
        """"Re-check": run the Critic again over the chapter exactly as stored (including the student's own edits)."""
        ch = self._chapter(cid)
        if ch["status"] != "ready":
            raise UserError("This chapter is still being prepared.")
        with self._scope(ch["notebook_id"]):
            nb = self._nb(ch["notebook_id"])
            prof = self.profile(nb["learner_id"], nb)
            blocks = self.store.blocks(cid)
            notes = next((b for b in blocks if b["type"] == "notes"), None)
            if not notes:
                raise UserError("This chapter has no notes to check.")
            slides = next((b["content"].get("cards", []) for b in blocks if b["type"] == "slides"), [])
            summ = next((b["content"] for b in blocks if b["type"] == "summary"), {})
            data = {"notes_md": notes["content"].get("markdown", ""), "slides": [{"title": s.get("title", ""), "points": s.get("points", [])} for s in slides],
                    "key_points": summ.get("key_points", []), "summary": summ.get("text", ""), "source_refs": (notes["meta"] or {}).get("refs", [])}
            try:
                content = ChapterContent.model_validate(data)
            except ValueError:   # the student's edits can break the strict shape (very short notes); the Critic only reads the text
                content = ChapterContent.model_construct(**data)
            chunks = self._chapter_chunks(ch, self._all_chunks(nb["id"])) + (ch["meta"].get("page_chunks") or [])
            allowed = {f"url:{b['content'].get('url')}" for b in blocks if b["type"] in ("link", "video") and b["content"].get("url")}
            review = critic.run(self.router, content, chunks, prof, allowed).value
            verified = review.verdict == "approve"
            rows = [] if verified else self._claim_rows([i.model_dump() for i in review.issues], chunks, allowed)
            self.store.update_chapter(cid, meta={**ch["meta"], "unverified": not verified, "review_issues": rows})
            return {"verified": verified, "review_issues": rows}

    def retry_notebook(self, nid: str) -> None:
        """"Try again" on the Loading screen: re-run the outline and/or the first chapter."""
        nb = self._nb(nid)
        if nb["status"] != "failed":
            return
        self.store.update_notebook(nid, status="outlining", error="")
        chs = self.store.chapters(nid)
        if chs and chs[0]["status"] == "failed":
            self.store.set_chapter_status(chs[0]["id"], "queued")
        self.store.add_job(nid, None, "notebook")

    # =============================================================== views
    GENERATION = {"queued": "queued", "drafting": "writing", "checking": "checking", "ready": "ready", "failed": "failed"}

    @classmethod
    def _generation(cls, ch: dict) -> str:
        """Where the chapter is in being built: queued, writing, checking, ready, failed. Shown only while it is not ready."""
        return cls.GENERATION.get(ch["status"], "queued")

    @staticmethod
    def _learning(ch: dict) -> str:
        """Where the STUDENT is with it: not_started, in_progress, done, needs_review. Independent of generation."""
        if ch["needs_review"]:
            return "needs_review"
        if ch["completed"]:
            return "done"
        return "in_progress" if ch["started"] else "not_started"

    @classmethod
    def _state(cls, ch: dict) -> str:
        """One value for colouring the graph: failed, generating (not ready yet), else the learning state."""
        if ch["status"] == "failed":
            return "failed"
        if ch["status"] != "ready":
            return "generating"
        return cls._learning(ch)

    @staticmethod
    def _claim_rows(points: list[dict], chunks: list[dict], allowed_extra: set) -> list[dict]:
        """What the Critic could not verify, with whether the source it points at exists (shown under "See which claims")."""
        known = {c["chunk_id"] for c in chunks} | set(allowed_extra)
        rows = []
        for p in points[:12]:
            ref = p.get("source_ref")
            rows.append({"claim": str(p.get("problem", ""))[:400], "where": str(p.get("step", "")), "source_ref": ref,
                         "source_status": "no source cited" if not ref else ("source found" if ref in known else "cited source not in this notebook")})
        return rows

    def notebook_view(self, nid: str) -> dict:
        nb = self._nb(nid)
        chs = self.store.chapters(nid)
        weights = [READINESS_WEIGHT.get(c["status"], 0) for c in chs]
        readiness = round(100 * sum(weights) / len(chs)) if chs else 0
        done = sum(1 for c in chs if c["completed"])
        view_chs = [{"id": c["id"], "position": c["position"], "title": c["title"], "summary": c["summary"],
                     "objectives": c["objectives"], "prereqs": c["prereqs"], "status": c["status"], "state": self._state(c),
                     "learning": self._learning(c), "generation": self._generation(c),
                     "kind": c["kind"], "needs_review": c["needs_review"], "completed": c["completed"], "error": c["error"]}
                    for c in chs]
        edges = [[p, c["id"]] for c in chs for p in c["prereqs"]]
        first_ready = bool(chs) and chs[0]["status"] == "ready"
        return {"id": nid, "title": nb["title"], "goal": nb["goal"], "description": nb["description"], "level": nb["level"],
                "language": nb["language"], "status": nb["status"], "error": nb["error"],
                "chapters": view_chs, "edges": edges, "readiness": readiness, "open_ready": first_ready,
                "your_progress": {"done": done, "total": len(chs), "pct": round(100 * done / len(chs)) if chs else 0},
                "preparing": {"ready": sum(1 for c in chs if c["status"] == "ready"), "total": len(chs),
                              "pct": round(100 * sum(1 for c in chs if c["status"] == "ready") / len(chs)) if chs else 0,
                              "complete": bool(chs) and all(c["status"] == "ready" for c in chs)},
                "progress": {"done": done, "total": len(chs)}, "warnings": [w for w in (self.store.cache_get(f"nbwarn:{nid}") or []) if not re.match(r"page \d+ unreadable or empty$", w)],
                "source_issues": [{"source_id": s["id"], "title": s["title"], "status": s["meta"].get("status", "ready"), "pages_total": s["meta"].get("pages_total"),
                                   "pages_read": s["meta"].get("pages_read"), "issues": s["meta"].get("issues", [])}
                                  for s in self.store.sources(nid) if (s["meta"] or {}).get("issues")],
                "sources": [{"id": src["id"], "kind": src["kind"], "title": src["title"], "status": (src["meta"] or {}).get("status", "ready"),
                             "url": src["raw_ref"] if src["kind"] == "url" else "", "chunks": len(src["chunks"])}
                            for src in self.store.sources(nid)],
                "mode": self.router.mode, "generating": self.store.active_jobs(nid) > 0,
                "next_chapter": next((c["id"] for c in chs if c["status"] == "ready" and not c["completed"]), None)}

    def list_notebooks(self, learner_id: str) -> list[dict]:
        out = []
        for nb in self.store.list_notebooks(learner_id):
            v = self.notebook_view(nb["id"])
            out.append({"id": nb["id"], "title": v["title"], "status": v["status"], "readiness": v["readiness"],
                        "progress": v["progress"], "last_opened": nb["last_opened"], "chapters": len(v["chapters"]),
                        "open_ready": v["open_ready"], "error": v["error"]})
        return out

    def chapter_view(self, cid: int, open_it: bool = True) -> dict:
        ch = self._chapter(cid)
        nb = self._nb(ch["notebook_id"])
        blocks = self.store.blocks(cid)
        if open_it:
            self.store.update_notebook(nb["id"], last_opened=time.time())
        out_blocks = []
        for b in blocks:
            content = b["content"]
            if b["type"] == "quiz":  # answer keys and hints stay on the server until the student answers
                content = {"questions": [{"id": q["id"], "text": q["text"], "options": q["options"], "concept": q["concept"]}
                                         for q in content["questions"]]}
            if b["type"] == "visual" and (content.get("payload") or {}).get("spec"):   # draw with the current renderer (theme tokens)
                try:
                    from .visuals import render, specs
                    content = {**content, "payload": {**content["payload"], "svg": render.render(specs.parse_spec(content["payload"]["spec"])).svg}}
                except Exception as e:  # noqa: BLE001  keep the stored drawing if it can't be redrawn
                    log.warning("[aasaan] could not redraw visual block %s: %s", b["id"], e)
            out_blocks.append({"id": b["id"], "type": b["type"], "content": content, "verified": b["verified"], "meta": b["meta"]})
        meta = ch["meta"]
        return {"id": cid, "notebook_id": nb["id"], "title": ch["title"], "summary": ch["summary"], "objectives": ch["objectives"],
                "status": ch["status"], "state": self._state(ch), "learning": self._learning(ch), "generation": self._generation(ch),
                "kind": ch["kind"], "error": ch["error"], "review_issues": meta.get("review_issues", []),
                "blocks": out_blocks, "unverified": bool(meta.get("unverified")), "grounded": meta.get("grounded", True),
                "curator_notes": meta.get("curator_notes", []), "completed": ch["completed"]}

    def open_chapter(self, cid: int) -> None:
        ch = self._chapter(cid)
        if ch["status"] == "ready" and not ch["started"]:
            self.store.update_chapter(cid, started=True)

    def edit_block(self, bid: int, content: dict) -> None:
        b = self.store.get_block(bid)
        if not b:
            raise UserError("unknown block")
        if b["type"] != "notes":
            raise UserError("only notes can be edited")
        md = str(content.get("markdown", ""))
        if len(md) > 20000:
            raise UserError("Notes are too long.")
        self.store.update_block_content(bid, {"markdown": md})

    # ============================================================ session/memory
    def open_session(self, learner_id: str) -> str:
        return self.store.open_session(learner_id)

    def companion_open(self, learner_id: str, nid: str) -> dict:
        nb = self._nb(nid)
        prof = self.profile(learner_id, nb)
        chs = self.store.chapters(nid)
        nxt = next((str(c["id"]) for c in chs if c["status"] == "ready" and not c["completed"]), None)
        # The greeting is fixed text chosen by code from stored memory. A model never writes it, so it can never claim a history the student
        # does not have ("welcome back" on a first session). Opening the app opens a session each time, so "returning" means real history.
        first = not self.store.has_history(learner_id)
        return {"message": self.greeting(first, nb["title"], prof.mistake_labels, self.store.last_summary(learner_id)),
                "suggested_next": nxt, "mistake_labels": prof.mistake_labels, "is_first_session": first}

    @staticmethod
    def greeting(first_session: bool, title: str, labels: list[str], last_summary: Optional[str]) -> str:
        if first_session:
            return f"Your notebook \"{title}\" is taking shape. Open the first chapter whenever you like, and ask me anything in the chat."
        if labels:
            return f"Welcome back. Last time you mixed up '{labels[0]}', so it is worth a quick look. Let's continue with \"{title}\"."
        return f"Welcome back. Let's continue with \"{title}\"."

    def close_session(self, learner_id: str, session_id: str, nid: Optional[str]) -> dict:
        nb = self._nb(nid) if nid else None
        prof = self.profile(learner_id, nb)
        atts = self.store.session_attempts(session_id)
        wrong = sorted({a["misconception"] for a in atts if a["result"] != "correct" and a["misconception"]})
        chs = self.store.chapters(nid) if nid else []
        nxt = next((str(c["id"]) for c in chs if c["status"] == "ready" and not c["completed"]), None)
        last = str(atts[-1]["chapter_id"]) if atts and str(atts[-1]["chapter_id"]) in {str(c["id"]) for c in chs} else None
        res = companion.run(self.router, "close", prof, [str(c["id"]) for c in chs], nxt, session_wrong=wrong, last_chapter=last,
                            notebook_title=nb["title"] if nb else "")
        out = res.value
        existing = set(self.store.mistake_labels(learner_id))
        for label in out.memory_updates.mistake_labels_add:
            if label not in existing:  # Diagnoser already counted this session's mistakes; avoid double counting
                self.store.add_mistake(learner_id, label)
        if out.memory_updates.last_chapter:
            self.store.set_memory(learner_id, "last", "chapter", out.memory_updates.last_chapter)
        if out.memory_updates.notes:
            self.store.set_memory(learner_id, "note", "companion", out.memory_updates.notes)
        self.store.close_session(session_id, out.message)
        return {"message": out.message, "suggested_next": out.suggested_next, "revisit": wrong}

    # ================================================================== quiz
    def answer(self, learner_id: str, session_id: str, cid: int, question_id: str, student_answer: str, attempt: int = 1) -> dict:
        ch = self._chapter(cid)
        qblock = next((b for b in self.store.blocks(cid) if b["type"] == "quiz"), None)
        if not qblock:
            raise UserError("this chapter has no quiz yet")
        raw = next((q for q in qblock["content"]["questions"] if q["id"] == question_id), None)
        if not raw:
            raise UserError("unknown question")
        q = Question.model_validate(raw)
        known = self.store.mistake_labels(learner_id)
        d = diagnoser.run(self.router, q, student_answer, known).value
        if d.result != "correct" and d.confidence < diagnoser.LOW_CONFIDENCE and attempt == 1:
            # unsure: ask for another try instead of marking the student wrong
            return {"question_id": question_id, "follow_up": True, "result": None, "hint": d.hint or q.hint, "misconception": None}
        self.store.add_attempt(learner_id=learner_id, notebook_id=ch["notebook_id"], chapter_id=cid, session_id=session_id,
                               question_id=question_id, concept=q.concept, answer=student_answer, result=d.result,
                               misconception=d.misconception, confidence=d.confidence)
        self.store.update_chapter(cid, started=True)
        if d.result != "correct" and d.misconception:
            self.store.add_mistake(learner_id, d.misconception)
        out = {"question_id": question_id, "follow_up": False, "result": d.result, "misconception": d.misconception,
               "hint": d.hint or q.hint, "confidence": d.confidence}
        if d.result == "correct" or attempt >= 2:  # hint first; reveal the answer only after a second try
            out["correct_answer"] = resolve_key(q)
        return out

    def finish_chapter(self, learner_id: str, session_id: str, cid: int) -> dict:
        """Mark the chapter done, then let the Rewriter adjust the course (at most 2 review items per session)."""
        ch = self._chapter(cid)
        nid = ch["notebook_id"]
        rows = self.store.attempts(cid, session_id)
        first: dict[str, dict] = {}
        missed: dict[str, dict] = {}
        for r in rows:  # a question is a gap if ANY attempt was wrong: right-after-a-hint still shows a misunderstanding
            first.setdefault(r["question_id"], r)
            if r["result"] != "correct":
                missed.setdefault(r["question_id"], r)
        total = len(first)
        correct = sum(1 for r in first.values() if r["result"] == "correct")
        agg: dict[str, dict] = {}
        for r in missed.values():
            label = r["misconception"] or r["concept"]
            e = agg.setdefault(label, {"concept": r["concept"], "misconception": label, "count": 0})
            e["count"] += 1
        wrong = sorted(agg.values(), key=lambda e: -e["count"])

        self.store.update_chapter(cid, completed=True, started=True, needs_review=bool(wrong))
        if not wrong and ch["kind"] == "review" and ch["prereqs"]:  # a passed review clears its parent's flag
            self.store.update_chapter(ch["prereqs"][0], needs_review=False)

        added, reason = [], "Course unchanged."
        if wrong and ch["kind"] != "review":
            made = [c for c in self.store.chapters(nid) if c["kind"] == "review" and c["meta"].get("session") == session_id]
            left = MAX_INSERTS_PER_SESSION - len(made)
            if left > 0:
                chs = [{"id": c["id"], "title": c["title"], "position": c["position"], "kind": c["kind"]} for c in self.store.chapters(nid)]
                rw = rewriter.run(self.router, chs, cid, wrong, self.store.mistake_counts(learner_id), left).value
                reason = rw.reason or reason
                for ins in rw.patch.insert[:left]:
                    after = self._chapter(int(ins.after))
                    self.store.shift_positions(nid, after["position"], 1)
                    new_id = self.store.add_chapter(nid, after["position"] + 1, ins.title, ins.summary, ins.objectives, [after["id"]],
                                                    after["source_refs"], kind="review", priority=80,
                                                    meta={"session": session_id, "misconception": wrong[0]["misconception"]})
                    self.store.add_job(nid, new_id, "chapter")
                    added.append({"id": new_id, "title": ins.title})
        self._refresh_status(nid)
        return {"score": {"correct": correct, "total": total}, "added": added, "reason": reason,
                "revisit": [w["misconception"] for w in wrong], "notebook": self.notebook_view(nid)}

    # ================================================================ doubts
    def doubt(self, learner_id: str, cid: int, text: Optional[str], audio: Optional[bytes]) -> dict:
        ch = self._chapter(cid)
        nb = self._nb(ch["notebook_id"])
        prof = self.profile(learner_id, nb)
        if not text and audio:
            text = self.voice.stt(audio, prof.language)
            if not text:
                return {"needs_text": True, "message": "Voice is unavailable right now. Please type your doubt."}
        if not text or not text.strip():
            raise UserError("empty question")
        pool = self._chapter_chunks(ch, self._all_chunks(nb["id"])) + (ch["meta"].get("page_chunks") or [])
        words = set(re.findall(r"\w+", text.lower()))
        pool.sort(key=lambda c: -len(words & set(re.findall(r"\w+", c["text"].lower()))))
        notes = next((b["content"].get("markdown", "") for b in self.store.blocks(cid) if b["type"] == "notes"), "")
        res = doubt.run(self.router, text, pool[:6], prof, notes)
        audio_out = self.voice.tts(res.value.answer, prof.language)
        return {"question": text, "answer": res.value.answer, "source_refs": res.value.source_refs,
                "audio_b64": base64.b64encode(audio_out).decode() if audio_out else None}

    # ================================================================= voice
    def transcribe(self, audio: bytes, language: str) -> dict:
        text = self.voice.stt(audio, language)
        if not text:
            return {"text": None, "message": "Voice is unavailable right now. Please type instead."}
        return {"text": text}

    def speak(self, text: str, language: str) -> dict:
        audio = self.voice.tts(text, language)
        return {"audio_b64": base64.b64encode(audio).decode() if audio else None}
