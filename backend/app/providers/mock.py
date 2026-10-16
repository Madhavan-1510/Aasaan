"""Offline provider: returns schema-valid JSON for every role, derived from the
context the agent passes in. Lets the whole engine run with no API keys (tests,
UI development, demo cache). Also supports scripted outputs and failure
injection so loops and fallbacks can be tested.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .base import Provider, ProviderError


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def ref_of(chunk: dict, source_id: str | None = None) -> dict:
    sid = source_id or chunk["chunk_id"].split("-")[0]
    return {"source_id": sid, "page": chunk["page"], "chunk_id": chunk["chunk_id"]}


class MockProvider(Provider):
    name = "mock"

    def __init__(self) -> None:
        self.scripts: dict[str, list[Any]] = {}  # role -> queue of canned outputs (dict, str or Exception)
        self.calls: list[tuple[str, str]] = []   # (role, model_id) for assertions

    def script(self, role: str, outputs: list[Any]) -> None:
        self.scripts[role] = list(outputs)

    def generate(self, model_id, prompt, *, role="", context=None, images=None, timeout_s=30, json_mode=True):
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

    # ---- generators -------------------------------------------------------
    def _intake(self, c: dict) -> dict:
        chunks = c.get("chunks", [])
        extra: dict = {}
        if not chunks:  # photo input: pretend the vision model transcribed one page
            chunks = [{"chunk_id": f"{c.get('source_id', 's1')}-p1-c1", "page": 1,
                       "text": "Sample transcription from a photo (mock mode)."}]
            extra["chunks"] = chunks
        by_page: dict[int, list[dict]] = {}
        for ch in chunks:
            by_page.setdefault(ch["page"], []).append(ch)
        topics = []
        for i, (page, cs) in enumerate(sorted(by_page.items())[:8], start=1):
            sents = _sentences(cs[0]["text"])
            first = sents[0] if sents else f"Page {page}"
            title = " ".join(first.split()[:6]).rstrip(".,;:")
            topics.append({"id": f"t{i}", "title": title, "summary": first[:160], "page_refs": [page]})
        return {"topics": topics, "warnings": [], **extra}

    def _architect(self, c: dict) -> dict:
        sid = c["source_id"]
        chunks = c["chunks"]
        topics = c["topics"]
        labels = c.get("mistake_labels", [])
        nodes: list[dict] = []
        n_topics = 7 if labels else 8
        for t in topics[:n_topics]:
            cs = [ch for ch in chunks if ch["page"] in t["page_refs"]][:2] or chunks[:1]
            nodes.append({
                "id": f"n{len(nodes) + 1}", "title": t["title"], "prereqs": [],
                "est_minutes": 6, "source_refs": [ref_of(ch, sid) for ch in cs],
            })
        if labels:  # review node near the start for a known mistake
            first = nodes[0]
            nodes.insert(0, {
                "id": "n0r", "title": f"Review: {labels[0]}", "prereqs": [], "est_minutes": 4,
                "source_refs": first["source_refs"][:1],
            })
        for i in range(1, len(nodes)):
            nodes[i]["prereqs"] = [nodes[i - 1]["id"]]
        edges = [[nodes[i]["id"], nodes[i + 1]["id"]] for i in range(len(nodes) - 1)]
        return {"path_id": c["path_id"], "start_node": nodes[0]["id"], "nodes": nodes, "edges": edges}

    def _teacher(self, c: dict) -> dict:
        node, chunks = c["node"], c["chunks"]
        sents = [s for ch in chunks for s in _sentences(ch["text"])] or [node["title"]]
        explain = " ".join(sents[:2])
        example = " ".join(sents[2:4]) or f"Look at how the book describes {node['title']}."
        return {
            "node_id": node["id"], "language": c.get("language", "en"),
            "lesson": {
                "title": node["title"],
                "steps": [
                    {"type": "explain", "text": explain},
                    {"type": "example", "text": example},
                    {"type": "check", "text": f"In your own words, what is the main idea of {node['title']}?"},
                ],
                "key_points": sents[:3],
                "source_refs": [ref_of(ch) for ch in chunks[:2]],
            },
        }

    def _critic(self, c: dict) -> dict:
        return {"verdict": "approve", "issues": [], "confidence": 0.9}

    def _quizzer(self, c: dict) -> dict:
        lesson, node_id = c["lesson"], c["node_id"]
        points = lesson.get("key_points") or [lesson["title"]]
        ref = (lesson.get("source_refs") or [None])[0]
        wrong_opts = ["The opposite of what the material says", "Something the material never mentions", "All of these are true"]
        labels = c.get("mistake_labels") or []
        qs = []
        for i in range(3):
            pt = points[i % len(points)]
            correct = pt if len(pt) < 140 else pt[:137] + "..."
            qs.append({
                "id": f"q{i + 1}", "type": "mcq",
                "text": (f"Which statement matches your material about {lesson['title']}?" if i == 0
                         else f"Question {i + 1}: which statement is supported by the text?"),
                "options": [correct] + [w if i == 0 else f"{w} ({i})" for w in wrong_opts],
                "answer_key": correct,
                "concept": lesson["title"] if (i < 2 or not labels) else labels[0],
                "hint": "Re-read the first step of the lesson and look for the key words.",
                "source_ref": ref,
            })
        return {"node_id": node_id, "questions": qs}

    def _diagnoser(self, c: dict) -> dict:
        q = c["question"]
        known = c.get("known_labels", [])
        label = next((k for k in known if q["concept"].lower() in k.lower()), None) or f"misunderstands {q['concept']}"
        return {"question_id": q["id"], "result": "wrong", "misconception": label, "confidence": 0.8,
                "hint": "Look again at the key words in the lesson, then try once more."}

    def _rewriter(self, c: dict) -> dict:
        wrong = c.get("wrong", [])
        cur = c["current_node"]
        node = next(n for n in c["path"]["nodes"] if n["id"] == cur)
        patch: dict = {"insert": [], "reorder": [], "skip": []}
        reason = "No gaps found; keep the path."
        if wrong:
            top = wrong[0]
            patch["insert"].append({"after": cur, "node": {
                "id": f"{cur}r", "title": f"Review: {top['misconception']}", "prereqs": [cur],
                "est_minutes": 5, "source_refs": node["source_refs"][:1]}})
            reason = f"Student struggled with {top['misconception']}; added a short review."
        return {"path_id": c["path"]["path_id"], "patch": patch, "reason": reason}

    def _companion(self, c: dict) -> dict:
        labels = c.get("labels", [])
        if c["mode"] == "open":
            if labels:
                msg = f"Welcome back! Last time you mixed up '{labels[0]}'. Let's review it, then move on."
            else:
                msg = "Hi! Let's learn from your material, one small step at a time."
            return {"message": msg, "suggested_next": c.get("suggested_next"), "memory_updates": {}}
        wrong = c.get("session_wrong", [])
        msg = "Good work today. " + (f"Revisit '{wrong[0]}' next time." if wrong else "You answered everything well.")
        return {"message": msg, "suggested_next": c.get("suggested_next"),
                "memory_updates": {"mistake_labels_add": [], "last_node": c.get("last_node")}}

    def _doubt(self, c: dict) -> dict:
        chunks = c.get("chunks", [])
        sents = [s for ch in chunks for s in _sentences(ch["text"])][:2]
        return {"answer": " ".join(sents) or "I could not find that in your material.",
                "source_refs": [ref_of(ch) for ch in chunks[:1]]}
