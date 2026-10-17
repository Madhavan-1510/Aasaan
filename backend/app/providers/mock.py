"""Offline provider (DEMO MODE): returns schema-valid JSON for every role, derived from the context the agent passes in.
Lets the whole app run with no API keys (tests, UI work, demos). The router labels this mode loudly in the UI and log.
Supports scripted outputs and call tracking so loops and fallbacks can be tested.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import Provider, ProviderError


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def _short(s: str, n: int = 60) -> str:
    return " ".join(s.split()[:n]).rstrip(".,;:")


ACRONYMS = {"dsa": "data structures and algorithms", "oop": "object-oriented programming", "dbms": "database management systems",
            "os": "operating systems", "ml": "machine learning", "ai": "artificial intelligence", "cn": "computer networks"}


class MockProvider(Provider):
    name = "mock"

    def __init__(self) -> None:
        self.scripts: dict[str, list[Any]] = {}  # role -> queue of canned outputs (dict, str or Exception)
        self.calls: list[tuple[str, str]] = []   # (role, model_id) for assertions

    def script(self, role: str, outputs: list[Any]) -> None:
        self.scripts[role] = list(outputs)

    def generate(self, model_id, prompt, *, role="", context=None, images=None, timeout_s=30, json_mode=True, thinking_level=None):
        self.calls.append((role, model_id))
        queue = self.scripts.get(role)
        if queue:
            out = queue.pop(0)
            if isinstance(out, Exception):
                raise out
            return out if isinstance(out, str) else json.dumps(out)
        fn = getattr(self, f"_{role}", None)
        if fn is None:
            raise ProviderError(f"mock has no generator for role {role}")
        return json.dumps(fn(context or {}))

    # ---- Intake -----------------------------------------------------------
    def _intake(self, c: dict) -> dict:
        chunks = c.get("chunks", [])
        extra: dict = {}
        if not chunks:  # photo input: pretend the vision model transcribed one page
            chunks = [{"chunk_id": f"{c.get('source_id', 's1')}-p1-c1", "page": 1,
                       "text": "Sample transcription from a photo (demo mode)."}]
            extra["chunks"] = chunks
        by_page: dict[int, list[dict]] = {}
        for ch in chunks:
            by_page.setdefault(ch["page"], []).append(ch)
        topics = []
        for i, (page, cs) in enumerate(sorted(by_page.items())[:8], start=1):
            sents = _sentences(cs[0]["text"])
            first = sents[0] if sents else f"Page {page}"
            topics.append({"id": f"t{i}", "title": _short(first, 6), "summary": first[:160], "page_refs": [page]})
        return {"topics": topics, "warnings": [], **extra}

    # ---- Architect --------------------------------------------------------
    def _architect(self, c: dict) -> dict:
        goal = (c.get("goal") or c.get("notebook_title") or "").strip()
        topics = c.get("topics") or []
        chunks = c.get("chunks") or []
        if topics:
            tps = topics[:14]
            chapters = []
            for i, t in enumerate(tps):
                refs = [ch["chunk_id"] for ch in chunks if ch.get("source_id") == t.get("source_id")
                        and ch["page"] in t.get("page_refs", [])][:3]
                chapters.append({"key": f"c{i + 1}", "title": t["title"], "summary": t.get("summary", ""),
                                 "objectives": [f"Understand: {t['title']}"], "prereqs": [f"c{i}"] if i else [], "source_refs": refs})
            while len(chapters) < 3:
                n = len(chapters)
                chapters.append({"key": f"c{n + 1}", "title": "Practice and review", "summary": "Consolidate what you read.",
                                 "objectives": ["Review the material"], "prereqs": [f"c{n}"], "source_refs": []})
            title = (goal or tps[0]["title"])[:80]
            return {"title": title, "description": tps[0].get("summary", "")[:240], "level": "beginner", "chapters": chapters}
        g = re.sub(r"^(please\s+)?(i\s+(want|would like|need)\s+to\s+|teach me( how)?( to)?\s+|help me\s+)?(learn|study|understand|master)\s+(about\s+)?", "", goal or "", flags=re.I).strip(" .!?") or "your topic"
        g = ACRONYMS.get(g.lower(), g)  # a real model expands acronyms; the demo outline does too
        names = [f"Introduction to {g}", f"Core concepts of {g}", f"Key terms and notation in {g}", "Worked examples",
                 "Common mistakes and how to avoid them", "Intermediate techniques", "Putting it all together",
                 "Practice and next steps"]
        prereqs = [[], ["c1"], ["c1"], ["c2", "c3"], ["c4"], ["c4"], ["c5", "c6"], ["c7"]]
        chapters = [{"key": f"c{i + 1}", "title": n, "summary": f"{n}: what it is and how to use it (demo outline).",
                     "objectives": [f"Explain the main ideas of: {n}", "Apply them to a simple example"],
                     "prereqs": prereqs[i], "source_refs": []} for i, n in enumerate(names)]
        return {"title": g[:1].upper() + g[1:], "description": f"A step-by-step course on {g} (demo mode: generic outline).",
                "level": "beginner", "chapters": chapters}

    # ---- Curator ----------------------------------------------------------
    def _curator(self, c: dict) -> dict:
        return {"relevant": [x["id"] for x in c.get("candidates", [])]}

    # ---- Teacher ----------------------------------------------------------
    def _teacher(self, c: dict) -> dict:
        ch, chunks = c["chapter"], c.get("chunks", [])
        allowed = c.get("allowed_refs", [])
        sents = [s for k in chunks if not k["chunk_id"].startswith("w") for s in _sentences(k["text"])]  # own material only
        title, objs = ch["title"], ch.get("objectives") or []
        if sents:
            body = " ".join(sents[:5])
            more = " ".join(sents[5:9]) or f"Look at how the material describes {title}."
            points = sents[:8]
        else:
            body = (ch.get("summary") or f"This chapter introduces {title}.") + " (Demo mode: this text is a placeholder, not generated by a model.)"
            more = "Objectives: " + "; ".join(objs or [title]) + "."
            points = (objs or [title]) + [ch.get("summary") or title]
        notes = (f"## {title}\n\n{body}\n\n### Worked idea\n\n{more}\n\n### Key points\n\n"
                 + "\n".join(f"- {p}" for p in points[:4]))
        slides = []
        for i in range(0, max(6, len(points)), 2):
            pts = [p[:140] for p in points[i:i + 2]] or [title]
            if len(pts) < 2:
                pts.append(ch.get("summary") or title)
            slides.append({"title": title if i == 0 else f"{title} ({i // 2 + 1})", "points": pts})
            if len(slides) == 4:
                break
        return {"language": c.get("language", "en"), "notes_md": notes, "slides": slides, "key_points": points[:4],
                "summary": (sents[0] if sents else ch.get("summary") or title)[:280], "source_refs": allowed[:2]}

    # ---- Critic -----------------------------------------------------------
    def _critic(self, c: dict) -> dict:
        return {"verdict": "approve", "issues": [], "confidence": 0.9}

    # ---- Quizzer ----------------------------------------------------------
    def _quizzer(self, c: dict) -> dict:
        ch, content = c["chapter"], c["content"]
        points = content.get("key_points") or [ch["title"]]
        ref = (content.get("source_refs") or [None])[0]
        wrong = ["The opposite of what the notes say", "Something the notes never mention", "All of these are true"]
        labels = c.get("mistake_labels") or []
        qs = []
        for i in range(3):
            pt = points[i % len(points)]
            correct = pt if len(pt) < 140 else pt[:137] + "..."
            qs.append({"id": f"q{i + 1}", "type": "mcq",
                       "text": (f"Which statement matches the notes on {ch['title']}?" if i == 0 else f"Question {i + 1}: which statement is supported?"),
                       "options": [correct] + [w if i == 0 else f"{w} ({i})" for w in wrong], "answer_key": correct,
                       "concept": ch["title"] if (i < 2 or not labels) else labels[0],
                       "hint": "Re-read the first part of the notes and look for the key words.", "source_ref": ref})
        return {"questions": qs}

    # ---- Diagnoser --------------------------------------------------------
    def _diagnoser(self, c: dict) -> dict:
        q = c["question"]
        known = c.get("known_labels", [])
        label = next((k for k in known if q["concept"].lower() in k.lower()), None) or f"misunderstands {_short(q['concept'], 8)}"
        return {"question_id": q["id"], "result": "wrong", "misconception": label, "confidence": 0.8,
                "hint": "Look again at the key words in the notes, then try once more."}

    # ---- Rewriter ---------------------------------------------------------
    def _rewriter(self, c: dict) -> dict:
        wrong = c.get("wrong", [])
        if not wrong:
            return {"patch": {"insert": []}, "reason": "No gaps found; keep the course."}
        top = wrong[0]
        label = top["misconception"] or top["concept"]
        return {"patch": {"insert": [{"after": c["current"], "title": f"Review: {label}"[:90], "summary": f"A short review of {label}.",
                                      "objectives": [f"Fix the misunderstanding: {label}"]}]},
                "reason": f"Student struggled with {label}; added a short review chapter."}

    # ---- Companion --------------------------------------------------------
    def _companion(self, c: dict) -> dict:
        labels = c.get("labels", [])
        if c["mode"] == "open":
            msg = (f"Welcome back! Last time you mixed up '{labels[0]}'. Let's review it, then move on." if labels
                   else "Hi! Your notebook is ready. Start with the first chapter.")
            return {"message": msg, "suggested_next": c.get("suggested_next"), "memory_updates": {}}
        wrong = c.get("session_wrong", [])
        msg = "Good work today. " + (f"Revisit '{wrong[0]}' next time." if wrong else "You answered everything well.")
        return {"message": msg, "suggested_next": c.get("suggested_next"),
                "memory_updates": {"mistake_labels_add": [], "last_chapter": c.get("last_chapter")}}

    # ---- Doubt ------------------------------------------------------------
    def _doubt(self, c: dict) -> dict:
        chunks = c.get("chunks", [])
        sents = [s for k in chunks for s in _sentences(k["text"])][:2] or _sentences(c.get("notes", ""))[:2]
        return {"answer": " ".join(sents) or "I could not find that in this chapter.",
                "source_refs": [k["chunk_id"] for k in chunks[:1]]}
