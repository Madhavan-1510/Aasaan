import pytest

from app.agents.rewriter import apply_patch
from app.schemas import Patch, PathOut

REF = {"source_id": "s1", "page": 1, "chunk_id": "s1-p1-c1"}


def n(i, pre=()):
    return {"id": i, "title": i, "prereqs": list(pre), "est_minutes": 5, "source_refs": [REF]}


def base():
    return PathOut(path_id="p", start_node="a", nodes=[n("a"), n("b", ["a"]), n("c", ["b"])],
                   edges=[("a", "b"), ("b", "c")])


def test_insert_review_node_rewires_edges():
    patch = Patch.model_validate({"insert": [{"after": "a", "node": n("ar", ["a"])}]})
    out = apply_patch(base(), patch)
    assert [x.id for x in out.nodes] == ["a", "ar", "b", "c"]
    assert ("a", "ar") in out.edges and ("ar", "b") in out.edges and ("a", "b") not in out.edges


def test_max_two_inserts():
    patch = Patch.model_validate({"insert": [{"after": "a", "node": n(f"x{i}")} for i in range(3)]})
    with pytest.raises(ValueError, match="at most 2"):
        apply_patch(base(), patch)


def test_skip_prereq_of_remaining_rejected():
    with pytest.raises(ValueError, match="prerequisite"):
        apply_patch(base(), Patch(skip=["b"]))


def test_skip_leaf_ok_and_locked_protected():
    assert [x.id for x in apply_patch(base(), Patch(skip=["c"])).nodes] == ["a", "b"]
    with pytest.raises(ValueError, match="completed"):
        apply_patch(base(), Patch(skip=["c"]), locked=["c"])


def test_path_never_exceeds_ten_nodes():
    nodes = [n(f"n{i}") for i in range(9)]
    p = PathOut(path_id="p", start_node="n0", nodes=nodes)
    patch = Patch.model_validate({"insert": [{"after": "n0", "node": n("r1")}, {"after": "n0", "node": n("r2")}]})
    with pytest.raises(ValueError, match="max 10"):
        apply_patch(p, patch)
