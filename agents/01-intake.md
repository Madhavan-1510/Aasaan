# 01 Intake

**Job:** Read the student's uploaded material and turn it into clean, referenced text and a list of topics.

## Inputs
One or more files (photo, PDF, pasted text), the student's language, optional goal.

## Output (JSON)
```json
{
  "source_id": "s1",
  "language": "en",
  "topics": [
    {
      "id": "t1",
      "title": "Speed and velocity",
      "summary": "One or two sentences",
      "page_refs": [
        12,
        13
      ]
    }
  ],
  "chunks": [
    {
      "chunk_id": "s1-p12-c3",
      "page": 12,
      "text": "..."
    }
  ],
  "warnings": [
    "page 14 blurry"
  ]
}
```

## Model and fallbacks
Primary: gemma_big (vision). Fallback 1: gemma_small. Last resort: plain text extraction with no topic structure (the Architect then builds a linear path from page order).

Chains are defined in `config/models.yaml` under role `intake`.

## Prompt outline
Read each page. Transcribe faithfully. Do not correct or add facts. Group the text into at most 8 topics. Give each topic page references. Report unreadable regions in `warnings`.

## Handoffs
Sends output to Architect. If `warnings` mark a page unreadable, the orchestrator asks the student to retake that photo.

## Validation (checked in code)
At least one topic. Every topic has at least one page ref that exists in `chunks`. No empty chunks.

## Failure modes
Blurry or handwritten pages, mixed languages, very long PDFs (cap at about 20 pages for v1), diagrams with no text (describe briefly, mark as `figure`).

## How to test
5 sample photos and 2 PDFs with known topics. Check topic recall by hand. Test one deliberately blurry photo.

## Open items
- Confirm exact model ids and rate limits.
- Tune the output length cap after the first real runs.
