"""Illustrator: specs, deterministic rendering (light + dark), AST flowcharts without a model, Mermaid check, plot expressions,
real figures with licences, the fallback ladder, caching, and that a failing visual never blocks a chapter."""
import xml.etree.ElementTree as ET

import pytest

from app import web
from app.agents import illustrator
from app.providers.typesafe import choice, noul
from app.router import ModelRouter
from app.tools import Chaos, ToolRunner
from app.visuals import astflow, cover, mermaid, render, safeexpr, specs
from app.visuals.figures import license_ok
from conftest import FakeResp

CODE_NOTES = """## Linear search
Linear search checks every item in the list in order until it finds the target value, then it stops and reports where it was.

```python
def find(items, target):
    for i, x in enumerate(items):
        if x == target:
            return i
    return -1
```
"""
LL_NOTES = "## Linked list nodes\nA linked list keeps the values 4, 8 and 15 in separate nodes, and every node points to the next node in the chain.\n"
HEART_NOTES = "## The human heart\nThe heart pumps blood around the body through four chambers called two atria and two ventricles which work together.\n"


def decisions(kind, wants=0.9):
    """A stand-in for router.decide that answers every section with the given kind."""
    def fake(gate, state, questions, **kw):
        if gate != "illustrator":
            return None
        out = {}
        for qid in questions:
            out[qid] = {"type": "noul", "noul": wants, "_by": "jev"} if qid.startswith("v_") else {"type": "choice", "choice": kind, "confidence": 0.9, "_by": "jev"}
        return out
    return fake


@pytest.fixture
def r(store):
    return ModelRouter(store=store, mode="mock")


def agent_events(store, agent="illustrator"):
    return [e for e in store.events(500) if e["agent"] == agent]


# --------------------------------------------------------------------- specs and rendering
SAMPLES = [
    {"type": "array", "title": "t", "alt_text": "a", "values": [3, 1, 4], "highlight": [1], "pointers": [{"name": "i", "index": 2}]},
    {"type": "linked_list", "title": "t", "alt_text": "a", "values": list(range(1, 10))},
    {"type": "stack_queue", "title": "t", "alt_text": "a", "kind": "queue", "items": [1, 2, 3]},
    {"type": "tree", "title": "t", "alt_text": "a", "nodes": [{"id": "a", "label": "8"}, {"id": "b", "label": "3"}, {"id": "c", "label": "10"}],
     "edges": [{"parent": "a", "child": "b"}, {"parent": "a", "child": "c"}]},
    {"type": "graph", "title": "t", "alt_text": "a", "nodes": ["A", "B", "C"], "edges": [{"from": "A", "to": "B", "weight": 2}, {"from": "B", "to": "C"}], "directed": True},
    {"type": "table", "title": "t", "alt_text": "a", "columns": ["Op", "Cost"], "rows": [["push", "O(1)"]]},
    {"type": "timeline", "title": "t", "alt_text": "a", "events": [{"label": "A", "when": 1}, {"label": "B", "when": 2}]},
    {"type": "plot", "title": "t", "alt_text": "a", "fn": "sin(x)", "x_range": [0, 6.28]},
]


@pytest.mark.parametrize("sample", SAMPLES, ids=[s["type"] for s in SAMPLES])
def test_every_kind_renders_valid_svg_in_both_themes(sample):
    s = specs.parse_spec(sample)
    rr = render.render(s)
    assert render.check_render(rr, s) == []
    root = ET.fromstring(rr.svg)
    assert root.tag.endswith("svg") and root.get("role") == "img" and root.get("aria-label")
    assert "prefers-color-scheme: dark" in rr.svg and ".vz-text" in rr.svg      # light by default, dark variant embedded
    assert "<script" not in rr.svg and "onload" not in rr.svg


def test_labels_from_a_model_are_escaped():
    s = specs.parse_spec({"type": "array", "title": "x", "alt_text": "a", "values": ["<img src=x onerror=alert(1)>"]})
    svg = render.render(s).svg
    assert "<img" not in svg and "&lt;img" in svg


