"""Helpers shared by all agents."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

LANG_NAMES = {"en": "English", "hi": "Hindi", "kn": "Kannada", "ta": "Tamil"}

RULES = (
    "Rules: reply with ONLY one JSON object, no prose, no markdown fences. "
    "Use only facts from the student's material. Internal field names stay in English. "
    "Keep outputs short."
)


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code)


def build_prompt(job: str, schema_example: dict, inputs: dict[str, Any], extra: str = "") -> str:
    return (
        f"{job}\n\n{RULES}\n{extra}\n\n"
        f"Output JSON schema (example):\n{json.dumps(schema_example, ensure_ascii=False)}\n\n"
        f"Inputs:\n{json.dumps(inputs, ensure_ascii=False)}"
    )


def split_sentences(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]


def ref_of(chunk: dict) -> dict:
    return {"source_id": chunk["chunk_id"].split("-")[0], "page": chunk["page"], "chunk_id": chunk["chunk_id"]}


def chunk_ids(chunks: list[dict]) -> set[str]:
    return {c["chunk_id"] for c in chunks}


def fingerprint(chunks: list[dict]) -> str:
    """Content hash of the source text a node is taught from. Cache keys use this (not the random
    source id) so a pre-warmed demo cache still hits when the same material is uploaded again."""
    h = hashlib.sha1("\n".join(c["text"].strip() for c in chunks).encode("utf-8")).hexdigest()
    return h[:16]
