"""Architect: the course outline for a notebook (8 to 20 chapters, prerequisites, acyclic).

The outline drives both views: the roadmap (ordered list) and the graph (chapters as nodes, prerequisites as edges).
"""
from __future__ import annotations

from typing import Optional

from ..router import ModelRouter, RouterResult
from ..schemas import LearnerProfile, OutlineOut
from .common import build_prompt, chunk_ids, lang_name


def run(router: ModelRouter, goal: Optional[str], topics: list[dict], chunks: list[dict], profile: LearnerProfile,
        notebook_title: str = "", track: Optional[str] = None) -> RouterResult:
    known = chunk_ids(chunks)
    ctx = {"goal": goal, "notebook_title": notebook_title, "topics": topics, "chunks": chunks, "level": profile.level,
           "language": profile.language, "mistake_labels": profile.mistake_labels}

    def validate(raw: dict) -> OutlineOut:
        out = OutlineOut.model_validate(raw)
        for c in out.chapters:
            bad = [r for r in c.source_refs if r not in known]
            if bad:
                raise ValueError(f"chapter {c.key} cites unknown source chunks {bad[:3]}")
        if not goal and not topics:
            raise ValueError("nothing to plan from")
        return out

    def linear_outline(c: dict) -> OutlineOut:
        """Last resort: one chapter per topic, in order. Impossible without topics (no sources, no model)."""
        tps = c["topics"][:20]
        if not tps:
            raise ValueError("no sources and no working model: cannot plan a course")
        chapters = []
        for i, t in enumerate(tps):
            refs = [ch["chunk_id"] for ch in c["chunks"] if ch.get("source_id") == t.get("source_id") and
                    ch["page"] in t.get("page_refs", [])][:3]
            chapters.append({"key": f"c{i + 1}", "title": t["title"], "summary": t.get("summary", ""),
                             "objectives": [f"Understand {t['title']}"], "prereqs": [f"c{i}"] if i else [], "source_refs": refs})
        while len(chapters) < 3:  # tiny sources: add generic bookends so the outline stays valid
            n = len(chapters)
            chapters.append({"key": f"c{n + 1}", "title": "Review and practice" if n else "Overview", "summary": "",
                             "objectives": ["Consolidate the material"], "prereqs": [f"c{n}"] if n else [], "source_refs": []})
        return OutlineOut(title=(goal or tps[0]["title"])[:80], description=tps[0].get("summary", "")[:240],
                          level=profile.level if profile.level in ("beginner", "intermediate", "advanced") else "beginner",
                          chapters=chapters)

    example = {"title": "Data Structures and Algorithms", "description": "One or two sentences about the course",
               "level": "beginner",
               "chapters": [{"key": "c1", "title": "Why data structures matter", "summary": "Two sentences",
                             "objectives": ["Explain what a data structure is"], "prereqs": [],
                             "source_refs": ["<chunk_id from the student's material, if any>"]},
                            {"key": "c2", "title": "Arrays and lists", "summary": "...", "objectives": ["..."],
                             "prereqs": ["c1"], "source_refs": []}]}
    job = ("You are Architect. Design a complete Coursera-style course for the student's goal. "
           "Create 8 to 20 chapters for a broad goal (fewer, minimum 3, only if the supplied material is small). "
           "Order them so prerequisites come first. Each chapter has a short title, a 1-2 sentence summary, 1-4 learning "
           "objectives, and `prereqs` listing the keys of chapters that must come first (a DAG: no cycles, no self loops; "
           "let independent chapters share a prerequisite so the graph branches). If the student supplied material, build the "
           "course around it and cite chunk ids in `source_refs`. Pick a `level` (beginner/intermediate/advanced) that fits the "
           f"goal. Write titles, summaries and objectives in {lang_name(profile.language)}.")
    prompt = build_prompt(job, example, {
        "goal": goal, "student_level": profile.level, "language": lang_name(profile.language), "track": track,
        "mistakes_to_revisit": profile.mistake_labels[:5],
        "topics_from_material": [{"id": t["id"], "title": t["title"], "summary": t.get("summary", ""),
                                  "source_id": t.get("source_id")} for t in topics],
        "available_chunk_ids": sorted(known)[:80]})
    return router.call("architect", prompt, ctx, validate, last_resort=linear_outline, summary="Course outline",
                       language=profile.language)