def test_spec_caps_and_structure_are_enforced():
    nodes = [{"id": f"n{i}", "label": "x"} for i in range(13)]
    with pytest.raises(ValueError):
        specs.parse_spec({"type": "flowchart", "title": "t", "alt_text": "a", "nodes": nodes, "edges": []})
    with pytest.raises(ValueError, match="more than 5 words"):
        specs.parse_spec({"type": "array", "title": "t", "alt_text": "a", "values": ["one two three four five six"]})
    with pytest.raises(ValueError, match="two labelled outgoing"):
        specs.parse_spec({"type": "flowchart", "title": "t", "alt_text": "a",
                          "nodes": [{"id": "a", "label": "Q", "shape": "decision"}, {"id": "b", "label": "B"}], "edges": [{"from": "a", "to": "b"}]})
    with pytest.raises(ValueError, match="more than one parent"):
        specs.parse_spec({"type": "tree", "title": "t", "alt_text": "a", "nodes": [{"id": "a", "label": "a"}, {"id": "b", "label": "b"}, {"id": "c", "label": "c"}],
                          "edges": [{"parent": "a", "child": "c"}, {"parent": "b", "child": "c"}]})
    with pytest.raises(ValueError):
        specs.parse_spec({"type": "array", "title": "t", "values": [1]})        # alt_text is required
    with pytest.raises(ValueError, match="unknown spec type"):
        specs.parse_spec({"type": "radar"})


def test_plot_expressions_are_whitelisted_never_evaluated():
    for bad in ('__import__("os")', "x.__class__", "open('f')", "sin(x)(1)", "lambda: 1", "[1,2]", "x if 1 else 2"):
        with pytest.raises(safeexpr.ExprError):
            safeexpr.parse(bad)
    assert safeexpr.compile_expr("x^2 + 2*x")(3) == 15
    assert safeexpr.sample("1/x", (-1, 1), 4)[2][1] is None            # division by zero is a gap, not a crash
    assert safeexpr.sample("x^99", (0, 10), 2)[2][1] is None           # exponent guard
    with pytest.raises(ValueError):
        specs.parse_spec({"type": "plot", "title": "t", "alt_text": "a", "fn": "os.system('x')", "x_range": [0, 1]})


def test_ast_flowchart_covers_loops_ifs_and_returns():
    d = astflow.code_to_flowchart(astflow.extract_python(CODE_NOTES)[0], "Linear search")
    s = specs.parse_spec(d)
    labels = [n.label for n in s.nodes]
    assert "Start" in labels and "End" in labels and any(l.endswith("?") for l in labels) and any(l.startswith("return") for l in labels)
    assert render.check_render(render.render(s), s) == []
    assert astflow.code_to_flowchart("def broken(:\n  pass") is None
    assert astflow.code_to_flowchart("\n".join(f"if a{i}:\n    b{i} = {i}" for i in range(9))) is None   # too big for one diagram


def test_mermaid_subset_is_parse_checked_and_converted():
    d = mermaid.parse("flowchart TD\nA([Start]) --> B{Sorted?}\nB -->|yes| C[Binary search]\nB -->|no| D[Sort first]\nD --> C\nC --> E([Done])")
    s = specs.parse_spec(d)
    assert {n.shape for n in s.nodes} >= {"decision", "start", "end"}
    for bad, why in (("sequenceDiagram\nA->>B: hi", "must start with"), ("flowchart TD\nA --- B", "arrow"), ("flowchart TD\nsubgraph x\nA-->B\nend", "subgraphs")):
        with pytest.raises(mermaid.MermaidError, match=why):
            mermaid.parse(bad)


def test_cover_is_deterministic_and_valid():
    a, b = cover.cover_svg("Foundations of DSA"), cover.cover_svg("Foundations of DSA")
    assert a == b and a != cover.cover_svg("Cell Biology")
    ET.fromstring(a)
    assert "<script" not in cover.cover_svg("</text><script>alert(1)</script>")


def test_licence_allow_list():
    for ok in ("CC BY-SA 4.0", "CC0", "Public domain", "CC BY 2.0"):
        assert license_ok(ok), ok
    for bad in ("CC BY-NC 4.0", "CC BY-ND 2.0", "Fair use", "", "All rights reserved"):
        assert not license_ok(bad), bad


# --------------------------------------------------------------------- acceptance: AST path needs no model
def test_python_snippet_becomes_a_flowchart_with_no_model_call(r, store):
    r.decide = decisions("code_flowchart")
    out = illustrator.run(r, store, "Searching in DSA", CODE_NOTES)
    assert len(out.blocks) == 1
    c = out.blocks[0]["content"]
    assert c["kind"] == "flowchart" and c["source_tool"] == "ast_flowchart" and c["payload"]["svg"].startswith("<svg") and c["alt_text"] and c["caption"]
    assert out.blocks[0]["type"] == "visual" and out.blocks[0]["verified"] is True
    assert [call for call in r.mock.calls if call[0] == "illustrator"] == []          # no model was asked to draw anything
    assert any("no model call" in e["error"] for e in agent_events(store))


