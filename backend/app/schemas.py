"""Shared types and per-agent output schemas (agents/README.md and agents/0X-*.md).

Every agent output is validated here. A ValueError / ValidationError means
"invalid output" and makes the router move to the next fallback.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_PATH_NODES = 8          # Architect cap (agents/02-architect.md)
MAX_PATH_NODES_PATCHED = 10  # Rewriter must never grow the path beyond this
MAX_INSERTS_PER_PATCH = 2
MAX_MESSAGE_CHARS = 400
MAX_LESSON_CHARS = 2500


class Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------- shared types
class SourceRef(Strict):
    source_id: str
    page: int
    chunk_id: str


class LearnerProfile(Strict):
    learner_id: str
    grade: int = 9
    language: str = "en"
    goal: Optional[str] = None
    mistake_labels: list[str] = Field(default_factory=list)


class PathNode(Strict):
    id: str
    title: str
    prereqs: list[str] = Field(default_factory=list)
    est_minutes: int = 6
    source_refs: list[SourceRef]

    @field_validator("source_refs")
    @classmethod
    def _has_refs(cls, v):
        if not v:
            raise ValueError("every node needs at least one source ref")
        return v


# -------------------------------------------------------------------- 01 Intake
class Topic(Strict):
    id: str
    title: str
    summary: str = ""
    page_refs: list[int]


class Chunk(Strict):
    chunk_id: str
    page: int
    text: str
    kind: Literal["text", "figure"] = "text"


class IntakeOut(Strict):
    source_id: str
    language: str = "en"
    topics: list[Topic]
    chunks: list[Chunk]
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        if not self.topics:
            raise ValueError("at least one topic required")
        if len(self.topics) > 8:
            raise ValueError("at most 8 topics")
        if any(not c.text.strip() for c in self.chunks):
            raise ValueError("empty chunk")
        pages = {c.page for c in self.chunks}
        for t in self.topics:
            if not t.page_refs or any(p not in pages for p in t.page_refs):
                raise ValueError(f"topic {t.id} has a page ref not present in chunks")
        return self


# ------------------------------------------------------------------ 02 Architect
class PathOut(Strict):
    path_id: str
    start_node: str
    nodes: list[PathNode]
    edges: list[tuple[str, str]] = Field(default_factory=list)


def check_path(path: PathOut, max_nodes: int = MAX_PATH_NODES) -> PathOut:
    """Code-side validation: size, ids, prereqs exist, acyclic, start exists."""
    if not path.nodes:
        raise ValueError("path has no nodes")
    if len(path.nodes) > max_nodes:
        raise ValueError(f"path has {len(path.nodes)} nodes, max {max_nodes}")
    ids = [n.id for n in path.nodes]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate node ids")
    idset = set(ids)
    if path.start_node not in idset:
        raise ValueError("start_node does not exist")
    for n in path.nodes:
        for p in n.prereqs:
            if p not in idset:
                raise ValueError(f"prereq {p} of {n.id} does not exist")
    for a, b in path.edges:
        if a not in idset or b not in idset:
            raise ValueError("edge references unknown node")
    if not is_acyclic(path):
        raise ValueError("path graph has a cycle")
    return path


def is_acyclic(path: PathOut) -> bool:
    """Kahn's algorithm over prereq links and explicit edges."""
    succ: dict[str, set[str]] = {n.id: set() for n in path.nodes}
    for n in path.nodes:
        for p in n.prereqs:
            succ[p].add(n.id)
    for a, b in path.edges:
        succ[a].add(b)
    indeg = {i: 0 for i in succ}
    for a in succ:
        for b in succ[a]:
            indeg[b] += 1
    queue = [i for i, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        cur = queue.pop()
        seen += 1
        for b in succ[cur]:
            indeg[b] -= 1
            if indeg[b] == 0:
                queue.append(b)
    return seen == len(succ)


# ------------------------------------------------------------------- 03 Teacher
class LessonStep(Strict):
    type: Literal["explain", "example", "check"]
    text: str


class Lesson(Strict):
    title: str
    steps: list[LessonStep]
    key_points: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef]


