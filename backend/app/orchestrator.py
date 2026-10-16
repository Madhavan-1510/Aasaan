"""Orchestrator: the learning loop (architecture/02-learning-loop).

Intake -> Architect -> Teacher <-> Critic -> Quizzer -> student -> Diagnoser -> Rewriter -> Teacher,
with Companion opening/closing sessions and Memory (SQLite) shared by all.

State lives in SQLite, so every call is stateless over (learner, session, path, node).
Plain Python state machine (PRD allows this as the LangGraph fallback); each loop is explicit below.
"""
from __future__ import annotations

import base64
import re
import uuid
from typing import Optional

from .agents import architect, companion, critic, diagnoser, doubt, intake, quizzer, rewriter, teacher
from .db import Store
from .router import AllFailed, ModelRouter
from .schemas import CriticOut, IntakeOut, LearnerProfile, LessonOut, PathOut, Question, QuizOut, resolve_key
from .voice import Voice

MAX_REVISIONS = 2  # Teacher <-> Critic loops before shipping the best draft as unverified


class UserError(Exception):
    """Bad request or missing data; surfaced as HTTP 400/404."""


class Engine:
    def __init__(self, store: Store, router: ModelRouter):
        self.store = store
        self.router = router
        self.voice = Voice(router)

    # ------------------------------------------------------------------ helpers
    def profile(self, learner_id: str) -> LearnerProfile:
        row = self.store.get_learner(learner_id)
        if not row:
            raise UserError("unknown learner")
        return LearnerProfile(learner_id=learner_id, grade=row["grade"] or 9, language=row["language"] or "en",
                              goal=row["goal"], mistake_labels=row["mistake_labels"])

    def _path(self, path_id: str) -> tuple[PathOut, str]:
        rec = self.store.get_path(path_id)
        if not rec:
            raise UserError("unknown path")
        return PathOut.model_validate(rec["path"]), rec["source_id"]

    def _node_chunks(self, source_id: str, node) -> list[dict]:
        src = self.store.get_source(source_id)
        if not src:
            raise UserError("source missing")
        ids = {r.chunk_id for r in node.source_refs}
        chunks = [c for c in src["chunks"] if c["chunk_id"] in ids]
        if not chunks:
            pages = {r.page for r in node.source_refs}
            chunks = [c for c in src["chunks"] if c["page"] in pages]
        return chunks

    def _next_node(self, path: PathOut, path_id: str) -> Optional[str]:
        done = {k for k, v in self.store.node_statuses(path_id).items() if v == "done"}
        return next((n.id for n in path.nodes if n.id not in done), None)

    def _course_meta(self, src: IntakeOut, goal: Optional[str]) -> dict:
        """Course title and blurb for the course page, derived from the material (no extra model call)."""
        first = src.topics[0]
        title = (goal or "").strip() or first.title
        blurb = " ".join(t.summary.strip() for t in src.topics[:3] if t.summary.strip())
        return {"title": title[:80], "description": blurb[:240], "topics": [t.title for t in src.topics]}

    def _path_view(self, path: PathOut, path_id: str) -> dict:
        st = self.store.node_statuses(path_id)
        course = self.store.cache_get(f"course:{path_id}") or {"title": path.nodes[0].title[:80], "description": ""}
        done = sum(1 for n in path.nodes if st.get(n.id) == "done")
        return {
            "course": course, "progress": {"done": done, "total": len(path.nodes)},
            "path_id": path_id, "start_node": path.start_node, "current": self._next_node(path, path_id),
            "nodes": [{**n.model_dump(), "status": st.get(n.id, "pending")} for n in path.nodes],
            "edges": [list(e) for e in path.edges],
        }

    # ----------------------------------------------------------------- learners
    def create_learner(self, grade: int, language: str, goal: Optional[str]) -> str:
        return self.store.create_learner(grade, language, goal)

    # -------------------------------------------------- session open (Companion)
    def open_session(self, learner_id: str) -> dict:
        """Start a session for a returning student: Companion greets using stored mistakes."""
        prof = self.profile(learner_id)
        row = self.store.get_learner(learner_id)
        returning = self.store.session_count(learner_id) > 0
        sid = self.store.open_session(learner_id)
        path_id = row.get("last_path")
        path, nxt = None, None
        if path_id and self.store.get_path(path_id):
            path, _ = self._path(path_id)
            nxt = self._next_node(path, path_id)
        res = companion.run(self.router, "open", prof, [n.id for n in path.nodes] if path else [], nxt,
                            last_summary=self.store.last_summary(learner_id), returning=returning)
        return {"session_id": sid, "message": res.value.message, "suggested_next": res.value.suggested_next,
                "path_id": path_id, "returning": returning, "mistake_labels": prof.mistake_labels,
                "path": self._path_view(path, path_id) if path else None}

    # --------------------------------------------------- start: Intake+Architect
    def start(self, learner_id: str, session_id: Optional[str], text: Optional[str], pdf: Optional[bytes],
              images: Optional[list[tuple[str, bytes]]], language: Optional[str], goal: Optional[str]) -> dict:
        row = self.store.get_learner(learner_id)
        if not row:
            raise UserError("unknown learner")
        self.store.update_learner(learner_id, language=language, goal=goal)
        prof = self.profile(learner_id)

        pages: list[str] = []
        if text and text.strip():
            pages = intake.pages_from_text(text)
        elif pdf:
            try:
                pages = intake.pages_from_pdf(pdf)
            except Exception as e:
                raise UserError(f"could not read the PDF: {e}")
        if not pages and not images:
            raise UserError("Please upload a clearer photo, a PDF, or paste your notes.")

        sid = session_id or self.store.open_session(learner_id)
        source_id = "s" + uuid.uuid4().hex[:6]
        try:
            ires = intake.run(self.router, source_id, pages, images, prof.language, prof.goal)
        except AllFailed:
            raise UserError("I could not read that material. Please retake the photo or paste the text.")
        src: IntakeOut = ires.value
        self.store.save_source(source_id, learner_id, src.model_dump())

        path_id = "p" + uuid.uuid4().hex[:6]
        try:
            ares = architect.run(self.router, src, prof, path_id)
        except AllFailed:
            raise UserError("Could not build a learning path from this material.")
        path: PathOut = ares.value
        self.store.save_path(path_id, learner_id, source_id, path.model_dump())
        self.store.cache_set(f"course:{path_id}", self._course_meta(src, goal))
        self.store.update_learner(learner_id, last_path=path_id)

        greeting = companion.run(self.router, "open", prof, [n.id for n in path.nodes], path.start_node,
                                 last_summary=self.store.last_summary(learner_id),
                                 returning=self.store.session_count(learner_id) > 1)
        return {"session_id": sid, "source_id": source_id, "warnings": src.warnings,
                "greeting": greeting.value.message, "path": self._path_view(path, path_id)}

    def list_courses(self, learner_id: str) -> list[dict]:
        """The student's own courses (no community): title, progress, next step."""
        self.profile(learner_id)
        out = []
        for rec in self.store.learner_paths(learner_id):
            path = PathOut.model_validate(rec["path"])
            v = self._path_view(path, rec["path_id"])
            out.append({"path_id": rec["path_id"], "title": v["course"]["title"], "description": v["course"]["description"],
                        "progress": v["progress"], "current": v["current"], "updated": rec["updated"]})
        return out

    def get_path(self, path_id: str) -> dict:
        path, _ = self._path(path_id)
        return self._path_view(path, path_id)

    # ------------------------------------------------- lesson: Teacher <-> Critic
    def lesson(self, learner_id: str, path_id: str, node_id: str) -> dict:
        prof = self.profile(learner_id)
        path, source_id = self._path(path_id)
        node = next((n for n in path.nodes if n.id == node_id), None)
        if not node:
            raise UserError("unknown node")
        cached = self.store.get_lesson(path_id, node_id)
        if cached:
            return {"node_id": node_id, "lesson": cached["lesson"]["lesson"], "verified": cached["verified"],
                    "revisions": 0, "cached": True}
        chunks = self._node_chunks(source_id, node)

        issues: Optional[list[dict]] = None
        previous: Optional[dict] = None
        draft: Optional[LessonOut] = None
        verified, revisions = False, 0
        for attempt in range(MAX_REVISIONS + 1):
            tres = teacher.run(self.router, node, chunks, prof, issues, previous, self.store, source_id)
            draft = tres.value
            review: CriticOut = critic.run(self.router, draft, chunks, prof).value
            if review.verdict == "approve":
                verified = True
                break
            if attempt == MAX_REVISIONS:
                break  # ship best version, logged as unverified
            revisions += 1
            issues = [i.model_dump() for i in review.issues]
            previous = draft.model_dump()
        assert draft is not None
        self.store.save_lesson(path_id, node_id, draft.model_dump(), verified)
        if verified:
            self.store.cache_set(teacher.lesson_cache_key(chunks, prof.language), draft.model_dump())
        self.store.set_node_status(path_id, node_id, "in_progress")
        return {"node_id": node_id, "lesson": draft.lesson.model_dump(), "verified": verified,
                "revisions": revisions, "cached": False}

    # ------------------------------------------------------------------- quizzer
    def quiz(self, learner_id: str, path_id: str, node_id: str) -> dict:
        prof = self.profile(learner_id)
        path, source_id = self._path(path_id)
        node = next((n for n in path.nodes if n.id == node_id), None)
        if not node:
            raise UserError("unknown node")
        quiz = self.store.get_quiz(path_id, node_id)
        if not quiz:
            les = self.store.get_lesson(path_id, node_id)
            if not les:
                self.lesson(learner_id, path_id, node_id)
                les = self.store.get_lesson(path_id, node_id)
            lesson = LessonOut.model_validate(les["lesson"])
            chunks = self._node_chunks(source_id, node)
            qout: QuizOut = quizzer.run(self.router, lesson, chunks, prof, self.store, source_id).value
            quiz = qout.model_dump()
            self.store.save_quiz(path_id, node_id, quiz)
            self.store.cache_set(quizzer.quiz_cache_key(chunks, prof.language), quiz)
        # never send answer keys or hints to the client up front (hint comes after a wrong answer)
        return {"node_id": node_id, "questions": [
            {"id": q["id"], "text": q["text"], "options": q["options"], "concept": q["concept"]}
            for q in quiz["questions"]]}

    # ------------------------------------------------------------------ diagnoser
    def answer(self, learner_id: str, session_id: str, path_id: str, node_id: str, question_id: str,
               student_answer: str, attempt: int = 1) -> dict:
        quiz = self.store.get_quiz(path_id, node_id)
        if not quiz:
            raise UserError("no quiz for this node")
        raw = next((q for q in quiz["questions"] if q["id"] == question_id), None)
        if not raw:
            raise UserError("unknown question")
        q = Question.model_validate(raw)
        known = self.store.mistake_labels(learner_id)
        d = diagnoser.run(self.router, q, student_answer, known).value
        # Low confidence -> ask for another try instead of a verdict (first attempt only).
        if d.result != "correct" and d.confidence < diagnoser.LOW_CONFIDENCE and attempt == 1:
            return {"question_id": question_id, "follow_up": True, "result": None,
                    "hint": d.hint or q.hint, "misconception": None}
        self.store.save_answer(learner_id=learner_id, session_id=session_id, path_id=path_id, node_id=node_id,
                               question_id=question_id, concept=q.concept, answer=student_answer,
                               result=d.result, misconception=d.misconception)
        if d.result != "correct" and d.misconception:
            self.store.add_mistake(learner_id, d.misconception)
        out = {"question_id": question_id, "follow_up": False, "result": d.result,
               "misconception": d.misconception, "hint": d.hint or q.hint, "confidence": d.confidence}
        if d.result == "correct" or attempt >= 2:  # hint first; reveal the answer only after a second try
            out["correct_answer"] = resolve_key(q)
        return out

    # ------------------------------------------------------------------ rewriter
    def finish_node(self, learner_id: str, session_id: str, path_id: str, node_id: str) -> dict:
        path, _ = self._path(path_id)
        rows = self.store.session_answers(session_id, node_id)
        # A question counts as a gap if ANY attempt was not correct: succeeding only after a hint
        # still shows the student misunderstood it. The score uses the first attempt.
        first: dict[str, dict] = {}
        missed: dict[str, dict] = {}
        for r in rows:
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

        self.store.set_node_status(path_id, node_id, "done")
        locked = [k for k, v in self.store.node_statuses(path_id).items() if v == "done"]
        node = next(n for n in path.nodes if n.id == node_id)

        patch_view: dict = {"insert": [], "reorder": [], "skip": []}
        reason = "Path unchanged."
        if wrong and not node.title.startswith("Review:"):  # never review a review (no endless loops)
            started = self.store._one("SELECT started FROM sessions WHERE id=?", (session_id,))["started"]
            inserted = sum(len(h["patch"].get("insert", [])) for h in self.store.path_history(path_id)
                           if h["ts"] >= started)
            if inserted < 2:  # at most 2 inserted nodes per session
                res = rewriter.run(self.router, path, node_id, wrong, self.store.mistake_counts(learner_id), locked)
                rw = res.value
                if rw.patch.insert or rw.patch.reorder or rw.patch.skip:
                    path = rewriter.apply_patch(path, rw.patch, locked)
                    self.store.save_path(path_id, learner_id, self.store.get_path(path_id)["source_id"], path.model_dump())
                    self.store.add_path_history(path_id, rw.patch.model_dump(), rw.reason)
                    patch_view = rw.patch.model_dump()
                reason = rw.reason
        nxt = self._next_node(path, path_id)
        self.store.update_learner(learner_id, last_node=node_id)
        return {"score": {"correct": correct, "total": total}, "patch": patch_view, "reason": reason,
                "next_node": nxt, "path": self._path_view(path, path_id)}

    # ------------------------------------------------------------ close (Companion)
    def close_session(self, learner_id: str, session_id: str, path_id: Optional[str]) -> dict:
        prof = self.profile(learner_id)
        path = self._path(path_id)[0] if path_id and self.store.get_path(path_id) else None
        wrong_labels = sorted({r["misconception"] for r in self.store.session_answers(session_id)
                               if r["result"] != "correct" and r["misconception"]})
        correct = sum(1 for r in self.store.session_answers(session_id) if r["result"] == "correct")
        nxt = self._next_node(path, path_id) if path else None
        res = companion.run(self.router, "close", prof, [n.id for n in path.nodes] if path else [], nxt,
                            session_wrong=wrong_labels, last_node=self.store.get_learner(learner_id).get("last_node"))
        out = res.value
        existing = set(self.store.mistake_labels(learner_id))
        for label in out.memory_updates.mistake_labels_add:
            if label not in existing:  # Diagnoser already counted this session's mistakes; avoid double counting
                self.store.add_mistake(learner_id, label)
        self.store.update_learner(learner_id, last_node=out.memory_updates.last_node, notes=out.memory_updates.notes)
        self.store.close_session(session_id, out.message)
        return {"message": out.message, "suggested_next": out.suggested_next, "revisit": wrong_labels,
                "answered_correct": correct}

    # --------------------------------------------------------------- doubts (voice)
    def doubt(self, learner_id: str, path_id: str, node_id: Optional[str], text: Optional[str],
              audio: Optional[bytes]) -> dict:
        prof = self.profile(learner_id)
        path, source_id = self._path(path_id)
        if not text and audio:
            text = self.voice.stt(audio, prof.language)
            if not text:
                return {"needs_text": True, "message": "Voice is unavailable right now. Please type your doubt."}
        if not text or not text.strip():
            raise UserError("empty question")
        src = self.store.get_source(source_id)
        words = set(re.findall(r"\w+", text.lower()))
        ranked = sorted(src["chunks"], key=lambda c: -len(words & set(re.findall(r"\w+", c["text"].lower()))))
        chunks = ranked[:6]
        res = doubt.run(self.router, text, chunks, prof)
        audio_out = self.voice.tts(res.value.answer, prof.language)
        return {"question": text, "answer": res.value.answer,
                "source_refs": [r.model_dump() for r in res.value.source_refs],
                "audio_b64": base64.b64encode(audio_out).decode() if audio_out else None}