def test_without_any_decision_provider_rules_pick_code_flowcharts_only(r, store):
    out = illustrator.run(r, store, "Mixed", CODE_NOTES + "\n" + LL_NOTES)               # mock mode: decide() returns None
    assert [b["content"]["source_tool"] for b in out.blocks] == ["ast_flowchart"]
    assert any("by rules" in e["summary"] for e in agent_events(store))


# --------------------------------------------------------------------- acceptance: spec path
def test_linked_list_chapter_gets_a_validated_spec_drawn_by_code(r, store):
    r.decide = decisions("linked_list")
    out = illustrator.run(r, store, "Linked lists", LL_NOTES)
    c = out.blocks[0]["content"]
    assert c["kind"] == "linked_list" and c["source_tool"] == "spec_renderer"
    assert c["payload"]["spec"]["values"] == [4, 8, 15] and "prefers-color-scheme: dark" in c["payload"]["svg"]
    assert [call[0] for call in r.mock.calls] == ["illustrator"]                         # one model call filled the spec


def test_invalid_spec_gets_one_repair_then_the_next_model_and_the_chapter_still_completes(r, store):
    r.decide = decisions("linked_list")
    bad = {"type": "linked_list", "title": "t", "alt_text": "a", "values": list(range(40))}
    r.mock.script("illustrator", [bad, bad, bad, bad, bad, bad])                       # every model, repair included, is wrong
    out = illustrator.run(r, store, "Linked lists", LL_NOTES)
    assert out.blocks == [] and out.skipped
    evs = agent_events(store)
    assert any("invalid output" in e["error"] for e in evs)                           # the repair attempt is visible
    assert any("deterministic_only" in e["model_used"] or "code-only last resort" in e["summary"] for e in evs)
    assert any(e["status"] == "fallback" and "no visual" in e["summary"] for e in evs)


def test_one_repair_fixes_a_first_bad_spec(r, store):
    r.decide = decisions("array")
    r.mock.script("illustrator", [{"type": "array", "title": "t", "alt_text": "a", "values": list(range(40))}])
    out = illustrator.run(r, store, "Arrays", "## Arrays\nAn array stores values 3, 1 and 4 at consecutive positions, each found by its index number.\n")
    assert out.blocks and out.blocks[0]["content"]["kind"] == "array"
    assert any("invalid output" in e["error"] for e in agent_events(store))


def test_free_form_diagram_is_mermaid_checked_then_converted(r, store):
    r.decide = decisions("diagram")
    r.mock.script("illustrator", [{"mermaid": "flowchart TD\nA --- B", "title": "t", "alt_text": "a"}])   # bad first, repaired by the mock default
    out = illustrator.run(r, store, "Process", "## A simple process\nFirst we start, then we do the step, and then we finish the whole process in order.\n")
    assert out.blocks and out.blocks[0]["content"]["source_tool"] == "mermaid_checked"
    assert any("subgraph" in e["error"] or "arrow" in e["error"] for e in agent_events(store))


def test_max_three_visuals_per_chapter(r, store):
    r.decide = decisions("linked_list")
    notes = "\n".join(f"## Part {i}\nA linked list keeps the values {i}, 8 and 15 in separate nodes, and each node points onwards to the next node.\n" for i in range(6))
    assert len(illustrator.run(r, store, "Many", notes).blocks) == illustrator.MAX_VISUALS


def test_physical_diagram_requests_are_never_model_drawn(r, store, fake_web):
    r.decide = decisions("diagram")
    out = illustrator.run(r, store, "Human heart anatomy", HEART_NOTES)
    assert out.blocks == [] and [c for c in r.mock.calls if c[0] == "illustrator"] == []


# --------------------------------------------------------------------- acceptance: faithfulness gate
def test_low_faithfulness_drops_the_visual_high_keeps_it(r, store):
    base = decisions("linked_list")

    def decide(gate, state, questions, **kw):
        if gate == "illustrator_faithful":
            return {qid: {"type": "noul", "noul": 0.1, "_by": "jev"} for qid in questions}
        return base(gate, state, questions, **kw)

    r.decide = decide
    assert illustrator.run(r, store, "Linked lists", LL_NOTES).blocks == []
    assert any("faithfulness" in e["error"] for e in agent_events(store))
    r.decide = lambda g, s, q, **kw: ({qid: {"type": "noul", "noul": 0.9, "_by": "jev"} for qid in q} if g == "illustrator_faithful" else base(g, s, q, **kw))
    assert len(illustrator.run(r, store, "Linked lists 2", LL_NOTES).blocks) == 1


