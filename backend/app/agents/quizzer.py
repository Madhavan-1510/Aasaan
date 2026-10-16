"""05 Quizzer: 3-5 questions per node with answer keys and concept tags (agents/05-quizzer.md)."""
from __future__ import annotations

from typing import Optional

from ..db import Store
from ..router import ModelRouter, RouterResult
from ..schemas import LearnerProfile, LessonOut, QuizOut, SourceRef
from .common import build_prompt, chunk_ids, fingerprint, lang_name


def quiz_cache_key(chunks: list[dict], language: str) -> str:
    return f"quiz:{fingerprint(chunks)}:{language}"


def run(
    router: ModelRouter,
    lesson: LessonOut,
    chunks: list[dict],
    profile: LearnerProfile,
    store: Optional[Store] = None,
    source_id: str = "",
) -> RouterResult:
    known = chunk_ids(chunks)
    node_id = lesson.node_id
    ctx = {"node_id": node_id, "lesson": lesson.lesson.model_dump(), "chunks": chunks,
           "mistake_labels": profile.mistake_labels}

    def validate(raw: dict) -> QuizOut:
        out = QuizOut.model_validate({**raw, "node_id": node_id})
        for q in out.questions:
            if q.source_ref and q.source_ref.chunk_id not in known:
                raise ValueError(f"{q.id} cites unknown chunk")
        return out

    def cached_questions(c: dict) -> QuizOut:
        """Last resort: cached quiz for this topic, else questions built in code from the key points."""
        if store:
            hit = store.cache_get(quiz_cache_key(chunks, profile.language))
            if hit:
                return QuizOut.model_validate({**hit, "node_id": node_id})
        points = lesson.lesson.key_points or [lesson.lesson.title]
        ref = lesson.lesson.source_refs[0].model_dump()
        decoys = ["The opposite of what the material says", "Something the material never mentions", "None of these"]
        qs = []
        for i in range(3):
            pt = points[i % len(points)]
            correct = pt if len(pt) < 140 else pt[:137] + "..."
            qs.append({"id": f"q{i + 1}", "type": "mcq", "text": f"Which statement matches your material about {lesson.lesson.title}?",
                       "options": [correct] + [f"{d}" if i == 0 else f"{d} ({i})" for d in decoys],
                       "answer_key": correct, "concept": lesson.lesson.title,
                       "hint": "Re-read the lesson and look for the key words.", "source_ref": ref})
        return QuizOut.model_validate({"node_id": node_id, "questions": qs})

    example = {"node_id": node_id, "questions": [{
        "id": "q1", "type": "mcq", "text": "...", "options": ["opt 1", "opt 2", "opt 3", "opt 4"],
        "answer_key": "opt 2", "concept": "speed vs velocity", "hint": "Think about direction",
        "source_ref": {"source_id": "s1", "page": 12, "chunk_id": "s1-p12-c3"}}]}
    prompt = build_prompt(
        "You are Quizzer. Write 3 to 5 multiple-choice questions that test understanding, not recall of wording. "
        "Mix easy and medium. If mistake_labels exist, include one question aimed at one of them. Each question has "
        "exactly one correct option (answer_key = the exact option text), a concept tag and a gentle hint that does "
        f"not give the answer away. Write question text in {lang_name(profile.language)}.",
        example, {"lesson": ctx["lesson"], "source_chunks": chunks, "grade": profile.grade,
                  "mistake_labels": profile.mistake_labels})
    return router.call("quizzer", prompt, ctx, validate, last_resort=cached_questions,
                       summary=f"Quiz for {node_id}")
