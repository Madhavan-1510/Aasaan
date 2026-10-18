# Aasaan

**Learning made aasaan.** (ஆசான், Tamil for "teacher"; in Hindi, "aasaan" means "easy".)

Type a goal ("I want to learn DSA"), or paste notes, links, photos and PDFs into one chat box. Aasaan builds a **notebook**:
a complete course with chapters, notes, verified links, videos, images with attribution, slide cards and quizzes.
The notebook has two views of the same course: a **roadmap** and a **prerequisite graph**. A small swarm of specialised
agents does the work; the student only sees a clean notebook.

## Run it

```bash
pip install -r backend/requirements.txt
python -m uvicorn app.main:create_app --factory --app-dir backend --port 8000     # http://localhost:8000
python -m pytest backend -q                                                       # 99 tests, offline
python scripts/acceptance.py                                                      # the 8 acceptance checks, against the running server
```

- **`GEMINI_API_KEY` in `.env` is required.** Copy `.env.example` and fill it in. Other keys (Sarvam, Jev, YouTube, ...) are optional.
- **Configurable paths:** SQLite path defaults to `data/aasaan.db` (override with `AASAAN_DB`), and uploads default to `data/uploads` (override with `AASAAN_UPLOADS`). Directories are created automatically on startup with write verification.
- **Health endpoints:** `GET /health` provides a lightweight liveness probe (`{"ok": true}`), `GET /health/ready` verifies database writability, and `GET /api/health/models` verifies model connectivity.
- **No key:** the app starts, shows a banner ("No AI model is connected"), and refuses to build notebooks. There is no demo or mock mode.
- **Tests** (`pytest backend/tests`) never call a real model: they inject a test double (`backend/tests/fake_provider.py`).
- **Check your models:** `GET /api/health/models` calls each configured model once and reports `ok` or `failed` with the error.
  This also runs at startup and logs one line per model. Nothing falls back silently: every fallback is recorded with its reason.

## How it works

1. **Outline (seconds):** Intake reads your inputs (text, PDFs, photos, links). Architect plans 8 to 20 chapters with prerequisites (a DAG, validated in code).
2. **Chapter 1 (then the notebook opens):** Curator finds and verifies resources, Teacher writes notes and slide cards, Critic checks them against the sources (max 2 revisions), Quizzer writes the quiz.
3. **Background:** a worker builds the other chapters (2 at a time), stored in SQLite so they resume after a restart. The notebook updates live (server-sent events). Opening a chapter that is not ready moves it to the front of the queue.
4. **Learning loop:** a wrong answer is graded in code, the misconception is named (Jev, then Gemma), and the Rewriter adds a short review chapter. Mistakes are remembered across sessions.

| Agent | Job | Models (primary, fallbacks, last resort) |
|---|---|---|
| Intake | Read text, PDFs, photos, links | Gemma 4 31B (vision), Gemma 4 26B, Sarvam Gemma 4; raw text |
| Architect | Outline, prerequisites, graph | Gemma 4 31B, 26B, Sarvam Gemma 4; linear outline |
| Curator | Find, verify, rank links, videos, images | Jev (relevance), Gemma 4 26B; search order |
| Teacher | Notes and slide cards | Gemma 4 26B, 31B, Sarvam Gemma 4 (Sarvam-105B for Indian languages); cached lesson |
| Critic | Check against sources | Gemma 4 31B, 26B, Sarvam Gemma 4; rule checks |
| Quizzer | Questions, keys, hints | Gemma 4 26B, 31B; cached questions |
| Diagnoser | Grade, name the misconception | code first, Jev, Gemma 4 26B; exact match |
| Rewriter | Add review chapters | Gemma 4 26B, 31B; one review item |
| Companion | Open and close sessions, memory | Gemma 4 26B, 31B; template |
| Doubt answerer | Answer from the chapter | Gemma 4 26B, 31B; keyword retrieval |

Models, fallback chains, timeouts and retries live in `config/models.yaml`. Agents never call a model directly.
Web access (reading URLs, discovery, verification) is in `backend/app/web.py`: tiered page reading
(httpx + trafilatura, then Jina Reader, then Firecrawl), SSRF guard, robots.txt respected, link/video/image verification in code.

## Layout

```
backend/app/   engine.py (pipeline), worker.py (jobs), router.py (models), web.py, db.py, main.py (API), agents/, providers/
backend/tests/ 99 offline tests
frontend/      plain HTML/CSS/JS (no build step): app.js, graph.js, markdown.js, util.js
config/        models.yaml, seed_resources.yaml
scripts/       acceptance.py
eval/          quality metrics (run_eval.py) and datasets
build-log/     what was built, milestone by milestone, and known gaps
Dockerfile, docker-compose.yml, Caddyfile, DEPLOY.md
```

## Deploying

One small server with Docker Compose and Caddy (automatic HTTPS), SQLite on a volume, nightly backups.
See [DEPLOY.md](DEPLOY.md). SQLite means exactly one app instance.

## Privacy and limits

No accounts. A random learner id lives in the browser; memory (mistake labels, progress) and uploads stay in the local database and data volume.
No medical, legal or other sensitive advice. Notes are written in the tutor's own words: third-party pages are used to check facts and to link to, never pasted.

The original planning documents (`PRD.md`, `agents/`, `architecture/`) describe the first, single-path concept of this project and are kept for reference.