# --------------------------------------------------------------------- acceptance: real figures
def commons_route(fake_web, svg=True, lic="CC BY-SA 4.0"):
    def route(url, params):
        if url.startswith("https://commons.wikimedia.org/w/api.php"):
            if "filetype:drawing" in params.get("gsrsearch", "") and not svg:
                return FakeResp(200, data={"query": {"pages": {}}}, headers={"content-type": "application/json"})
            return FakeResp(200, headers={"content-type": "application/json"}, data={"query": {"pages": {"1": {"index": 1, "title": "File:Human heart diagram.svg", "imageinfo": [{
                "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/h/heart.svg/900px-heart.svg.png", "url": "https://upload.wikimedia.org/heart.svg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Human_heart_diagram.svg", "mime": "image/svg+xml" if svg else "image/png", "width": 900,
                "extmetadata": {"Artist": {"value": "<a>Jane Doe</a>"}, "LicenseShortName": {"value": lic}, "ImageDescription": {"value": "Human heart diagram"}}}]}}}})
        if url.startswith("https://upload.wikimedia.org"):
            return FakeResp(206, headers={"content-type": "image/png"})
        return None
    fake_web.get(route)


def test_biology_chapter_gets_a_commons_svg_figure_with_licence_and_attribution(r, store, fake_web):
    commons_route(fake_web)
    r.decide = decisions("figure")
    out = illustrator.run(r, store, "The human heart", HEART_NOTES)
    c = out.blocks[0]["content"]
    assert c["kind"] == "figure" and c["source_tool"] == "commons" and c["payload"]["url"].startswith("https://upload.wikimedia.org")
    assert c["attribution"] == {"author": "Jane Doe", "license": "CC BY-SA 4.0", "source": "Wikimedia Commons",
                                "page_url": "https://commons.wikimedia.org/wiki/File:Human_heart_diagram.svg"}
    assert c["alt_text"] and c["caption"]
    assert [call for call in r.mock.calls if call[0] == "illustrator"] == []          # the model never draws a real figure


def test_a_figure_with_a_non_free_licence_is_not_used(r, store, fake_web):
    commons_route(fake_web, lic="CC BY-NC 4.0")
    r.decide = decisions("figure")
    assert illustrator.run(r, store, "The human heart", HEART_NOTES).blocks == []


def test_commons_down_falls_to_openverse_both_down_ships_without_a_figure(r, store, fake_web, monkeypatch):
    def openverse(url, params):
        if url.startswith("https://api.openverse.org/v1/images/"):
            return FakeResp(200, headers={"content-type": "application/json"}, data={"results": [{
                "title": "Heart anatomy", "url": "https://live.staticflickr.com/heart.jpg", "foreign_landing_url": "https://flickr.com/p/1", "creator": "Sam",
                "license": "by", "license_version": "2.0", "width": 1000, "tags": [{"name": "heart"}]}]})
        if url.startswith("https://live.staticflickr.com"):
            return FakeResp(200, headers={"content-type": "image/jpeg"})
        return None
    fake_web.get(openverse)
    runner = ToolRunner(store, r.sink, Chaos("tool:commons=fail"), rate_scale=1000)
    runner.sleep = lambda s: None
    monkeypatch.setattr(web, "RUNNER", runner)
    r.decide = decisions("figure")
    out = illustrator.run(r, store, "The human heart", HEART_NOTES)
    c = out.blocks[0]["content"]
    assert c["source_tool"] == "openverse" and c["attribution"]["license"] == "BY 2.0" and c["attribution"]["author"] == "Sam"

    runner.chaos.set("tool:commons=fail,tool:openverse=fail")
    out = illustrator.run(r, store, "The human heart again", HEART_NOTES)
    assert out.blocks == []
    why = [e["error"] for e in agent_events(store) if "no figure" in e["summary"]]
    assert why and "commons: unavailable" in why[0] and "openverse: unavailable" in why[0]       # events say why


