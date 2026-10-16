# 05 Quizzer

**Job:** Write 3 to 5 questions for the current node with answer keys and concept tags.

## Inputs
Approved lesson, source chunks, learner profile (level, mistake labels).

## Output (JSON)
```json
{
  "node_id": "n3",
  "questions": [
    {
      "id": "q1",
      "type": "mcq",
      "text": "...",
      "options": [
        "A",
        "B",
        "C",
        "D"
      ],
      "answer_key": "B",
      "concept": "speed vs velocity",
      "hint": "Think about direction",
      "source_ref": {
        "source_id": "s1",
        "page": 12,
        "chunk_id": "s1-p12-c3"
      }
    }
  ]
}
```

## Model and fallbacks
Primary: gemma_small. Fallback 1: gemma_big. Last resort: cached questions for the demo chapter.

Chains are defined in `config/models.yaml` under role `quizzer`.

## Prompt outline
Write questions that test understanding, not recall of wording. Mix easy and medium. Include one question aimed at a known mistake label if present. Each question needs one correct answer, a concept tag and a gentle hint that does not give the answer away.

## Handoffs
Sends questions to the student one at a time. Each answer goes to Diagnoser with the question, key and concept.

## Validation (checked in code)
3 to 5 questions. MCQs have exactly one correct option that appears in the options. Every question has a concept tag and a hint. Answer keys are consistent with the lesson.

## Failure modes
Ambiguous options, two correct answers, answers that contradict the source.

## How to test
Generate for 5 nodes and manually check keys. Run questions through the Critic's rules as a second check.

## Open items
- Confirm exact model ids and rate limits.
- Tune the output length cap after the first real runs.
