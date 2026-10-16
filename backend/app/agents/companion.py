"""08 Companion: remembers the student, opens and closes sessions (agents/08-companion.md)."""
from __future__ import annotations

from typing import Optional

from ..router import ModelRouter, RouterResult
from ..schemas import CompanionOut, LearnerProfile
from .common import build_prompt, lang_name


def run(
    router: ModelRouter,
    mode: str,                       # "open" | "close"
    profile: LearnerProfile,
    valid_nodes: list[str],
    suggested_next: Optional[str],
    last_summary: Optional[str] = None,
    session_wrong: Optional[list[str]] = None,
    last_node: Optional[str] = None,
    returning: bool = False,
) -> RouterResult:
    labels = profile.mistake_labels
    ctx = {"mode": mode, "labels": labels, "last_summary": last_summary, "suggested_next": suggested_next,
           "session_wrong": session_wrong or [], "last_node": last_node, "returning": returning}

    def validate(raw: dict) -> CompanionOut:
        out = CompanionOut.model_validate(raw)  # memory_updates is extra=forbid: only allowed fields pass
        if out.suggested_next and out.suggested_next not in valid_nodes:
            raise ValueError(f"suggested_next {out.suggested_next} is not a node of the path")
        if out.memory_updates.last_node and out.memory_updates.last_node not in valid_nodes:
            raise ValueError("last_node is not a node of the path")
        return out

    def template_message(c: dict) -> CompanionOut:
        """Last resort: fixed template built only from stored memory."""
        if mode == "open":
            if returning and labels:
                msg = f"Welcome back. Last time you mixed up '{labels[0]}'. Let's continue."
            elif returning:
                msg = "Welcome back. Let's continue."
            else:
                msg = "Hi! Let's start learning from your material."
        else:
            wrong = session_wrong or []
            msg = "Good work today." + (f" Revisit '{wrong[0]}' next time." if wrong else "")
        return CompanionOut(message=msg, suggested_next=suggested_next,
                            memory_updates={"last_node": last_node} if last_node else {})

    example = {"message": "Short greeting or recap", "suggested_next": suggested_next,
               "memory_updates": {"mistake_labels_add": [], "last_node": last_node, "notes": "optional"}}
    job = ("You are Companion. Be warm, brief and specific. ")
    job += ("At session START mention one thing the student did well and one thing to revisit, using only the stored "
            "mistake labels. If memory is empty, just welcome them." if mode == "open" else
            "At session END summarise in two sentences.")
    job += (" Never invent history that is not in memory. Do not collect personal details. "
            f"Write the message in {lang_name(profile.language)}.")
    prompt = build_prompt(job, example, {"mode": mode, "mistake_labels": labels, "last_session_summary": last_summary,
                                         "mistakes_this_session": session_wrong or [],
                                         "valid_node_ids": valid_nodes, "suggested_next": suggested_next,
                                         "returning_student": returning})
    return router.call("companion", prompt, ctx, validate, last_resort=template_message,
                       summary=f"Companion {mode}")
