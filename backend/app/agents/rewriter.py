"""07 Rewriter: patch the learning path from this session's mistakes (agents/07-rewriter.md)."""
from __future__ import annotations

from typing import Iterable

from ..router import ModelRouter, RouterResult
from ..schemas import (MAX_INSERTS_PER_PATCH, MAX_PATH_NODES_PATCHED, Patch, PathOut, RewriterOut, check_path)
from .common import build_prompt


def apply_patch(path: PathOut, patch: Patch, locked: Iterable[str] = ()) -> PathOut:
    """Apply insert / reorder / skip in code and re-validate. Raises ValueError on a bad patch.
    `locked` = node ids already completed; they cannot be skipped or reordered."""
    locked = set(locked)
    if len(patch.insert) > MAX_INSERTS_PER_PATCH:
        raise ValueError(f"at most {MAX_INSERTS_PER_PATCH} inserted nodes per session")
    nodes = [n.model_copy(deep=True) for n in path.nodes]
    edges = [tuple(e) for e in path.edges]
    ids = {n.id for n in nodes}

    # skip
    for s in patch.skip:
        if s not in ids:
            raise ValueError(f"skip: unknown node {s}")
        if s in locked:
            raise ValueError(f"skip: {s} is already completed")
    skipped = set(patch.skip)
    for n in nodes:
        if n.id not in skipped and skipped & set(n.prereqs):
            raise ValueError("skipped node is a prerequisite of a remaining node")
    nodes = [n for n in nodes if n.id not in skipped]
    edges = [e for e in edges if e[0] not in skipped and e[1] not in skipped]

    # insert
    for op in patch.insert:
        ids_now = {n.id for n in nodes}
        if op.node.id in ids_now or op.node.id in ids:
            raise ValueError(f"insert: id {op.node.id} already exists")
        if op.after not in ids_now:
            raise ValueError(f"insert: unknown anchor {op.after}")
        idx = next(i for i, n in enumerate(nodes) if n.id == op.after)
        nodes.insert(idx + 1, op.node)
        succ = [b for a, b in edges if a == op.after]
        edges = [e for e in edges if e[0] != op.after] + [(op.after, op.node.id)] + [(op.node.id, b) for b in succ]

    # reorder: listed nodes take their existing slots in the new order
    if patch.reorder:
        if len(set(patch.reorder)) != len(patch.reorder):
            raise ValueError("reorder: duplicate ids")
        by_id = {n.id: n for n in nodes}
        for r in patch.reorder:
            if r not in by_id:
                raise ValueError(f"reorder: unknown node {r}")
            if r in locked:
                raise ValueError(f"reorder: {r} is already completed")
        slots = [i for i, n in enumerate(nodes) if n.id in set(patch.reorder)]
        for slot, nid in zip(slots, patch.reorder):
            nodes[slot] = by_id[nid]

    start = path.start_node if any(n.id == path.start_node for n in nodes) else nodes[0].id
    new = PathOut(path_id=path.path_id, start_node=start, nodes=nodes, edges=edges)
    return check_path(new, max_nodes=MAX_PATH_NODES_PATCHED)


def run(
    router: ModelRouter,
    path: PathOut,
    current_node: str,
    wrong: list[dict],          # [{"concept","misconception","count"}] most frequent first
    mistake_counts: dict[str, int],
    locked: Iterable[str] = (),
) -> RouterResult:
    locked = list(locked)
    ctx = {"path": path.model_dump(), "current_node": current_node, "wrong": wrong,
           "mistake_counts": mistake_counts}

    def validate(raw: dict) -> RewriterOut:
        out = RewriterOut.model_validate({**raw, "path_id": path.path_id})
        apply_patch(path, out.patch, locked)  # raises if the patched path would be invalid
        return out

    def insert_review_step(c: dict) -> RewriterOut:
        """Last resort: one review node for the most frequent mistake of this session."""
        if not wrong:
            return RewriterOut(path_id=path.path_id, patch=Patch(), reason="No mistakes; path unchanged.")
        node = next(n for n in path.nodes if n.id == current_node)
        new_id, k = f"{current_node}r", 1
        taken = {n.id for n in path.nodes}
        while new_id in taken:
            k += 1
            new_id = f"{current_node}r{k}"
        top = wrong[0]
        patch = Patch.model_validate({"insert": [{"after": current_node, "node": {
            "id": new_id, "title": f"Review: {top['misconception'] or top['concept']}",
            "prereqs": [current_node], "est_minutes": 5, "source_refs": [r.model_dump() for r in node.source_refs[:1]]}}]})
        apply_patch(path, patch, locked)
        return RewriterOut(path_id=path.path_id, patch=patch, reason="Added a review step for the most frequent mistake.")

    example = {"path_id": path.path_id, "patch": {
        "insert": [{"after": current_node, "node": {"id": f"{current_node}r", "title": "Review: ...",
                    "prereqs": [current_node], "est_minutes": 5,
                    "source_refs": [{"source_id": "s1", "page": 12, "chunk_id": "s1-p12-c3"}]}}],
        "reorder": [], "skip": []}, "reason": "One sentence"}
    prompt = build_prompt(
        "You are Rewriter. Propose the SMALLEST change that fixes the student's gap. Insert at most 2 review nodes "
        "(new nodes must cite source_refs from existing nodes). Only reorder or skip when the student clearly knows "
        "a topic, and never skip a node that is a prerequisite of a remaining node. Explain the reason in one sentence.",
        example, {"path": ctx["path"], "current_node": current_node, "mistakes_this_session": wrong,
                  "mistake_counts": mistake_counts, "completed_nodes": locked})
    return router.call("rewriter", prompt, ctx, validate, last_resort=insert_review_step,
                       summary=f"Rewrite path {path.path_id}")
