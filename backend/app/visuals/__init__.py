"""Deterministic visuals for Aasaan. Code draws; models only fill small, validated JSON specs.

specs.py      the nine diagram specs (pydantic) with caps and structural validation
render.py     spec -> SVG (both colour themes), plus a text-fit check
astflow.py    Python snippet -> flowchart spec, no model involved
mermaid.py    parse-check a small Mermaid subset and convert it to a flowchart spec (we never render Mermaid itself)
safeexpr.py   whitelisted function expressions for plots (no eval)
figures.py    real-world figures from Wikimedia Commons, then Openverse, with licence and attribution
cover.py      generated notebook cover SVG
"""


def register(tools) -> None:
    """Show the deterministic Illustrator tools in /api/health/tools, each with a real self-check (not just 'imported')."""
    from . import astflow, cover, mermaid, render, safeexpr, specs

    def renderer():
        s = specs.parse_spec({"type": "array", "title": "t", "alt_text": "a", "values": [1, 2]})
        problems = render.check_render(render.render(s), s)
        if problems:
            raise RuntimeError("; ".join(problems))
        return {"description": "spec -> SVG renderer (9 diagram kinds, light and dark)", "kinds": list(specs.KINDS)}

    def ast_check():
        if not astflow.code_to_flowchart("x = 1\nif x:\n    x = 2"):
            raise RuntimeError("AST flowchart failed on a sample")
        return {"description": "Python AST -> flowchart spec, no model call"}

    def mermaid_check():
        mermaid.parse("flowchart TD\nA([Start]) --> B([End])")
        return {"description": "Mermaid subset parse-check, converted to a flowchart spec"}

    def plot_check():
        safeexpr.compile_expr("sin(x)")(0.0)
        return {"description": "whitelisted plot expressions (ast, never eval)"}

    def cover_check():
        if "<svg" not in cover.cover_svg("Test"):
            raise RuntimeError("cover failed")
        return {"description": "generated notebook cover SVG"}

    for name, fn in (("illustrator_renderer", renderer), ("ast_flowchart", ast_check), ("mermaid_check", mermaid_check),
                     ("plot_expression", plot_check), ("notebook_cover", cover_check)):
        tools.register_local(name, fn)
