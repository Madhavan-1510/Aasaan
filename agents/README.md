# Agents: shared contracts

Eight agents, one model router, one orchestrator. Read this file first, then each agent spec.

| # | Agent | One-line job |
|---|---|---|
| 1 | Intake | Read the student's material and extract topics with page references |
| 2 | Architect | Turn topics into a short learning path |
| 3 | Teacher | Write the lesson for the current step |
| 4 | Critic | Check the lesson against the source and approve or send back |
| 5 | Quizzer | Write questions for the step |
| 6 | Diagnoser | Grade answers and name the misconception |
| 7 | Rewriter | Change the path based on mistakes |
| 8 | Companion | Remember the student and open and close sessions |

Voice (speech to text, text to speech, translation) is a service behind the router, not an agent.

## Rules every agent follows

1. Ask the router for a role. Never call a model directly. Chains live in `config/models.yaml`.
2. Return JSON that matches the schema in the agent's spec. The router validates it. Invalid output counts as a failure and triggers the next fallback.
3. Use only the student's material for facts. Every factual statement carries a `source_ref`.
4. Respond in the student's language where the output is shown to the student. Internal JSON stays in English.
5. Keep outputs short. Small outputs are faster, cheaper and easier to validate.
6. Never reveal full answers before offering a hint (Teacher, Quizzer, Companion).
7. Emit one event per call (see below), even on failure.

## Shared types

```json
{
  "source_ref": {"source_id": "s1", "page": 12, "chunk_id": "s1-p12-c3"},
  "learner_profile": {
    "learner_id": "local-uuid",
    "grade": 9,
    "language": "kn",
    "goal": "exam on Friday",
    "mistake_labels": ["confuses speed and velocity"]
  },
  "path_node": {
    "id": "n3",
    "title": "Speed and velocity",
    "prereqs": ["n1", "n2"],
    "est_minutes": 8,
    "source_refs": [{"source_id": "s1", "page": 12, "chunk_id": "s1-p12-c3"}]
  }
}
```

## Event record (logged by the router, not shown in the UI)

```json
{
  "run_id": "uuid",
  "ts": "ISO-8601",
  "agent": "teacher",
  "model_used": "gemma_big",
  "fallback_step": 0,
  "status": "ok | retry | fallback | failed",
  "latency_ms": 5400,
  "validation": "pass | fail",
  "summary": "Lesson for n3, 4 steps"
}
```

These events make a future swarm view cheap to build.

## Handoffs at a glance

```
Intake -> Architect -> Teacher <-> Critic -> Quizzer -> (student) -> Diagnoser -> Rewriter -> Teacher
                                                                    Diagnoser -> Memory
Companion reads and writes Memory at session start and end.
```

## Common failure handling

| Situation | Behaviour |
|---|---|
| Model timeout or rate limit | Next model in chain |
| Invalid JSON | One repair retry on the same model, then next model |
| All models fail | Run the non-model last resort named in the spec; log `failed` |
| Source missing or unreadable | Ask the student for a clearer upload; do not invent content |
