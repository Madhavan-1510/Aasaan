# 03 Teacher

**Job:** Write a short lesson for the current node, in the student's language, using only the student's material.

## Inputs
Path node, relevant source chunks, learner profile, and (on revisions) the Critic's issues.

## Output (JSON)
```json
{
  "node_id": "n3",
  "language": "kn",
  "lesson": {
    "title": "Speed and velocity",
    "steps": [
      {
        "type": "explain",
        "text": "..."
      },
      {
        "type": "example",
        "text": "..."
      },
      {
        "type": "check",
        "text": "Quick question to think about"
      }
    ],
    "key_points": [
      "..."
    ],
    "source_refs": [
      {
        "source_id": "s1",
        "page": 12,
        "chunk_id": "s1-p12-c3"
      }
    ]
  }
}
```

## Model and fallbacks
Primary: gemma_big. Fallback 1: gemma_small. Last resort: a cached lesson for the demo chapter (pre-written, pre-checked).

Chains are defined in `config/models.yaml` under role `teacher`.

## Prompt outline
Teach one idea at a time in 3 to 5 short steps. Use the student's own examples and wording where possible. Use plain language suited to the grade. Never state a fact that is not supported by the source chunks. End with a thinking prompt, not the answer. On a revision, fix only the issues the Critic listed.

## Handoffs
Sends the draft to Critic. If Critic returns `revise`, revise and resend (max 2 loops). After approval the lesson goes to the student and to Quizzer.

## Validation (checked in code)
Between 3 and 5 steps. Every step with a factual claim has a source ref. Language matches the profile. Length within a cap.

## Failure modes
Over-long lessons, unsupported claims, drifting into other topics, wrong language register for a school student.

## How to test
Run each node of two chapters. Have a teacher or friend rate clarity 1 to 5. Check source refs resolve.

## Open items
- Confirm exact model ids and rate limits.
- Tune the output length cap after the first real runs.
