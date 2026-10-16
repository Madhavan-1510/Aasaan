"""Quizzer: 3 to 5 multiple-choice questions per chapter with answer keys, concept tags and hints."""
from __future__ import annotations

from typing import Optional

from ..db import Store
from ..router import ModelRouter, RouterResult
from ..schemas import ChapterContent, LearnerProfile, QuizOut
from .common import build_prompt, fingerprint, lang_name


def quiz_cache_key(chapter: dict, language: str) -> str:
    return f"quiz:{fingerprint([{'text': chapter['title'] + '|' + chapter.get('summary', '')}])}:{language}"


def run(router: ModelRouter, chapter: dict, content: ChapterContent, chunks: list[dict], profile: LearnerProfile,
        store: Optional[Store] = None) -> RouterResult:
    known = {c["chunk_id"] for c in chunks}
    ctx = {"chapter": {k: chapter.get(k) for k in ("id", "title", "summary", "objectives")}, "content": content.model_dump(),
           "chunks": chunks, "mistake_labels": profile.mistake_labels}

    def validate(raw: dict) -> QuizOut:
        out = QuizOut.model_validate(raw)
        for q in out.questions:
            if q.source_ref and q.source_ref not in known and not q.source_ref.startswith("url:"):
                q.source_ref = None  # tolerate a stray ref rather than reject a good question
        return out

    def cached_questions(c: dict) -> QuizOut:
        """Last resort: cached quiz for this chapter, else questions built in code from the key points."""
        if store:
            hit = store.cache_get(quiz_cache_key(chapter, profile.language))
            if hit:
                return QuizOut.model_validate(hit)
        points = [p for p in (content.key_points or [s.points[0] for s in content.slides]) if p] or [chapter["title"]]
        decoys = ["The opposite of what the notes say", "Something the notes never mention", "None of these"]
        qs = []
        for i in range(3):
            pt = points[i % len(points)]
            correct = pt if len(pt) < 140 else pt[:137] + "..."
            qs.append({"id": f"q{i + 1}", "type": "mcq", "text": f"Which statement matches the notes on '{chapter['title']}'?",
                       "options": [correct] + [d if i == 0 else f"{d} ({i})" for d in decoys], "answer_key": correct,
                       "concept": chapter["title"], "hint": "Re-read the notes and look for the key words.",
                       "source_ref": content.source_refs[0] if content.source_refs else None})
        return QuizOut.model_validate({"questions": qs})

    example = {"questions": [{"id": "q1", "type": "mcq", "text": "...", "options": ["opt 1", "opt 2", "opt 3", "opt 4"],
                              "answer_key": "opt 2", "concept": "short concept tag", "hint": "A nudge that does not give the answer",
                              "source_ref": "<chunk id or null>"}]}
    prompt = build_prompt(
        "You are Quizzer. Write 3 to 5 multiple-choice questions that test understanding of this chapter, not recall of wording. "
        "Mix easy and medium. If mistake_labels exist, aim one question at one of them. Each question has exactly one correct "
        "option (answer_key = the exact option text), a concept tag and a gentle hint that does not give the answer away. "
        f"Write question text in {lang_name(profile.language)}.",
        example, {"chapter": ctx["chapter"], "notes": content.notes_md[:3000], "key_points": content.key_points,
                  "source_chunks": [{"id": c["chunk_id"], "text": c["text"][:600]} for c in chunks[:6]],
                  "mistake_labels": profile.mistake_labels[:3]})
    return router.call("quizzer", prompt, ctx, validate, last_resort=cached_questions,
                       summary=f"Quiz for '{chapter['title'][:40]}'", language=profile.language)
