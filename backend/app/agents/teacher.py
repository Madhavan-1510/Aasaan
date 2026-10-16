"""03 Teacher: write the lesson for one node from the student's material (agents/03-teacher.md)."""
from __future__ import annotations

from typing import Optional

from ..db import Store
from ..router import ModelRouter, RouterResult
from ..schemas import LearnerProfile, LessonOut, PathNode
from .common import build_prompt, chunk_ids, fingerprint, lang_name, ref_of, split_sentences


def lesson_cache_key(chunks: list[dict], language: str) -> str:
    return f"lesson:{fingerprint(chunks)}:{language}"


def run(
    router: ModelRouter,
    node: PathNode,
    chunks: list[dict],
    profile: LearnerProfile,
    issues: Optional[list[dict]] = None,
    previous: Optional[dict] = None,
    store: Optional[Store] = None,
    source_id: str = "",
) -> RouterResult:
    known = chunk_ids(chunks)
    ctx = {"node": node.model_dump(), "chunks": chunks, "language": profile.language,
           "issues": issues or [], "previous": previous}

    def validate(raw: dict) -> LessonOut:
        raw = {**raw, "node_id": node.id}
        out = LessonOut.model_validate(raw)
        if out.language != profile.language:
            raise ValueError(f"language must be {profile.language}, got {out.language}")
        for r in out.lesson.source_refs:
            if r.chunk_id not in known:
                raise ValueError(f"lesson cites unknown chunk {r.chunk_id}")
        return out

    def cached_lesson(c: dict) -> LessonOut:
        """Last resort: a previously approved lesson for this topic, else a plain extract of the source."""
        if store:
            hit = store.cache_get(lesson_cache_key(chunks, profile.language))
            if hit:
                return LessonOut.model_validate({**hit, "node_id": node.id})
        sents = [s for ch in chunks for s in split_sentences(ch["text"])] or [node.title]
        return LessonOut.model_validate({
            "node_id": node.id, "language": profile.language,
            "lesson": {
                "title": node.title,
                "steps": [
                    {"type": "explain", "text": " ".join(sents[:2])},
                    {"type": "example", "text": " ".join(sents[2:4]) or " ".join(sents[:1])},
                    {"type": "check", "text": f"Think: what is the main idea of {node.title}?"},
                ],
                "key_points": sents[:3],
                "source_refs": [ref_of(ch) for ch in chunks[:2]],
            },
        })

    example = {
        "node_id": node.id, "language": profile.language,
        "lesson": {"title": node.title,
                   "steps": [{"type": "explain", "text": "..."}, {"type": "example", "text": "..."},
                             {"type": "check", "text": "Quick question to think about"}],
                   "key_points": ["..."], "source_refs": [{"source_id": "s1", "page": 1, "chunk_id": "s1-p1-c1"}]},
    }
    job = ("You are Teacher. Teach ONE idea in 3 to 5 short steps (types: explain, example, check). Use the "
           "student's own examples and wording where possible, plain language for the grade. Never state a fact "
           "not supported by the source chunks. End with a thinking prompt (a check step), not the answer. "
           f"Write all student-facing text in {lang_name(profile.language)}.")
    if issues:
        job += " This is a REVISION: fix only the issues the Critic listed and keep the rest."
    prompt = build_prompt(job, example, {"node": ctx["node"], "chunks": chunks, "grade": profile.grade,
                                         "critic_issues": issues or [], "previous_draft": previous})
    return router.call("teacher", prompt, ctx, validate, last_resort=cached_lesson,
                       summary=f"Lesson for {node.id}" + (" (revision)" if issues else ""))
