"""02 Architect: topics -> learning path of at most 8 nodes (agents/02-architect.md)."""
from __future__ import annotations

from ..router import ModelRouter, RouterResult
from ..schemas import IntakeOut, LearnerProfile, PathOut, check_path
from .common import build_prompt, chunk_ids, lang_name, ref_of


def run(router: ModelRouter, intake: IntakeOut, profile: LearnerProfile, path_id: str) -> RouterResult:
    chunks = [c.model_dump() for c in intake.chunks]
    known = chunk_ids(chunks)
    ctx = {
        "path_id": path_id, "source_id": intake.source_id, "topics": [t.model_dump() for t in intake.topics],
        "chunks": chunks, "mistake_labels": profile.mistake_labels,
    }

    def validate(raw: dict) -> PathOut:
        raw = {**raw, "path_id": path_id}
        path = check_path(PathOut.model_validate(raw))
        for n in path.nodes:
            for r in n.source_refs:
                if r.chunk_id not in known:
                    raise ValueError(f"node {n.id} cites unknown chunk {r.chunk_id}")
        return path

    def linear_path(c: dict) -> PathOut:
        """Last resort: one node per topic, in topic order."""
        nodes = []
        for t in c["topics"][:8]:
            cs = [ch for ch in c["chunks"] if ch["page"] in t["page_refs"]][:2] or c["chunks"][:1]
            nodes.append({"id": f"n{len(nodes) + 1}", "title": t["title"], "est_minutes": 6,
                          "prereqs": [f"n{len(nodes)}"] if nodes else [],
                          "source_refs": [ref_of(ch) for ch in cs]})
        edges = [(nodes[i]["id"], nodes[i + 1]["id"]) for i in range(len(nodes) - 1)]
        return check_path(PathOut(path_id=path_id, start_node=nodes[0]["id"], nodes=nodes, edges=edges))

    example = {
        "path_id": path_id, "start_node": "n1",
        "nodes": [{"id": "n1", "title": "What is motion", "prereqs": [], "est_minutes": 6,
                   "source_refs": [{"source_id": intake.source_id, "page": 10, "chunk_id": f"{intake.source_id}-p10-c1"}]}],
        "edges": [["n1", "n2"]],
    }
    prompt = build_prompt(
        "You are Architect. Order the topics so prerequisites come first. At most 8 nodes; every node maps to "
        "source_refs using the given chunk_ids. If the learner has mistake_labels, add one short review node "
        "near the start for any related concept. Fit total time to the goal if one is given. The graph must be "
        "acyclic and every prereq id must exist.",
        example,
        {"topics": ctx["topics"], "chunks": [{"chunk_id": c["chunk_id"], "page": c["page"]} for c in chunks],
         "grade": profile.grade, "goal": profile.goal, "mistake_labels": profile.mistake_labels,
         "student_language": lang_name(profile.language)},
    )
    return router.call("architect", prompt, ctx, validate, last_resort=linear_path,
                       summary=f"Path {path_id} from {len(intake.topics)} topics")
