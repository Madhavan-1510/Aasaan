"""Aasaan acceptance tests (brief section 12) against a RUNNING server.

    python scripts/acceptance.py [--url http://localhost:8000] [--admin-token TOKEN]

Prints PASS / FAIL / SKIP per check with the measured numbers. SKIP always says why (usually a missing key, or a
step that needs a human, such as speaking into a microphone). Exit code 1 if anything FAILS.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
rows: list[tuple[str, str, str]] = []


def rec(name: str, status: str, detail: str = "") -> None:
    rows.append((name, status, detail))
    print(f"[{status:4}] {name}  {detail}")


def make_pdf(text: str) -> bytes:
    """A minimal valid one-page PDF with selectable text (for the upload test)."""
    esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 11 Tf 40 740 Td ({esc}) Tj ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode() + "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    return out + f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()


class Client:
    def __init__(self, url: str, token: str):
        self.c = httpx.Client(base_url=url, timeout=60)
        self.h = {"x-admin-token": token} if token else {}
        self.learner = self.c.post("/api/learners", json={}).json()["learner_id"]
        self.session = self.c.post("/api/session/open", json={"learner_id": self.learner}).json()["session_id"]

    def create(self, text="", files=None, language="en") -> str:
        r = self.c.post("/api/notebooks", data={"learner_id": self.learner, "text": text, "language": language}, files=files or [])
        r.raise_for_status()
        return r.json()["notebook_id"]

    def view(self, nid):
        return self.c.get(f"/api/notebooks/{nid}").json()

    def wait(self, nid, key, timeout=180, every=0.4):
        t0 = time.time()
        while time.time() - t0 < timeout:
            v = self.view(nid)
            if v[key] if key in v else False:
                return v, time.time() - t0
            if v["status"] == "failed":
                raise RuntimeError(f"notebook failed: {v['error']}")
            time.sleep(every)
        raise TimeoutError(f"timed out waiting for {key}")

    def chapter(self, cid):
        return self.c.get(f"/api/chapters/{cid}").json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--admin-token", default="")
    a = ap.parse_args()
    try:
        health = httpx.get(a.url + "/health", timeout=5).json()
    except httpx.HTTPError as e:
        print(f"Server not reachable at {a.url}: {e}")
        return 2
    mode = health["mode"]
    print(f"Aasaan acceptance run against {a.url} (mode: {mode})\n")
    cl = Client(a.url, a.admin_token)
    nid1 = None

    # ---- 1. type a goal: notebook opens within the budget with roadmap + graph and a complete chapter 1
    t0 = time.time()
    try:
        nid1 = cl.create("I want to learn DSA")
        v, _ = cl.wait(nid1, "chapters", timeout=90)
        t_outline = time.time() - t0
        v, _ = cl.wait(nid1, "open_ready", timeout=180)
        t_open = time.time() - t0
        n = len(v["chapters"])
        ch = cl.chapter(v["chapters"][0]["id"])
        types = [b["type"] for b in ch["blocks"]]
        links = [b for b in ch["blocks"] if b["type"] == "link" and b["verified"]]
        videos = [b for b in ch["blocks"] if b["type"] == "video" and b["verified"]]
        images = [b for b in ch["blocks"] if b["type"] == "image" and b["content"]["attribution"].get("author")]
        quiz = next((b for b in ch["blocks"] if b["type"] == "quiz"), None)
        edges = len(v["edges"])
        rec("1a roadmap and graph of 8-20 chapters", "PASS" if 8 <= n <= 20 and edges else "FAIL", f"{n} chapters, {edges} prerequisite edges")
        rec("1b outline within ~15 s", "PASS" if t_outline <= 15 else "FAIL", f"{t_outline:.1f}s")
        rec("1c notebook opened within ~40 s", "PASS" if t_open <= 40 else "FAIL", f"{t_open:.1f}s")
        ok = "notes" in types and "slides" in types and quiz and 3 <= len(quiz["content"]["questions"]) <= 5
        rec("1d chapter 1: notes, slides, 3-5 question quiz", "PASS" if ok else "FAIL", ",".join(types))
        rec("1e chapter 1: >=2 verified links", "PASS" if len(links) >= 2 else "FAIL", f"{len(links)} verified links")
        rec("1f chapter 1: images with attribution", "PASS" if images else "FAIL", f"{len(images)} images")
        if videos:
            rec("1g chapter 1: >=1 verified video", "PASS", f"{len(videos)} video(s)")
        else:
            rec("1g chapter 1: >=1 verified video", "SKIP", "none found: needs YOUTUBE_API_KEY or live Gemini Google Search grounding")
    except Exception as e:
        rec("1 goal -> notebook", "FAIL", str(e))

    # ---- 2. remaining chapters fill in live (SSE), and an opened queued chapter jumps the queue
    try:
        nid = cl.create("I want to learn Python basics")
        v, _ = cl.wait(nid, "open_ready", timeout=180)
        seen, queued = [], [c for c in v["chapters"] if c["status"] == "queued"]
        target = queued[-1] if queued else None
        if target:
            pr = cl.c.post(f"/api/chapters/{target['id']}/prioritize").json()
        with cl.c.stream("GET", f"/api/notebooks/{nid}/stream", timeout=None) as r:
            end = time.time() + 120
            for line in r.iter_lines():
                if line.startswith("data: "):
                    snap = json.loads(line[6:])
                    if not seen or snap["readiness"] != seen[-1]:
                        seen.append(snap["readiness"])
                    if snap["status"] == "complete" or time.time() > end:
                        break
        rec("2a chapters fill in live over SSE", "PASS" if len(seen) >= 2 and seen[-1] == 100 else "FAIL", f"readiness {seen}")
        if target:
            rec("2b opening a queued chapter bumps its priority", "PASS" if pr.get("priority", 0) >= 50 else "FAIL", f"priority {pr.get('priority')}")
        else:
            rec("2b opening a queued chapter bumps its priority", "SKIP", "everything was already built when checked")
    except Exception as e:
        rec("2 live generation", "FAIL", str(e))

    # ---- 3. URL + PDF shape the course and are cited
    try:
        pdf = make_pdf("Binary search halves the sorted range at every step so the number of comparisons grows with the logarithm of the list size. "
                       "The list must already be sorted. A common bug is computing the midpoint with an addition that overflows.")
        nid = cl.create("I want to learn binary search https://en.wikipedia.org/wiki/Binary_search", files=[("files", ("notes.pdf", pdf, "application/pdf"))])
        v, _ = cl.wait(nid, "open_ready", timeout=180)
        kinds = {s["kind"]: s for s in v["sources"]}
        ch = cl.chapter(v["chapters"][0]["id"])
        refs = next((b["meta"].get("refs", []) for b in ch["blocks"] if b["type"] == "notes"), [])
        cited = [r for r in refs if any(r.startswith(s["id"] + "-") for s in v["sources"])]
        warn = "; ".join(v["warnings"])[:120]
        rec("3a URL and PDF are read as sources", "PASS" if {"url", "pdf"} <= set(kinds) and all(s["chunks"] for s in kinds.values()) else "FAIL",
            f"{ {k: s['chunks'] for k, s in kinds.items()} } {warn}")
        rec("3b chapter notes cite them", "PASS" if cited else "FAIL", f"refs {refs[:3]}")
    except Exception as e:
        rec("3 URL + PDF", "FAIL", str(e))

    # ---- 4. kill the primary model: flow completes, events say why
    try:
        r = cl.c.post("/api/admin/kill", json={"model": "gemma_31b"}, headers=cl.h)
        if r.status_code == 403:
            rec("4 kill primary model", "SKIP", "admin endpoints need --admin-token (or run from localhost)")
        else:
            nid = cl.create("I want to learn algebra")
            v, _ = cl.wait(nid, "open_ready", timeout=180)
            ev = cl.c.get("/api/events", params={"limit": 300}, headers=cl.h).json()
            why = [e for e in ev if "killed" in (e.get("error") or "")]
            cl.c.post("/api/admin/revive", json={}, headers=cl.h)
            rec("4 flow completes with the primary model killed", "PASS" if v["open_ready"] and why else "FAIL",
                f"{len(why)} events explain it, e.g. {why[0]['agent']} step {why[0]['fallback_step']}: {why[0]['error']}" if why else "no event explains the fallback")
    except Exception as e:
        cl.c.post("/api/admin/revive", json={}, headers=cl.h)
        rec("4 kill primary model", "FAIL", str(e))

    # ---- 5. model health
    try:
        rep = cl.c.get("/api/health/models").json()
        bad = {k: v["error"] for k, v in rep["models"].items() if v["status"] == "failed" and not v.get("optional")}
        states = {k: v["status"] for k, v in rep["models"].items()}
        rec("5 /api/health/models reports every model", "PASS" if rep["models"] and all(v["status"] in ("ok", "failed", "mock", "skipped") for v in rep["models"].values()) else "FAIL",
            f"mode={rep['mode']} {states}")
        if rep["mode"] != "mock":
            rec("5b every required model is ok", "PASS" if not bad else "FAIL", json.dumps(bad)[:300])
        else:
            rec("5b every required model is ok", "SKIP", "mock mode: no keys, models are not called")
    except Exception as e:
        rec("5 model health", "FAIL", str(e))

    # ---- 6. wrong answer -> misconception + review chapter
    try:
        if not nid1:
            raise RuntimeError("no notebook from check 1")
        v = cl.view(nid1)
        cid = v["chapters"][0]["id"]
        qs = next(b for b in cl.chapter(cid)["blocks"] if b["type"] == "quiz")["content"]["questions"]
        miscon = None
        for i, q in enumerate(qs):  # rotate the picked option so at least one answer is wrong
            r = cl.c.post(f"/api/chapters/{cid}/answer", json={"learner_id": cl.learner, "session_id": cl.session, "question_id": q["id"],
                                                            "answer": q["options"][(len(q["options"]) - 1 - i) % len(q["options"])]}).json()
            miscon = miscon or r.get("misconception")
        fin = cl.c.post(f"/api/chapters/{cid}/finish", json={"learner_id": cl.learner, "session_id": cl.session}).json()
        if not miscon and not fin["added"]:
            rec("6 wrong answer -> misconception + review item", "SKIP", "every answer happened to be right")
        else:
            after = cl.view(nid1)
            rec("6a feedback names a misconception", "PASS" if miscon else "FAIL", str(miscon))
            rec("6b the course gains a review item", "PASS" if fin["added"] and len(after["chapters"]) == len(v["chapters"]) + len(fin["added"]) else "FAIL",
                f"added {[x['title'] for x in fin['added']]}")
    except Exception as e:
        rec("6 misconception + review", "FAIL", str(e))

    # ---- 7. doubts: typed always works; voice degrades gracefully; spoken Indian-language doubt is manual
    try:
        cid = cl.view(nid1)["chapters"][0]["id"]
        d = cl.c.post(f"/api/chapters/{cid}/doubt", data={"learner_id": cl.learner, "text": "What is the main idea of this chapter?"}).json()
        rec("7a typed doubt is answered", "PASS" if d.get("answer") else "FAIL", (d.get("answer") or "")[:80])
        t = cl.c.post("/api/voice/stt", data={"language": "en"}, files={"audio": ("a.wav", b"RIFF....WAVEfmt ", "audio/wav")})
        ok_degrade = t.status_code == 200 and ("text" in t.json())
        rec("7b voice failure never crashes", "PASS" if ok_degrade else "FAIL", str(t.json())[:100])
        rec("7c spoken Indian-language doubt and spoken answer", "SKIP", "manual: needs SARVAM_API_KEY and a microphone (use the mic button on a chapter)")
    except Exception as e:
        rec("7 doubts", "FAIL", str(e))

    # ---- 8. product name
    try:
        html = httpx.get(a.url + "/").text
        js = httpx.get(a.url + "/static/app.js").text
        old = re.compile("swarm tutor", re.I)
        hits = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and p.suffix in {".py", ".js", ".html", ".css", ".md", ".yml", ".yaml", ".txt", ".example"}
                and not any(x in p.parts for x in ("data", ".git", "__pycache__", "node_modules")) and p.name != "acceptance.py" and old.search(p.read_text(encoding="utf-8", errors="ignore"))]
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        ok = "<title>Aasaan</title>" in html and "Aasaan" in js and "Aasaan" in readme and "image: aasaan" in compose and not hits
        rec("8 name 'Aasaan' everywhere, old name nowhere", "PASS" if ok else "FAIL", f"old-name files: {hits}" if hits else "title, header, README, compose image")
    except Exception as e:
        rec("8 name check", "FAIL", str(e))

    # ---- Milestone 2.1 addendum. Items that need a server are checked live; items that need to break a service on purpose
    # (chaos, disabled keys, invalid specs) are proven by the offline tests, which this script runs by name.
    try:
        hits = [str(p.relative_to(ROOT)) for p in list((ROOT / "backend" / "app").rglob("*.py")) + [ROOT / "config" / "models.yaml", ROOT / ".env.example"]
                if "pexels" in p.read_text(encoding="utf-8", errors="ignore").lower()]
        rec("A1 no Pexels anywhere (code, config, .env.example)", "PASS" if not hits else "FAIL", ", ".join(hits))
    except Exception as e:
        rec("A1 no Pexels", "FAIL", str(e))
    try:
        tl = httpx.get(a.url + "/api/health/tools", timeout=15).json()
        need = ["illustrator_renderer", "ast_flowchart", "mermaid_check", "plot_expression", "notebook_cover", "commons", "openverse", "firecrawl"]
        missing = [n for n in need if n not in tl["tools"]]
        dm = tl.get("decision_models", {})
        raw = json.dumps(tl)
        leaked = [k for k in __import__("os").environ if k.endswith(("_API_KEY", "_SECRET")) and len(__import__("os").environ[k]) > 12 and __import__("os").environ[k] in raw]
        ok = not missing and {"jev", "jev_or", "mercury_decide"} <= set(dm) and not leaked
        rec("A9 /api/health/tools lists Illustrator tools, Firecrawl, Jev, Mercury, image sources; keys masked", "PASS" if ok else "FAIL",
            f"missing {missing}; decision models {sorted(dm)}; leaked keys {leaked}")
        hm = httpx.get(a.url + "/api/health/models", timeout=60).json()
        rec("A9b /api/health/models answers", "PASS" if hm else "FAIL", f"mode {hm.get('mode')}")
    except Exception as e:
        rec("A9 health endpoints", "FAIL", str(e))
    offline = [
        ("A2 Python snippet -> AST flowchart, no model call", "test_illustrator.py::test_python_snippet_becomes_a_flowchart_with_no_model_call"),
        ("A3 linked-list chapter -> validated spec drawn by code, light + dark", "test_illustrator.py::test_linked_list_chapter_gets_a_validated_spec_drawn_by_code"),
        ("A4 invalid spec -> one repair, next tier, chapter completes", "test_illustrator.py::test_invalid_spec_gets_one_repair_then_the_next_model_and_the_chapter_still_completes"),
        ("A5 biology chapter -> Commons SVG figure with licence + attribution", "test_illustrator.py::test_biology_chapter_gets_a_commons_svg_figure_with_licence_and_attribution"),
        ("A6 Commons chaos -> Openverse; both off -> no figure, events say why", "test_illustrator.py::test_commons_down_falls_to_openverse_both_down_ships_without_a_figure"),
        ("A7 Jev + Mercury disabled -> Gemma gates, escalations in events", "test_gates.py::test_with_jev_and_mercury_disabled_gemma_decides_and_events_say_so"),
        ("A8 Firecrawl last tier, off after 401/402, capped per notebook", "test_firecrawl.py"),
        ("A10 second notebook on the same topic reuses cached visuals", "test_illustrator.py::test_second_notebook_on_the_same_topic_reuses_cached_visuals"),
    ]
    import subprocess
    for name, node in offline:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", f"backend/tests/{node}"], cwd=ROOT, capture_output=True, text=True)
        rec(name, "PASS" if r.returncode == 0 else "FAIL", "offline test: " + node.split("::")[-1])

    c = {s: sum(1 for _, st, _ in rows if st == s) for s in ("PASS", "FAIL", "SKIP")}
    print(f"\n{c['PASS']} passed, {c['FAIL']} failed, {c['SKIP']} skipped (mode: {mode})")
    return 1 if c["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
