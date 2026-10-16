# 06 Diagnoser

**Job:** Grade the student's answer and, if wrong, identify the likely misconception.

## Inputs
Question, answer key, concept, student answer, previous mistake labels.

## Output (JSON)
```json
{
  "question_id": "q1",
  "result": "correct | partial | wrong",
  "misconception": "confuses speed and velocity | null",
  "confidence": 0.8,
  "hint": "Short nudge in the student's language"
}
```

## Model and fallbacks
Primary: jev_fast if accessible (fast scoring and choices). Fallback 1: gemma_small. Last resort: exact-match grading in code with no misconception label.

Chains are defined in `config/models.yaml` under role `diagnoser`.

## Prompt outline
Decide whether the answer is correct, partially correct, or wrong. If wrong, choose the closest misconception from the known labels, or propose a new short label. Rate your confidence from 0 to 1. Write a one-line hint that does not reveal the answer.

## Handoffs
Results go to the student (feedback), to Memory (mistake labels), and to Rewriter (after the quiz). Low confidence triggers a follow-up question instead of a verdict.

## Validation (checked in code)
Result is one of three values. Confidence between 0 and 1. A `wrong` result has a misconception label. Numeric and multiple-choice answers are always checked in code first.

## Failure modes
Over-confident labels, penalising correct answers phrased unusually, mislabelling guesses.

## How to test
20 sample answers graded by a teacher. Target: at least 80% agreement. Track how often low-confidence cases occur.

## Open items
- Confirm exact model ids and rate limits.
- Tune the output length cap after the first real runs.