class LessonOut(Strict):
    node_id: str
    language: str
    lesson: Lesson

    @model_validator(mode="after")
    def _check(self):
        n = len(self.lesson.steps)
        if not 3 <= n <= 5:
            raise ValueError(f"lesson must have 3-5 steps, got {n}")
        if not self.lesson.source_refs:
            raise ValueError("lesson has no source refs")
        total = sum(len(s.text) for s in self.lesson.steps)
        if total > MAX_LESSON_CHARS:
            raise ValueError(f"lesson too long ({total} chars)")
        if any(not s.text.strip() for s in self.lesson.steps):
            raise ValueError("empty lesson step")
        return self


# -------------------------------------------------------------------- 04 Critic
class CriticIssue(Strict):
    step: int
    problem: str
    source_ref: Optional[SourceRef] = None


class CriticOut(Strict):
    verdict: Literal["approve", "revise", "reject"]
    issues: list[CriticIssue] = Field(default_factory=list)
    confidence: float = 0.5

    @model_validator(mode="after")
    def _check(self):
        if self.verdict == "reject" and not self.issues:
            raise ValueError("reject requires at least one issue")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence out of range")
        return self


# ------------------------------------------------------------------ 05 Quizzer
class Question(Strict):
    id: str
    type: Literal["mcq"] = "mcq"
    text: str
    options: list[str]
    answer_key: str
    concept: str
    hint: str
    source_ref: Optional[SourceRef] = None

    @model_validator(mode="after")
    def _check(self):
        if len(self.options) < 2 or len(set(self.options)) != len(self.options):
            raise ValueError(f"{self.id}: options must be unique, at least 2")
        if resolve_key(self) is None:
            raise ValueError(f"{self.id}: answer_key does not match exactly one option")
        if not self.concept.strip() or not self.hint.strip():
            raise ValueError(f"{self.id}: concept and hint are required")
        return self


def resolve_key(q: Question) -> Optional[str]:
    """Return the correct option text. answer_key may be the option text or a letter A-D."""
    if q.answer_key in q.options:
        return q.answer_key
    k = q.answer_key.strip().upper()
    if len(k) == 1 and "A" <= k <= "Z":
        idx = ord(k) - ord("A")
        if idx < len(q.options):
            return q.options[idx]
    return None


class QuizOut(Strict):
    node_id: str
    questions: list[Question]

    @model_validator(mode="after")
    def _check(self):
        if not 3 <= len(self.questions) <= 5:
            raise ValueError("quiz must have 3-5 questions")
        ids = [q.id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate question ids")
        return self


# ---------------------------------------------------------------- 06 Diagnoser
class DiagnoserOut(Strict):
    question_id: str
    result: Literal["correct", "partial", "wrong"]
    misconception: Optional[str] = None
    confidence: float = 0.5
    hint: str = ""

    @model_validator(mode="after")
    def _check(self):
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence out of range")
        return self


# ------------------------------------------------------------------ 07 Rewriter
class InsertOp(Strict):
    after: str
    node: PathNode


class Patch(Strict):
    insert: list[InsertOp] = Field(default_factory=list)
    reorder: list[str] = Field(default_factory=list)  # ids; listed nodes take their slots in this order
    skip: list[str] = Field(default_factory=list)


class RewriterOut(Strict):
    path_id: str
    patch: Patch
    reason: str = ""


# ----------------------------------------------------------------- 08 Companion
class MemoryUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")  # only allowed fields (spec)

    mistake_labels_add: list[str] = Field(default_factory=list)
    last_node: Optional[str] = None
    notes: Optional[str] = None


class CompanionOut(Strict):
    message: str
    suggested_next: Optional[str] = None
    memory_updates: MemoryUpdates = Field(default_factory=MemoryUpdates)

    @field_validator("message")
    @classmethod
    def _len(cls, v):
        if not v.strip():
            raise ValueError("empty message")
        if len(v) > MAX_MESSAGE_CHARS:
            raise ValueError("message over length cap")
        return v


# ------------------------------------------------- doubts (voice Q&A, extension)
class DoubtOut(Strict):
    answer: str
    source_refs: list[SourceRef] = Field(default_factory=list)
