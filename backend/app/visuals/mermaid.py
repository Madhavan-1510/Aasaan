"""Parse-check a small Mermaid flowchart subset and convert it to a flowchart spec. We never render Mermaid itself:
the spec goes through the same validation and deterministic renderer as everything else.

Supported: `flowchart TD|LR|TB|BT` or `graph ...`, nodes `A[text]`, `A(text)`, `A([text])`, `A{text}`, `A[/text/]`,
edges `A --> B`, `A -->|label| B`, `A -- label --> B`, chained edges.
"""
from __future__ import annotations

import re
from typing import Optional

from .specs import FlowchartSpec, spec_dict


class MermaidError(ValueError):
    """Message is written so it can be fed back to a model for one repair attempt."""


NODE = re.compile(r"""(?P<id>[A-Za-z_][\w]*)\s*(?:
    \(\[(?P<stadium>[^\]]*)\]\) | \{\{(?P<hex>[^}]*)\}\} | \{(?P<diamond>[^}]*)\} | \[/(?P<io>[^\]]*?)/\] |
    \[(?P<box>[^\]]*)\] | \((?P<round>[^)]*)\) )?""", re.X)
ARROW = re.compile(r"\s*(?:--\s*(?P<l1>[^->|]+?)\s*-->|-->\s*\|(?P<l2>[^|]*)\||-->)\s*")


def _clean(t: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().strip('"').replace("<br>", " ").replace("<br/>", " "))


def parse(text: str, title: str = "Diagram", alt: str = "") -> dict:
    lines = [l.strip() for l in re.split(r"[\n;]", text or "") if l.strip() and not l.strip().startswith("%%")]
    if not lines or not re.match(r"^(flowchart|graph)\b", lines[0], re.I):
        raise MermaidError("must start with `flowchart TD` (or `graph TD`)")
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def take(s: str, pos: int):
        m = NODE.match(s, pos)
        if not m:
            raise MermaidError(f"cannot read a node at '{s[pos:pos + 20]}'")
        nid = m.group("id")
        label, shape = None, None
        for g, sh in (("stadium", None), ("hex", "process"), ("diamond", "decision"), ("io", "io"), ("box", "process"), ("round", "process")):
            if m.group(g) is not None:
                label, shape = _clean(m.group(g)), sh
                if g == "stadium":
                    shape = "start"
                break
        cur = nodes.setdefault(nid, {"id": nid, "label": nid, "shape": "process"})
        if label:
            cur["label"] = label
            cur["shape"] = shape or cur["shape"]
        return nid, m.end()

    for line in lines[1:]:
        if re.match(r"^(subgraph|end|style|classDef|class|click|linkStyle)\b", line):
            if line.startswith(("subgraph", "click")):
                raise MermaidError("subgraphs and click handlers are not supported; use plain nodes and arrows")
            continue
        pos = 0
        prev, pos = take(line, 0)
        while pos < len(line):
            a = ARROW.match(line, pos)
            if not a:
                raise MermaidError(f"expected an arrow `-->` after '{line[max(0, pos - 12):pos]}'")
            nxt, pos = take(line, a.end())
            lab = _clean(a.group("l1") or a.group("l2")) or None
            edges.append({"from": prev, "to": nxt, **({"label": lab} if lab else {})})
            prev = nxt
    if not edges:
        raise MermaidError("no edges found")
    # nodes without outgoing edges are ends, the first node with no incoming edge is the start (unless shaped already)
    has_in = {e["to"] for e in edges}
    has_out = {e["from"] for e in edges}
    for n in nodes.values():
        if n["shape"] == "process" and n["id"] not in has_in:
            n["shape"] = "start"
        elif n["shape"] == "process" and n["id"] not in has_out:
            n["shape"] = "end"
        elif n["shape"] == "start" and n["id"] in has_in:      # a stadium node that is entered is a middle step or the end
            n["shape"] = "process" if n["id"] in has_out else "end"
    try:
        spec = FlowchartSpec.model_validate({"type": "flowchart", "title": title, "alt_text": alt or f"Flowchart: {title}",
                                             "nodes": list(nodes.values()), "edges": edges})
    except ValueError as e:
        raise MermaidError(str(e)) from e
    return spec_dict(spec)
