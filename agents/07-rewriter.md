# 07 Rewriter

**Job:** Change the learning path based on what the student got wrong.

## Inputs
Current path, Diagnoser results for this session, mistake memory.

## Output (JSON)
```json
{
  "path_id": "p1",
  "patch": {
    "insert": [
      {
        "after": "n3",
        "node": {
          "id": "n3r",
          "title": "Review: speed vs velocity",
          "prereqs": [
            "n3"
          ],
          "est_minutes": 5,
          "source_refs": [
            {
              "source_id": "s1",
              "page": 12,
              "chunk_id": "s1-p12-c3"
            }
          ]
        }
      }
    ],
    "reorder": [],
    "skip": []
  },
  "reason": "Student confused speed and velocity twice"
}
```

## Model and fallbacks
Primary: gemma_small. Fallback 1: gemma_big. Last resort: insert one review node for the most frequent mistake.

Chains are defined in `config/models.yaml` under role `rewriter`.

## Prompt outline
Propose the smallest change that fixes the student's gap. Insert at most 2 review nodes per session. Only reorder or skip when the student clearly knows a topic. Explain the reason in one sentence.

## Handoffs
Applies the patch to the path (in code), writes the reason to Memory, and hands control to Teacher for the next node.

## Validation (checked in code)
Patched path stays acyclic. At most 2 inserted nodes. New nodes cite source refs. Skipped nodes are not prerequisites of remaining nodes.

## Failure modes
Over-editing the path, endless review loops, breaking prerequisites.

## How to test
Replay 5 mistake scenarios and check the patch by hand. Confirm the path never grows beyond 10 nodes.

## Open items
- Confirm exact model ids and rate limits.
- Tune the output length cap after the first real runs.
