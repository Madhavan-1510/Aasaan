"""Doubt answering (voice Q&A, F9). Not one of the eight agents: a small extra role that reuses the
router so the student can ask a question about their material and get a grounded answer."""
from __future__ import annotations

import re

from ..router import ModelRouter, RouterResult
from ..schemas import DoubtOut, LearnerProfile
from .common import build_prompt, chunk_ids, lang_name, ref_of, split_sentences


def run(router: ModelRouter, question: str, chunks: list[dict], profile: LearnerProfile) -> RouterResult:
    known = chunk_ids(chunks)
    ctx = {"question": question, "chunks": chunks, "language": profile.language}

    def validate(raw: dict) -> DoubtOut:
        out = DoubtOut.model_validate(raw)
        if not out.answer.strip():
            raise ValueError("empty answer")
        for r in out.source_refs:
            if r.chunk_id not in known:
                raise ValueError("answer cites unknown chunk")
        return out

    def template_answer(c: dict) -> DoubtOut:
        """Last resort: the source sentence that shares the most words with the question."""
        words = set(re.findall(r"\w+", question.lower()))
        best, best_chunk, score = None, None, 0
        for ch in chunks:
            for s in split_sentences(ch["text"]):
                sc = len(words & set(re.findall(r"\w+", s.lower())))
                if sc > score:
                    best, best_chunk, score = s, ch, sc
        if not best:
            return DoubtOut(answer="I could not find that in your material. Try asking in another way.")
        return DoubtOut(answer=best, source_refs=[ref_of(best_chunk)])

    example = {"answer": "Short answer", "source_refs": [{"source_id": "s1", "page": 12, "chunk_id": "s1-p12-c3"}]}
    prompt = build_prompt(
        "You are Teacher answering a student's doubt. Answer in 2-3 short sentences using ONLY the source chunks; "
        "if the material does not cover it, say so. Give a hint before a full answer when it is a homework-style "
        f"question. Write the answer in {lang_name(profile.language)}.",
        example, {"question": question, "source_chunks": chunks, "grade": profile.grade})
    return router.call("doubt", prompt, ctx, validate, last_resort=template_answer, summary="Answer doubt")
