"""01 Intake: student material -> referenced chunks + topics (agents/01-intake.md).

Text and PDF extraction happens in code. The model groups text into topics (and
transcribes photos). Chunks produced by code are authoritative for text input.
"""
from __future__ import annotations

import io
import re
from typing import Optional

from ..router import ModelRouter, RouterResult
from ..schemas import IntakeOut
from .common import build_prompt, lang_name

MAX_PAGES = 20           # v1 cap (spec: "about 20 pages")
CHUNK_CHARS = 600
PAGE_CHARS = 1800


def pages_from_text(text: str) -> list[str]:
    """Pasted text -> pseudo pages. Form-feed splits pages; otherwise ~1800-char pages on paragraph breaks."""
    text = text.replace("\r\n", "\n").strip()
    if "\f" in text:
        pages = [p.strip() for p in text.split("\f") if p.strip()]
    else:
        pages, cur = [], ""
        for para in re.split(r"\n\s*\n", text):
            if cur and len(cur) + len(para) > PAGE_CHARS:
                pages.append(cur.strip())
                cur = ""
            cur += para + "\n\n"
        if cur.strip():
            pages.append(cur.strip())
    return pages[:MAX_PAGES]


def pages_from_pdf(data: bytes) -> list[str]:
    return analyze_pdf(data)[0]


# --------------------------------------------------------------------------- PDF pages that have no text
OCR_MAX_PAGES = 12          # vision calls per PDF, at most
VECTOR_TEXT_BYTES = 20_000  # a page with no fonts but this much drawing could be text drawn as outlines


