# 04 Critic

**Job:** Review the Teacher's lesson against the source text. Approve it, ask for a revision, or reject it.

## Inputs
Lesson draft, the source chunks it cites, learner profile (grade, language).

## Output (JSON)
```json
{
  "verdict": "approve | revise | reject",
  "issues": [
    {
      "step": 2,
      "problem": "Claim not found in source",
      "source_ref": {
        "source_id": "s1",
        "page": 12,
        "chunk_id": "s1-p12-c3"
      }
    }
  ],
  "confidence": 0.85
}
```

## Model and fallbacks
Primary: gemma_big. Fallback 1: gemma_small. Last resort: rule checks only (every claim has a ref, refs exist, length cap, no answer leakage by keyword).

Chains are defined in `config/models.yaml` under role `critic`.

## Prompt outline
You are a strict fact-checker. For each step, find the supporting text in the cited chunks. Flag any claim without support, any error, any answer given too early, and any wording too hard for the grade. Be specific so the Teacher can fix it. Do not rewrite the lesson.

## Handoffs
Returns the verdict to Teacher. After 2 revision loops the orchestrator ships the best version and logs it as `unverified`.

## Validation (checked in code)
Verdict is one of the three values. Each issue names a step and a problem. `reject` requires at least one issue.

## Failure modes
Being too lenient (misses seeded errors) or too strict (never approves). Both are tested.

## How to test
Seed 10 lessons with deliberate errors and 10 correct lessons. Target: catches at least 8 of 10 errors and approves at least 8 of 10 correct ones.

## Open items
- Confirm exact model ids and rate limits.
- Tune the output length cap after the first real runs.
