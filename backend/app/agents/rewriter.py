"""Rewriter: adjusts the course after mistakes by inserting a short review chapter (at most 2 per session)."""
from __future__ import annotations

from ..router import ModelRouter, RouterResult
from ..schemas import ChapterPatch, RewriterOut, ReviewInsert
from .common import build_prompt


def run(router: ModelRouter, chapters: list[dict], current_id: int, wrong: list[dict],
        mistake_counts: dict[str, int], inserts_left: int) -> RouterResult:
    """chapters: [{"id","title","position","kind"}]. wrong: [{"concept","misconception","count"}] most frequent first."""
    ids = {str(c["id"]) for c in chapters}
    ctx = {"chapters": chapters, "current": str(current_id), "wrong": wrong, "mistake_counts": mistake_counts}

    def validate(raw: dict) -> RewriterOut:
        out = RewriterOut.model_validate(raw)
        if len(out.patch.insert) > inserts_left:
            raise ValueError(f"only {inserts_left} review item(s) allowed this session")
        titles = set()
        for ins in out.patch.insert:
            if ins.after not in ids:
                raise ValueError(f"insert after unknown chapter {ins.after}")
            if not ins.title.strip() or ins.title in titles:
                raise ValueError("review item needs a unique title")
            titles.add(ins.title)
        return out

    def insert_review_step(c: dict) -> RewriterOut:
        """Last resort: one review item for the most frequent mistake of this session."""
        if not wrong or inserts_left < 1:
            return RewriterOut(patch=ChapterPatch(), reason="No review needed.")
        top = wrong[0]
        label = top["misconception"] or top["concept"]
        return RewriterOut(patch=ChapterPatch(insert=[ReviewInsert(
            after=str(current_id), title=f"Review: {label}"[:90], summary=f"A short review of {label}.",
            objectives=[f"Fix the misunderstanding: {label}"])]),
            reason=f"Added a review for '{label}', your most frequent mistake.")

    example = {"patch": {"insert": [{"after": str(current_id), "title": "Review: <the misconception>", "summary": "One sentence",
                                     "objectives": ["Fix the misunderstanding"]}]}, "reason": "One sentence"}
    prompt = build_prompt(
        "You are Rewriter. Propose the SMALLEST change that fixes the student's gap: insert at most "
        f"{inserts_left} short review chapter(s), each right after the chapter where the mistake happened (use that chapter's id in "
        "`after`). Do not review a review chapter. If nothing needs fixing, return an empty insert list. Explain the reason in one sentence.",
        example, {"chapters": chapters, "current_chapter_id": str(current_id), "mistakes_this_session": wrong,
                  "lifetime_mistake_counts": mistake_counts})
    return router.call("rewriter", prompt, ctx, validate, last_resort=insert_review_step, summary="Adjust course")
