"""PDF pages with no text layer: scans go to vision OCR, drawing-only pages get one probe per kind, nothing is silently dropped."""
import pytest

from app.agents import intake


@pytest.fixture
def renderer(monkeypatch):
    calls = []
    monkeypatch.setattr(intake, "render_page_png", lambda data, page, scale=1.6: calls.append(page) or b"png")
    return calls


def empties(*specs):
    return [{"page": p, "images": im, "fonts": 0, "content_bytes": cb} for p, im, cb in specs]


def ocr_calls(router):
    return [c for c in router.mock.calls if c[0] == "ocr"]


def test_scanned_pages_are_read_with_vision(engine, renderer):
    pages = ["Real text on page one about velocity and speed in motion.", "", ""]
    out, meta = engine._recover_pages(b"x", pages, empties((2, 1, 5000), (3, 1, 5000)), "en", "scan.pdf")
    assert "Sample transcription" in out[1] and "Sample transcription" in out[2]
    assert meta["recovered_with_vision"] == [2, 3] and meta["status"] == "ready" and meta["issues"] == []
    assert any("vision model" in e["summary"] for e in engine.store.events())


def test_a_run_of_empty_table_grids_costs_one_probe_not_one_call_per_page(engine, renderer):
    engine.router.mock.script("ocr", [{"text": "", "empty": True}])
    pages = ["Some real text about cloud data engineering topics."] + [""] * 66
    out, meta = engine._recover_pages(b"x", pages, empties(*[(p, 0, 3247) for p in range(2, 68)]), "en", "TOC.pdf")
    assert len(ocr_calls(engine.router)) == 1 and renderer == [2]
    assert meta["status"] == "partly_readable" and meta["pages_read"] == 1 and meta["pages_total"] == 67
    issue = meta["issues"][0]
    assert issue["kind"] == "drawing_only" and len(issue["pages"]) == 66 and issue["checked_with_vision"] and "table grid" in issue["reason"]


def test_a_probe_that_finds_text_reads_the_whole_run(engine, renderer):
    pages = [""] * 4
    out, meta = engine._recover_pages(b"x", pages, empties(*[(p, 0, 3000) for p in range(1, 5)]), "en", "outlined.pdf")
    assert all("Sample transcription" in t for t in out) and meta["status"] == "ready"


def test_pages_with_nothing_on_them_need_no_model(engine, renderer):
    out, meta = engine._recover_pages(b"x", ["Text on the first page of this file."] + [""] * 3, empties((2, 0, 0), (3, 0, 0), (4, 0, 0)), "en", "blank.pdf")
    assert ocr_calls(engine.router) == [] and meta["issues"][0]["kind"] == "blank" and meta["issues"][0]["pages"] == [2, 3, 4]


def test_vision_failure_is_reported_not_hidden(engine, renderer, monkeypatch):
    monkeypatch.setattr(intake, "ocr_page", lambda *a, **k: {"text": "", "empty": False, "failed": True})
    out, meta = engine._recover_pages(b"x", ["", "Text here is on page two of the file."], empties((1, 1, 9000)), "en", "bad.pdf")
    assert meta["issues"][0]["kind"] == "failed" and not meta["issues"][0]["checked_with_vision"] and "clearer" in meta["issues"][0]["reason"]


def test_no_renderer_installed_degrades_to_a_clear_reason(engine, monkeypatch):
    monkeypatch.setattr(intake, "render_page_png", lambda *a, **k: None)
    _, meta = engine._recover_pages(b"x", ["", "Real text is on the second page here."], empties((1, 1, 9000)), "en", "x.pdf")
    assert meta["issues"][0]["kind"] == "failed"


def test_vision_budget_is_capped(engine, renderer):
    n = intake.OCR_MAX_PAGES + 5
    _, meta = engine._recover_pages(b"x", [""] * n, empties(*[(p, 1, 9000) for p in range(1, n + 1)]), "en", "big.pdf")
    assert len(ocr_calls(engine.router)) == intake.OCR_MAX_PAGES and meta["issues"][0]["kind"] == "failed" and len(meta["issues"][0]["pages"]) == 5


def test_notebook_view_reports_issues_per_file_not_as_header_warnings(engine, worker, store, monkeypatch, sample_text, renderer):
    engine.router.mock.script("ocr", [{"text": "", "empty": True}])
    monkeypatch.setattr(intake, "analyze_pdf", lambda data: ([sample_text, "", "", ""], empties((2, 0, 3000), (3, 0, 3000), (4, 0, 3000))))
    nid = engine.create_notebook("L1", "", "en", [("toc.pdf", "application/pdf", b"%PDF-1.4 fake")])
    worker.run_until_idle()
    v = engine.notebook_view(nid)
    assert not any("unreadable or empty" in w for w in v["warnings"])
    assert v["source_issues"][0]["title"] == "toc.pdf" and v["source_issues"][0]["status"] == "partly_readable"
    assert v["source_issues"][0]["issues"][0]["pages"] == [2, 3, 4] and v["sources"][0]["status"] == "partly_readable"


def test_pages_beyond_the_read_limit_are_reported(engine, worker, store, monkeypatch, sample_text):
    monkeypatch.setattr(intake, "analyze_pdf", lambda data: ([sample_text, sample_text], []))
    monkeypatch.setattr(intake, "pdf_page_count", lambda data: 75)
    nid = engine.create_notebook("L1", "", "en", [("big.pdf", "application/pdf", b"%PDF-1.4 fake")])
    worker.run_until_idle()
    issue = engine.notebook_view(nid)["source_issues"][0]
    assert issue["pages_total"] == 75 and issue["issues"][-1]["kind"] == "over_limit" and issue["issues"][-1]["pages"][0] == 3
