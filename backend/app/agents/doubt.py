"""Doubt answerer: answers a student's question from the chapter's source chunks and notes."""
from __future__ import annotations

import re

from ..router import ModelRouter, RouterResult
from ..schemas import DoubtOut, LearnerProfile
from .common import build_prompt, chunk_ids, lang_name, split_sentences, tokens


def run(router: ModelRouter, question: str, chunks: list[dict], profile: LearnerProfile, notes: str = "", style: str = "",
        history: list[dict] | None = None, external: bool = False) -> RouterResult:
    """`history`: the last few turns [{"role", "text"}] so "that", "it" and "again" mean something. `external`: the chunks are web pages (ids w1, w2...)."""
    known = chunk_ids(chunks)
    ctx = {"question": question, "chunks": chunks, "language": profile.language, "notes": notes, "history": history or []}

    def validate(raw: dict) -> DoubtOut:
        out = DoubtOut.model_validate(raw)
        out.source_refs = [r for r in out.source_refs if r in known]
        return out

    def template_answer(c: dict) -> DoubtOut:
        """Last resort: the source (or notes) sentence that shares the most words with the question."""
        words = tokens(question)
        best, best_ref, score = None, None, 0
        pool = [(ch["chunk_id"], ch["text"]) for ch in chunks] + [(None, notes)]
        for ref, text in pool:
            for s in split_sentences(text or ""):
                sc = len(words & tokens(s))
                if sc > score:
                    best, best_ref, score = s, ref, sc
        if not best:
            return DoubtOut(answer="I could not find that in this chapter. Try asking in another way.")
        return DoubtOut(answer=best, source_refs=[best_ref] if best_ref else [], covered=score > 0)

    example = {"answer": "Short answer with a citation [1]", "source_refs": ["<chunk id>"], "covered": True}
    prompt = build_prompt(
        "You are a tutor answering a student's doubt. " + ("The student wants it EXPLAINED AGAIN: use plain words, one concrete example or analogy taken from the material, "
                                                           "at most 5 short sentences. " if style == "explain" else "") +
        ("The source chunks below are WEB PAGES the student has not seen: use only them. " if external else
         "Answer in 2-3 short sentences using ONLY the source chunks and chapter notes; if they do not cover it, say so and set covered to false. ") +
        "Cite the sources you used inline as [1], [2] (the number is the position of the chunk in source_chunks, starting at 1). "
        "Use the conversation so far to understand words like 'that', 'it', 'again'. Math and symbols are fine: write them plainly (x^2, sum, <=). "
        "Give a hint before a full answer when it looks like a homework question. "
        f"Write the answer in {lang_name(profile.language)}.",
        example, {"question": question, "conversation": (history or [])[-8:],
                  "source_chunks": [{"n": i, "id": c["chunk_id"], "text": c["text"]} for i, c in enumerate(chunks, start=1)],
                  "chapter_notes": notes[:2500]})
    return router.call("doubt", prompt, ctx, validate, last_resort=template_answer, summary="Answer doubt",
                       language=profile.language)
