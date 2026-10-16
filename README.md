# Aasaan: planning pack

Draft documents for the Hacktoberfest Hack Day Bengaluru 2026 build.

```
PRD.md                       Product requirements (start here)
architecture/                Diagrams (SVG for editing, PNG for slides)
  01-system-overview
  02-learning-loop
  03-model-routing
agents/                      One spec per agent, plus shared contracts
  README.md
  01-intake.md ... 08-companion.md
config/models.yaml           Per-agent models and fallback chains (placeholders)
```

Code lives in `backend/` (API, router, agents, tests) and `frontend/`. See `build-log/README.md` for what is built and how to run it.

Reading order: PRD, architecture images, agents/README, then each agent.

Status: UX is intentionally minimal and the swarm view is out of scope for v1.
Model ids, partner access (Jev, Sarvam) and rate limits must be verified before building.
