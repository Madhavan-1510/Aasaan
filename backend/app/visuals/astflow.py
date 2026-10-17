"""Python snippet -> flowchart spec using the `ast` module. No model is involved.

Handles if/elif/else, for, while, return, break, continue and plain statements. Runs of simple statements collapse into one
box. If the result would exceed the node cap, the code is too big for one diagram and we return None (the caller falls back).
"""
from __future__ import annotations

import ast
import re
import textwrap
from typing import Optional

from .specs import MAX_LABEL_WORDS, MAX_NODES, FlowchartSpec, spec_dict

CODE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S | re.I)


class TooBig(Exception):
    pass


def _short(text: str, words: int = MAX_LABEL_WORDS) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    parts = text.split(" ")
    if len(parts) > words:
        text = " ".join(parts[:words])
    return text[:34]


def _src(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return "step"


def _stmt_label(s: ast.stmt) -> str:
    if isinstance(s, ast.Assign):
        return _short(f"{_src(s.targets[0])} = {_src(s.value)}")
    if isinstance(s, ast.AugAssign):
        return _short(_src(s))
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
        name = _src(s.value.func)
        return _short(("print output" if name == "print" else f"call {name}"))
    if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return _short(f"define {s.name}")
    return _short(_src(s).split("\n")[0])


class Builder:
    def __init__(self):
        self.nodes: list[dict] = []
        self.edges: list[dict] = []

    def node(self, label: str, shape: str = "process") -> str:
        if len(self.nodes) >= MAX_NODES:
            raise TooBig()
        nid = f"n{len(self.nodes) + 1}"
        self.nodes.append({"id": nid, "label": label or "step", "shape": shape})
        return nid

    def edge(self, a: str, b: str, label: Optional[str] = None):
        e = {"from": a, "to": b}
        if label:
            e["label"] = label
        if not any(x["from"] == a and x["to"] == b and x.get("label") == label for x in self.edges):
            self.edges.append(e)

    def block(self, stmts: list[ast.stmt], entries: list[tuple[str, Optional[str]]], loop: Optional[tuple[str, list]] = None, end: Optional[str] = None):
        """Add `stmts` after `entries` ([(node, edge label)]); return the dangling exits in the same form."""
        run: list[ast.stmt] = []

        def flush():
            nonlocal entries, run
            if not run:
                return
            label = _stmt_label(run[0]) if len(run) == 1 else _short(f"{_stmt_label(run[0])} and {len(run) - 1} more", 6)
            n = self.node(label)
            for a, lab in entries:
                self.edge(a, n, lab)
            entries, run = [(n, None)], []

        for s in stmts:
            if isinstance(s, (ast.Import, ast.ImportFrom, ast.Pass)) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)):
                continue
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
                entries = self.block(s.body, entries, loop, end)  # draw the function body inline
                continue
            if isinstance(s, ast.If):
                flush()
                d = self.node(_short(_src(s.test)) + "?", "decision")
                for a, lab in entries:
                    self.edge(a, d, lab)
                yes = self.block(s.body, [(d, "yes")], loop, end)
                no = self.block(s.orelse, [(d, "no")], loop, end) if s.orelse else [(d, "no")]
                entries = yes + no
            elif isinstance(s, (ast.For, ast.While)):
                flush()
                test = f"more in {_src(s.target)}" if isinstance(s, ast.For) else _src(s.test)
                d = self.node(_short(test) + "?", "decision")
                for a, lab in entries:
                    self.edge(a, d, lab)
                breaks: list = []
                tail = self.block(s.body, [(d, "yes")], (d, breaks), end)
                for a, lab in tail:
                    self.edge(a, d, lab)
                entries = [(d, "no")] + breaks
            elif isinstance(s, ast.Return):
                flush()
                n = self.node(_short("return " + (_src(s.value) if s.value else "")), "io")
                for a, lab in entries:
                    self.edge(a, n, lab)
                if end:
                    self.edge(n, end)
                entries = []
            elif isinstance(s, ast.Break) and loop:
                flush()
                loop[1].extend(entries)
                entries = []
            elif isinstance(s, ast.Continue) and loop:
                flush()
                for a, lab in entries:
                    self.edge(a, loop[0], lab)
                entries = []
            else:
                run.append(s)
            if not entries and not run:
                break
        flush()
        return entries


def extract_python(text: str) -> list[str]:
    """Python code blocks from markdown notes (fenced, or a whole snippet that parses)."""
    out = [m.group(1) for m in CODE_RE.finditer(text or "")]
    return [textwrap.dedent(c).strip("\n") for c in out if c.strip()]


def code_to_flowchart(code: str, title: str = "How the code runs") -> Optional[dict]:
    """A validated flowchart spec dict for the code, or None when it can't be drawn (syntax error, nothing to draw, too big)."""
    try:
        tree = ast.parse(textwrap.dedent(code))
    except SyntaxError:
        return None
    b = Builder()
    try:
        start = b.node("Start", "start")
        end = b.node("End", "end")
        exits = b.block(tree.body, [(start, None)], None, end)
        if len(b.nodes) <= 2:
            return None
        for a, lab in exits:
            b.edge(a, end, lab)
        spec = FlowchartSpec.model_validate({"type": "flowchart", "title": _short(title, 8) or "Code flow", "nodes": b.nodes, "edges": b.edges,
                                             "alt_text": "Flowchart of the code: " + "; ".join(n["label"] for n in b.nodes[2:8])})
    except (TooBig, ValueError):
        return None
    reachable = {"n1"}
    for _ in range(len(b.nodes)):
        for e in b.edges:
            if e["from"] in reachable:
                reachable.add(e["to"])
    if len(reachable) < len(b.nodes):  # dead code after a return: keep only what is reachable
        keep = [n for n in b.nodes if n["id"] in reachable]
        edges = [e for e in b.edges if e["from"] in reachable and e["to"] in reachable]
        try:
            spec = FlowchartSpec.model_validate({**spec_dict(spec), "nodes": keep, "edges": edges})
        except ValueError:
            return None
    return spec_dict(spec)
