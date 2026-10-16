import pytest
from pydantic import ValidationError

from app.schemas import CriticOut, PathOut, Question, check_path, resolve_key

REF = {"source_id": "s1", "page": 1, "chunk_id": "s1-p1-c1"}


def node(i, prereqs=()):
    return {"id": i, "title": i, "prereqs": list(prereqs), "est_minutes": 5, "source_refs": [REF]}


def test_valid_path():
    p = PathOut(path_id="p", start_node="a", nodes=[node("a"), node("b", ["a"])], edges=[("a", "b")])
    assert check_path(p)


def test_cycle_rejected():
    p = PathOut(path_id="p", start_node="a", nodes=[node("a", ["b"]), node("b", ["a"])])
    with pytest.raises(ValueError, match="cycle"):
        check_path(p)


def test_missing_prereq_and_size_cap():
    with pytest.raises(ValueError, match="does not exist"):
        check_path(PathOut(path_id="p", start_node="a", nodes=[node("a", ["zzz"])]))
    many = [node(f"n{i}") for i in range(9)]
    with pytest.raises(ValueError, match="max 8"):
        check_path(PathOut(path_id="p", start_node="n0", nodes=many))


def test_node_needs_source_ref():
    with pytest.raises(ValidationError):
        PathOut(path_id="p", start_node="a", nodes=[{"id": "a", "title": "a", "source_refs": []}])


def test_question_key_resolution():
    q = Question(id="q1", text="?", options=["x", "y", "z"], answer_key="B", concept="c", hint="h")
    assert resolve_key(q) == "y"
    with pytest.raises(ValidationError):
        Question(id="q1", text="?", options=["x", "y"], answer_key="nope", concept="c", hint="h")
    with pytest.raises(ValidationError):
        Question(id="q1", text="?", options=["x", "x"], answer_key="x", concept="c", hint="h")


def test_critic_reject_needs_issue():
    with pytest.raises(ValidationError):
        CriticOut(verdict="reject", issues=[])
