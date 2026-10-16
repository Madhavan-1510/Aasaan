# 02 Architect

**Job:** Build a short learning path from the topics and the learner profile.

## Inputs
Intake output, learner profile (grade, goal, mistake labels, time available).

## Output (JSON)
```json
{
  "path_id": "p1",
  "start_node": "n1",
  "nodes": [
    {
      "id": "n1",
      "title": "What is motion",
      "prereqs": [],
      "est_minutes": 6,
      "source_refs": [
        {
          "source_id": "s1",
          "page": 10,
          "chunk_id": "s1-p10-c1"
        }
      ]
    }
  ],
  "edges": [
    [
      "n1",
      "n2"
    ]
  ]
}
```

## Model and fallbacks
Primary: gemma_big. Fallback 1: gemma_small. Last resort: linear path in topic order, one node per topic.

Chains are defined in `config/models.yaml` under role `architect`.

## Prompt outline
Order topics so prerequisites come first. At most 8 nodes. Each node must map to source refs. If the learner has mistake labels, add a short review node near the start for any related concept. Fit total time to the goal if given.

## Handoffs
Sends the path to Teacher (start node). The Rewriter later edits this path using patches.

## Validation (checked in code)
At most 8 nodes. Graph is acyclic (check in code). Every prereq id exists. Every node has at least one source ref.

## Failure modes
Cycles, orphan nodes, paths too long for the stated goal.

## How to test
Run 5 sample chapters; check acyclic graph and sensible order by eye. Seed a mistake label and confirm a review node appears.

## Open items
- Confirm exact model ids and rate limits.
- Tune the output length cap after the first real runs.