# --------------------------------------------------------------------- acceptance: cache across notebooks
def test_second_notebook_on_the_same_topic_reuses_cached_visuals(r, store):
    r.decide = decisions("linked_list")
    first = illustrator.run(r, store, "Linked lists", LL_NOTES)
    calls_after_first = len([c for c in r.mock.calls if c[0] == "illustrator"])
    second = illustrator.run(r, store, "Linked lists", LL_NOTES)
    assert second.blocks == first.blocks and len([c for c in r.mock.calls if c[0] == "illustrator"]) == calls_after_first
    assert any("cache hit" in e["summary"] for e in agent_events(store))


# --------------------------------------------------------------------- the chapter is never blocked
def test_illustrator_crash_never_blocks_the_chapter(engine, worker, store, monkeypatch):
    monkeypatch.setattr(illustrator, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    nid = engine.create_notebook("L1", "Teach me binary search", "en", [])
    worker.run_until_idle()
    v = engine.notebook_view(nid)
    assert v["status"] == "complete"
    assert any("Illustrator failed" in e["summary"] for e in store.events(500))


def test_visuals_are_stored_as_blocks_right_after_the_notes(engine, worker, store, monkeypatch):
    monkeypatch.setattr(engine.router, "decide", decisions("linked_list"))
    nid = engine.create_notebook("L1", "Teach me linked lists in Python", "en", [])
    worker.run_until_idle()
    ch = engine.chapter_view(engine.notebook_view(nid)["chapters"][0]["id"])
    types = [b["type"] for b in ch["blocks"]]
    assert types[0] == "notes" and ("visual" not in types or types[1] == "visual")


# --------------------------------------------------------------------- health, cover endpoint, no Pexels
@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(db_path=str(tmp_path / "t.db"), mode="mock", start_worker=False)) as c:
        yield c


def test_health_tools_lists_illustrator_tools_and_image_sources_with_masked_keys(client, monkeypatch):
    monkeypatch.setenv("OPENVERSE_CLIENT_ID", "ov-client-id-123456")
    monkeypatch.setenv("OPENVERSE_CLIENT_SECRET", "ov-client-secret-987654")
    tools = client.get("/api/health/tools").json()["tools"]
    for name in ("illustrator_renderer", "ast_flowchart", "mermaid_check", "plot_expression", "notebook_cover", "commons", "openverse", "firecrawl"):
        assert name in tools, name
    assert tools["illustrator_renderer"]["state"] == "ok" and tools["ast_flowchart"]["kind"] == "local"
    raw = client.get("/api/health/tools").text
    assert "ov-client-secret-987654" not in raw and "ov-client-id-123456" not in raw


def test_models_health_lists_the_illustrator_chain(client):
    cfg = client.get("/api/health/models").text
    assert "sarvam_gemma4" in cfg or "illustrator" in cfg


def test_notebook_cover_endpoint_serves_svg(client):
    lid = client.post("/api/learners").json()["learner_id"]
    nid = client.post("/api/notebooks", data={"learner_id": lid, "text": "Teach me graph theory", "language": "en"}).json()["notebook_id"]
    r = client.get(f"/api/notebooks/{nid}/cover.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and "<svg" in r.text


def test_pexels_is_gone_everywhere():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    hits = [str(p.relative_to(root)) for p in list(root.glob("backend/app/**/*.py")) + [root / "config" / "models.yaml", root / ".env.example"]
            if "pexels" in p.read_text(encoding="utf-8").lower()]
    assert hits == []


# --------------------------------------------------------------------- theming (Milestone 3)
def test_diagrams_and_covers_use_theme_tokens_with_standalone_fallbacks():
    s = specs.parse_spec(SAMPLES[0])
    svg = render.render(s).svg
    assert "var(--vz-node-fill,#eef1fa)" in svg and "var(--vz-node-fill,#232a3d)" in svg          # token first, light then dark fallback
    cv = cover.cover_svg("Cloud Data Engineering")
    assert "var(--cv-bg1" in cv and "var(--cv-ink" in cv and 'style="--h:' in cv and "#" not in cv.split("<style>")[0].replace("&#", "")


def test_frontend_sets_the_theme_before_first_paint_and_defines_both_token_sets():
    from pathlib import Path
    fe = Path(__file__).resolve().parents[2] / "frontend"
    html, css = (fe / "index.html").read_text(encoding="utf-8"), (fe / "style.css").read_text(encoding="utf-8")
    assert html.index("aasaan_theme") < html.index("style.css")                                  # inline script runs before the stylesheet
    for token in ('[data-theme="light"]', '[data-theme="dark"]', ":root:not([data-theme])", "--vz-node-fill", "--cv-bg1", "--accent-fg"):
        assert token in css, token
