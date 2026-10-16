"""04 Critic: check a lesson against the source; approve, revise or reject (agents/04-critic.md)."""
from __future__ import annotations

import re

from ..router import ModelRouter, RouterResult
from ..schemas import MAX_LESSON_CHARS, CriticOut, LearnerProfile, LessonOut
from .common import build_prompt, chunk_ids, lang_name

# Phrases that signal the lesson hands over the answer to its own check question.
LEAK_PATTERNS = [r"\bthe answer is\b", r"\banswer:\s", r"\bcorrect answer\b"]


def rule_checks(lesson: LessonOut, chunks: list[dict]) -> CriticOut:
    """Last resort (no model): refs exist, every step set has refs, length cap, no answer leakage."""
    known = chunk_ids(chunks)
    issues = []
    if not lesson.lesson.source_refs:
        issues.append({"step": 0, "problem": "Lesson has no source refs"})
    for r in lesson.lesson.source_refs:
        if r.chunk_id not in known:
            issues.append({"step": 0, "problem": f"Source ref {r.chunk_id} does not exist", "source_ref": r.model_dump()})
    if sum(len(s.text) for s in lesson.lesson.steps) > MAX_LESSON_CHARS:
        issues.append({"step": 0, "problem": "Lesson over the length cap"})
    for i, s in enumerate(lesson.lesson.steps, start=1):
        if any(re.search(p, s.text, re.I) for p in LEAK_PATTERNS):
            issues.append({"step": i, "problem": "Possible answer given too early"})
    return CriticOut(verdict="revise" if issues else "approve", issues=issues, confidence=0.4)


def run(router: ModelRouter, lesson: LessonOut, chunks: list[dict], profile: LearnerProfile) -> RouterResult:
    ctx = {"lesson": lesson.model_dump(), "chunks": chunks}

    def validate(raw: dict) -> CriticOut:
        return CriticOut.model_validate(raw)

    example = {"verdict": "approve | revise | reject",
               "issues": [{"step": 2, "problem": "Claim not found in source",
                           "source_ref": {"source_id": "s1", "page": 12, "chunk_id": "s1-p12-c3"}}],
               "confidence": 0.85}
    prompt = build_prompt(
        "You are Critic, a strict fact-checker. For each step, find the supporting text in the cited chunks. Flag "
        "any claim without support, any error, any answer given too early, and any wording too hard for the "
        "grade. Be specific so the Teacher can fix it. Do not rewrite the lesson. 'reject' needs at least one issue.",
        example, {"lesson": ctx["lesson"], "source_chunks": chunks, "grade": profile.grade,
                  "student_language": lang_name(profile.language)})
    return router.call("critic", prompt, ctx, validate, last_resort=lambda c: rule_checks(lesson, chunks),
                       summary=f"Critic review of {lesson.node_id}")
