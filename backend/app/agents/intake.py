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
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return [(p.extract_text() or "").strip() for p in reader.pages[:MAX_PAGES]]


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