def analyze_pdf(data: bytes) -> tuple[list[str], list[dict]]:
    """(page texts, facts about each page with no text). The facts say WHY a page has no text (images only = a scan; drawing only
    = grid, diagram or outlined text; nothing at all = blank), so only pages that could hold text reach a vision model."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    texts, empties = [], []
    for n, p in enumerate(reader.pages[:MAX_PAGES], start=1):
        t = (p.extract_text() or "").strip()
        texts.append(t)
        if t:
            continue
        try:
            images = len(list(p.images))
        except Exception:  # noqa: BLE001  odd image objects must not stop reading
            images = 0
        res = p.get("/Resources") or {}
        fonts = len((res.get("/Font") or {}) if hasattr(res, "get") else {})
        size = 0
        try:
            c = p.get("/Contents")
            obj = c.get_object() if c is not None else None
            parts = obj if isinstance(obj, list) else [obj] if obj is not None else []
            size = sum(len(x.get_object().get_data()) for x in parts)
        except Exception:  # noqa: BLE001
            pass
        empties.append({"page": n, "images": images, "fonts": fonts, "content_bytes": size})
    return texts, empties


def pdf_page_count(data: bytes) -> int:
    from pypdf import PdfReader
    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:  # noqa: BLE001  an unreadable file is reported elsewhere
        return 0


def render_page_png(data: bytes, page: int, scale: float = 1.6) -> Optional[bytes]:
    """The page as a PNG for a vision model (pypdfium2). None when no renderer is installed."""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        img = pdf[page - 1].render(scale=scale).to_pil()
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "PNG")
        return buf.getvalue()
    except Exception:  # noqa: BLE001  no renderer (or Pillow) installed, or a damaged page: callers report "couldn't read", never crash
        return None


def ocr_page(router: ModelRouter, png: bytes, language: str, label: str) -> dict:
    """{"text", "empty", "failed"}: ask a vision model to transcribe one rendered page. Says "empty" for a page with no text."""
    def validate(raw: dict) -> dict:
        text = str(raw.get("text") or "").strip()
        if raw.get("empty") is True and not text:
            return {"text": "", "empty": True, "failed": False}
        if not text:
            raise ValueError("no text and not marked empty")
        return {"text": text[:6000], "empty": False, "failed": False}

    prompt = build_prompt(
        "You are Reader. Transcribe ALL text visible on this page image faithfully, in reading order; do not correct, translate or add anything. "
        "If the page has no readable text (blank, an empty table grid, only lines or shapes), reply with empty=true and text=\"\".",
        {"text": "the transcribed text", "empty": False}, {"language_hint": lang_name(language), "page": label})
    res = router.call("ocr", prompt, {"page": label}, validate, last_resort=lambda c: {"text": "", "empty": False, "failed": True},
                      images=[("image/png", png)], summary=f"OCR {label}", language=None)
    return res.value


def chunk_pages(source_id: str, pages: list[str], first_page: int = 1) -> tuple[list[dict], list[str]]:
    """Split pages into chunks with ids like s1-p12-c3. Returns (chunks, warnings)."""
    chunks, warnings = [], []
    for i, page_text in enumerate(pages):
        page_no = first_page + i
        if not page_text.strip():
            warnings.append(f"page {page_no} unreadable or empty")
            continue
        n, cur = 0, ""
        pieces = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", page_text) if p.strip()]
        for piece in pieces + [None]:
            if piece is None or (cur and len(cur) + len(piece) > CHUNK_CHARS):
                if cur.strip():
                    n += 1
                    chunks.append({"chunk_id": f"{source_id}-p{page_no}-c{n}", "page": page_no, "text": cur.strip()})
                cur = ""
            if piece:
                cur += piece + " "
    return chunks, warnings


def _text_only(ctx: dict) -> IntakeOut:
    """Last resort: no topic structure; one topic per page (grouped when >8 pages)."""
    chunks = ctx["chunks"]
    if not chunks:
        raise ValueError("no readable text")
    pages = sorted({c["page"] for c in chunks})
    group = -(-len(pages) // 8)  # ceil
    topics = []
    for i in range(0, len(pages), group):
        grp = pages[i : i + group]
        first = next(c["text"] for c in chunks if c["page"] == grp[0])
        title = " ".join(first.split()[:6]).rstrip(".,;:") or f"Page {grp[0]}"
        topics.append({"id": f"t{len(topics) + 1}", "title": title, "summary": first[:160], "page_refs": grp})
    return IntakeOut(source_id=ctx["source_id"], language=ctx["language"], topics=topics, chunks=chunks,
                     warnings=ctx.get("warnings", []))


def run(
    router: ModelRouter,
    source_id: str,
    pages: list[str],
    images: Optional[list[tuple[str, bytes]]],
    language: str,
    goal: Optional[str] = None,
) -> RouterResult:
    chunks, warnings = chunk_pages(source_id, pages) if pages else ([], [])
    ctx = {"source_id": source_id, "language": language, "chunks": chunks, "warnings": warnings}

    def validate(raw: dict) -> IntakeOut:
        # Code-made chunks win for text input; for photos the model's transcription is used.
        merged = {
            "source_id": source_id, "language": raw.get("language", language),
            "topics": raw.get("topics", []),
            "chunks": chunks or raw.get("chunks", []),
            "warnings": warnings + list(raw.get("warnings", [])),
        }
        return IntakeOut.model_validate(merged)

    example = {
        "topics": [{"id": "t1", "title": "Speed and velocity", "summary": "One or two sentences", "page_refs": [12]}],
        "chunks": [{"chunk_id": f"{source_id}-p1-c1", "page": 1, "text": "(only when transcribing photos)"}],
        "warnings": ["page 14 blurry"],
    }
    prompt = build_prompt(
        "You are Intake. Read the student's material. Transcribe faithfully; do not correct or add facts. "
        "Group the text into at most 8 topics, each with page_refs that exist. Report unreadable regions in "
        "warnings. For photos, also return chunks (chunk_id like sX-pN-cM, page, text). Describe diagrams "
        "briefly and mark them kind=figure.",
        example,
        {"source_id": source_id, "student_language": lang_name(language), "goal": goal,
         "chunks": chunks if chunks else "(see attached images)"},
    )
    return router.call("intake", prompt, ctx, validate, last_resort=_text_only, images=images,
                       summary=f"Intake {source_id}: {len(chunks)} chunks")
