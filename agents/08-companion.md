# 08 Companion

**Job:** Remember the student and open and close each session.

## Inputs
Memory (profile, mistake labels, path history), last session summary.

## Output (JSON)
```json
{
  "message": "Short greeting or recap in the student's language",
  "suggested_next": "n4",
  "memory_updates": {
    "mistake_labels_add": [
      "confuses speed and velocity"
    ],
    "last_node": "n3",
    "notes": "Prefers examples from daily life"
  }
}
```

## Model and fallbacks
Primary: gemma_small. Fallback 1: gemma_big. Last resort: template message such as 'Welcome back. Let's continue with {next_node}.'

Chains are defined in `config/models.yaml` under role `companion`.

## Prompt outline
Be warm, brief and specific. At session start, mention one thing the student did well and one thing to revisit, using stored mistake labels. At session end, summarise in two sentences. Never invent history that is not in memory. Do not collect personal details.

## Handoffs
Reads and writes Memory. At session start passes `suggested_next` to the orchestrator. In v1 there are no reminders or notifications.

## Validation (checked in code)
Message under a length cap. `suggested_next` is a valid node id. Memory updates contain only allowed fields.

## Failure modes
Made-up memories, over-chatty messages, repeating the same greeting.

## How to test
Run two simulated sessions. Confirm session 2 references a mistake from session 1 and nothing else.

## Open items
- Confirm exact model ids and rate limits.
- Tune the output length cap after the first real runs.
