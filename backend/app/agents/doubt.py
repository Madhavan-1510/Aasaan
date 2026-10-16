"""Doubt answerer: answers a student's question from the chapter's source chunks and notes."""
from __future__ import annotations

import re

from ..router import ModelRouter, RouterResult
from ..schemas import DoubtOut, LearnerProfile
from .common import build_prompt, chunk_ids, lang_name, split_sentences


def run(router: ModelRouter, question: str, chunks: list[dict], profile: LearnerProfile, notes: str = "") -> RouterResult:
    known = chunk_ids(chunks)
    ctx = {"question": question, "chunks": chunks, "language": profile.language, "notes": notes}

    def validate(raw: dict) -> DoubtOut:
        out = DoubtOut.model_validate(raw)
        out.source_refs = [r for r in out.source_refs if r in known]
        return out

    def template_answer(c: dict) -> DoubtOut:
        """Last resort: the source (or notes) sentence that shares the most words with the question."""
        words = set(re.findall(r"\w+", question.lower()))
        best, best_ref, score = None, None, 0
        pool = [(ch["chunk_id"], ch["text"]) for ch in chunks] + [(None, notes)]
        for ref, text in pool:
            for s in split_sentences(text or ""):
                sc = len(words & set(re.findall(r"\w+", s.lower())))
                if sc > score:
                    best, best_ref, score = s, ref, sc
        if not best:
            return DoubtOut(answer="I could not find that in this chapter. Try asking in another way.")
        return DoubtOut(answer=best, source_refs=[best_ref] if best_ref else [])

    example = {"answer": "Short answer", "source_refs": ["<chunk id>"]}
    prompt = build_prompt(
        "You are a tutor answering a student's doubt. Answer in 2-3 short sentences using ONLY the source chunks and chapter notes; "
        "if they do not cover it, say so. Give a hint before a full answer when it looks like a homework question. "
        f"Write the answer in {lang_name(profile.language)}.",
        example, {"question": question, "source_chunks": [{"id": c["chunk_id"], "text": c["text"]} for c in chunks],
                  "chapter_notes": notes[:2500]})
    return router.call("doubt", prompt, ctx, validate, last_resort=template_answer, summary="Answer doubt",
                       language=profile.language)
