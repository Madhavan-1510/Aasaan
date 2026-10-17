"""Diagram specs. Models emit small JSON; these classes validate it. Models NEVER emit drawing syntax.

Caps: at most MAX_NODES nodes or MAX_ELEMENTS elements, labels at most MAX_LABEL_WORDS words.
Every spec has `type`, `title` and `alt_text` (all required, non-empty).
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from .safeexpr import ExprError, parse as parse_expr

MAX_NODES, MAX_ELEMENTS, MAX_LABEL_WORDS = 12, 16, 5
KINDS = ("flowchart", "array", "linked_list", "stack_queue", "tree", "graph", "table", "timeline", "plot")


def check_label(label: object, what: str = "label") -> str:
    s = str(label).strip()
    if not s:
        raise ValueError(f"empty {what}")
    if len(s.split()) > MAX_LABEL_WORDS:
        raise ValueError(f"{what} '{s[:30]}...' has more than {MAX_LABEL_WORDS} words")
    return s


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    title: str
    alt_text: str

    @field_validator("title", "alt_text")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("title and alt_text are required")
        return v.strip()


# ---------------------------------------------------------------------------- flowchart
class FlowNode(BaseModel):
    id: str
    label: str
    shape: Literal["start", "end", "process", "decision", "io"] = "process"

    @field_validator("label")
    @classmethod
    def _label(cls, v):
        return check_label(v)


class FlowEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    label: Optional[str] = None


class FlowchartSpec(_Base):
    type: Literal["flowchart"] = "flowchart"
    nodes: list[FlowNode]
    edges: list[FlowEdge]

    @model_validator(mode="after")
    def _check(self):
        ids = [n.id for n in self.nodes]
        if not 2 <= len(ids) <= MAX_NODES:
            raise ValueError(f"a flowchart needs 2-{MAX_NODES} nodes, got {len(ids)}")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate node ids")
        if len(self.edges) > MAX_ELEMENTS + 4:
            raise ValueError("too many edges")
        for e in self.edges:
            if e.from_ not in ids or e.to not in ids:
                raise ValueError(f"edge {e.from_}->{e.to} references an unknown node")
            if e.label:
                check_label(e.label, "edge label")
        for n in self.nodes:
            if n.shape == "decision":
                out = [e for e in self.edges if e.from_ == n.id]
                if len(out) != 2 or any(not (e.label or "").strip() for e in out):
                    raise ValueError(f"decision '{n.label}' needs exactly two labelled outgoing edges")
        return self


# ------------------------------------------------------------------------------- array
class Pointer(BaseModel):
    name: str
    index: int


class ArraySpec(_Base):
    type: Literal["array"] = "array"
    values: list[Union[int, float, str]]
    highlight: list[int] = Field(default_factory=list)
    pointers: list[Pointer] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        n = len(self.values)
        if not 1 <= n <= MAX_ELEMENTS:
            raise ValueError(f"an array needs 1-{MAX_ELEMENTS} values")
        for v in self.values:
            check_label(v, "value")
        if any(not 0 <= i < n for i in self.highlight) or any(not 0 <= p.index < n for p in self.pointers):
            raise ValueError("highlight/pointer index out of range")
        for p in self.pointers:
            check_label(p.name, "pointer name")
        return self


class LinkedListSpec(_Base):
    type: Literal["linked_list"] = "linked_list"
    values: list[Union[int, float, str]]
    doubly: bool = False
    head_label: Optional[str] = "head"

    @model_validator(mode="after")
    def _check(self):
        if not 1 <= len(self.values) <= MAX_NODES:
            raise ValueError(f"a linked list needs 1-{MAX_NODES} values")
        for v in self.values:
            check_label(v, "value")
        if self.head_label:
            check_label(self.head_label, "head label")
        return self


class StackQueueSpec(_Base):
    type: Literal["stack_queue"] = "stack_queue"
    kind: Literal["stack", "queue"]
    items: list[Union[int, float, str]]
    operation: Optional[str] = None

    @model_validator(mode="after")
    def _check(self):
        if not 1 <= len(self.items) <= MAX_NODES:
            raise ValueError(f"needs 1-{MAX_NODES} items")
        for v in self.items:
            check_label(v, "item")
        if self.operation:
            check_label(self.operation, "operation")
        return self


# --------------------------------------------------------------------------------- tree
class TreeNode(BaseModel):
    id: str
    label: str

    @field_validator("label")
    @classmethod
    def _label(cls, v):
        return check_label(v)


class TreeEdge(BaseModel):
    parent: str
    child: str


class TreeSpec(_Base):
    type: Literal["tree"] = "tree"
    nodes: list[TreeNode]
    edges: list[TreeEdge]
    highlight: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self):
        ids = [n.id for n in self.nodes]
        if not 1 <= len(ids) <= MAX_NODES or len(set(ids)) != len(ids):
            raise ValueError(f"a tree needs 1-{MAX_NODES} nodes with unique ids")
        parents: dict[str, str] = {}
        for e in self.edges:
            if e.parent not in ids or e.child not in ids:
                raise ValueError("edge references an unknown node")
            if e.child in parents:
                raise ValueError(f"node {e.child} has more than one parent: not a tree")
            parents[e.child] = e.parent
        roots = [i for i in ids if i not in parents]
        if len(roots) != 1:
            raise ValueError(f"a tree has exactly one root, found {len(roots)}")
        for i in ids:  # no cycles and everything reachable from the root
            seen, cur = set(), i
            while cur in parents:
                if cur in seen:
                    raise ValueError("cycle: not a tree")
                seen.add(cur)
                cur = parents[cur]
            if cur != roots[0]:
                raise ValueError("disconnected: not a tree")
        if any(h not in ids for h in self.highlight):
            raise ValueError("highlight references an unknown node")
        return self


# -------------------------------------------------------------------------------- graph
class GraphNode(BaseModel):
    id: str
    label: Optional[str] = None


class GraphEdge(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    weight: Optional[Union[int, float]] = None


class GraphSpec(_Base):
    type: Literal["graph"] = "graph"
    nodes: list[Union[str, GraphNode]]
    edges: list[GraphEdge]
    directed: bool = False

    @field_validator("nodes")
    @classmethod
    def _norm(cls, v):
        return [GraphNode(id=n, label=n) if isinstance(n, str) else GraphNode(id=n.id, label=n.label or n.id) for n in v]

    @model_validator(mode="after")
    def _check(self):
        ids = [n.id for n in self.nodes]
        if not 2 <= len(ids) <= MAX_NODES or len(set(ids)) != len(ids):
            raise ValueError(f"a graph needs 2-{MAX_NODES} nodes with unique ids")
        if len(self.edges) > MAX_ELEMENTS:
            raise ValueError(f"at most {MAX_ELEMENTS} edges")
        for n in self.nodes:
            check_label(n.label)
        for e in self.edges:
            if e.from_ not in ids or e.to not in ids:
                raise ValueError("edge references an unknown node")
        return self


# -------------------------------------------------------------------------------- table
class TableSpec(_Base):
    type: Literal["table"] = "table"
    columns: list[str]
    rows: list[list[Union[int, float, str]]]

    @model_validator(mode="after")
    def _check(self):
        if not 2 <= len(self.columns) <= 6 or not 1 <= len(self.rows) <= 8:
            raise ValueError("a table needs 2-6 columns and 1-8 rows")
        if (len(self.rows) + 1) * len(self.columns) > MAX_ELEMENTS + 8:
            raise ValueError("table is too large")
        for c in self.columns:
            check_label(c, "column")
        for r in self.rows:
            if len(r) != len(self.columns):
                raise ValueError("every row must have one cell per column")
            for cell in r:
                check_label(cell, "cell")
        return self


class TimelineEvent(BaseModel):
    label: str
    when: Union[int, float, str]

    @field_validator("label")
    @classmethod
    def _label(cls, v):
        return check_label(v)


class TimelineSpec(_Base):
    type: Literal["timeline"] = "timeline"
    events: list[TimelineEvent]

    @model_validator(mode="after")
    def _check(self):
        if not 2 <= len(self.events) <= MAX_NODES:
            raise ValueError(f"a timeline needs 2-{MAX_NODES} events")
        for e in self.events:
            check_label(e.when, "date")
        return self


class PlotLabels(BaseModel):
    x: str = "x"
    y: str = "y"


class PlotSpec(_Base):
    type: Literal["plot"] = "plot"
    fn: str
    x_range: tuple[float, float]
    y_range: Optional[tuple[float, float]] = None
    labels: PlotLabels = Field(default_factory=PlotLabels)

    @model_validator(mode="after")
    def _check(self):
        try:
            parse_expr(self.fn)
        except ExprError as e:
            raise ValueError(str(e)) from e
        if not self.x_range[0] < self.x_range[1] or self.x_range[1] - self.x_range[0] > 1e6:
            raise ValueError("x_range must be increasing")
        if self.y_range and not self.y_range[0] < self.y_range[1]:
            raise ValueError("y_range must be increasing")
        check_label(self.labels.x, "axis label")
        check_label(self.labels.y, "axis label")
        return self


Spec = Annotated[Union[FlowchartSpec, ArraySpec, LinkedListSpec, StackQueueSpec, TreeSpec, GraphSpec, TableSpec, TimelineSpec, PlotSpec],
                 Field(discriminator="type")]
_adapter = TypeAdapter(Spec)


def parse_spec(data: dict):
    """Validate a spec dict (raises pydantic ValidationError, a ValueError, with a readable message for the repair retry)."""
    if not isinstance(data, dict) or "type" not in data:
        raise ValueError("spec must be a JSON object with a `type`")
    if data["type"] not in KINDS:
        raise ValueError(f"unknown spec type '{data['type']}'; use one of {', '.join(KINDS)}")
    return _adapter.validate_python(data)


def spec_dict(spec) -> dict:
    return spec.model_dump(by_alias=True, exclude_none=True)
