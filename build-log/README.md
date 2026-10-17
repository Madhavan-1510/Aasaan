# Aasaan build log

Last updated 2026-10-16. Source of truth for the requirements: the build brief (notebooks, progressive generation).

## Status

All six milestones are built. **102 offline tests pass.** `scripts/acceptance.py` against a running server in demo mode:
**17 passed, 0 failed, 3 skipped** (see "Not verified" for why).

Nothing in this repository has yet run against real Gemma, Sarvam or Jev, because no keys were available. Everything that
touches those services was written from their official documentation and tested with recorded requests, not live calls.

## Milestones

| | Scope | Status |
|---|---|---|
| M1 | Rename to Aasaan, model config (Gemma 4 31B/26B), real Gemini/Sarvam/Jev clients from the docs, visible fallbacks, `/api/health/models`, startup self-test | Done |
| M2 | Notebook data model, Intake with URL tiers, Architect outline (3-20 chapters, DAG), Curator (discover, verify, gate), chapter 1 built before the notebook opens | Done |
| M3 | Background job worker (2 at a time), live updates over SSE, priority bump, resumable jobs, input-hash call cache, global call limit, per-IP cap | Done |
| M4 | Notebook UI: roadmap and graph views, chapter blocks (notes editor, slides, video, images, links, quiz, summary), skeletons, progress, voice buttons | Done |
| M5 | Learning loop on notebooks: Diagnoser (Jev first), Rewriter adds review chapters, Companion memory across sessions, doubts, voice | Done |
| M6 | Dockerfile, Compose, Caddy, backup, `DEPLOY.md`, README, evaluation harness, acceptance script | Done (image build not run, see below) |

## How the pieces fit

```
chat box (text, links, files, mic)
   -> POST /api/notebooks          creates the notebook + a `notebook` job, returns immediately
   -> Loading screen polls         until open_ready (outline + chapter 1)
Worker (asyncio, jobs table)
   notebook job:  phase0  Intake per source -> Architect outline -> chapters rows
                  phase1  build_chapter(1): Curator -> Teacher <-> Critic (max 2 revisions) -> Quizzer -> blocks (one transaction)
                  then queues a `chapter` job per remaining chapter (priority first, then order; 2 at a time)
   chapter job:   build_chapter(n); 3 attempts with backoff, then `failed` with a reason and a Try again button
Notebook page: roadmap/graph + chapter page, updated live by SSE
Quiz: answer -> Diagnoser (code first, then Jev, then Gemma) -> finish -> Rewriter inserts a review chapter -> worker builds it
```

## Files

| Path | What it does |
|---|---|
| `backend/app/main.py` | FastAPI app: notebooks, chapters, blocks, quiz, doubts, voice, SSE, health, protected admin endpoints, static frontend, lifespan (worker + model self-test) |
| `backend/app/engine.py` | The pipeline: `create_notebook`, `phase0`, `build_chapter`, `finish_chapter`, views, memory, doubts. Friendly errors for students |
| `backend/app/worker.py` | In-process job worker with a concurrency limit and restart recovery |
| `backend/app/router.py` | Model router: chains, retries, JSON repair, call cache, global semaphore, `decide()` for Jev, services, self test, events with errors |
| `backend/app/web.py` | Page reading (3 tiers), SSRF guard, robots.txt, discovery (Wikipedia, YouTube, Commons, Gemini grounding, seeds), verification, Wikipedia lead images |
| `backend/app/db.py` | SQLite (WAL): notebooks, sources, chapters, blocks, quiz_attempts, memory, sessions, events, jobs, cache |
| `backend/app/schemas.py` | Output schemas and code validation (acyclic outlines, slide and notes rules, quiz keys, patch caps) |
| `backend/app/voice.py` | Speech to text, text to speech, translation with fallbacks |
| `backend/app/agents/` | `intake`, `architect`, `curator`, `teacher`, `critic`, `quizzer`, `diagnoser`, `rewriter`, `companion`, `doubt`, `common` |
| `backend/app/providers/` | `gemini` (incl. Google Search grounding), `sarvam_chat`, `sarvam` (speech/translate), `typesafe` (Jev), `ollama`, `mock` (demo mode) |
| `backend/tests/` | 102 offline tests (web layer with a fake network, router, curator, engine pipeline, worker, API) |
| `frontend/` | `app.js` (routing, home, loading, notebook, chapter, quiz, doubts), `graph.js`, `markdown.js`, `util.js`, `style.css`, `index.html` |
| `config/models.yaml`, `config/seed_resources.yaml` | Model chains and fallbacks; small curated fallback links per subject |
| `scripts/acceptance.py` | The 8 acceptance checks against a running server |
| `eval/` | `run_eval.py` plus datasets: grading agreement, seeded-error Critic test, citation, fallback, review, memory |
| `Dockerfile`, `docker-compose.yml`, `Caddyfile`, `.dockerignore`, `DEPLOY.md` | Single-server deployment with HTTPS and nightly backup |

## Decisions and deviations (all deliberate)

1. **Frontend is plain JS, not React.** The brief allows "the lightest equivalents". The graph is a small custom layered layout (rows wrap at 3 nodes) rendered as SVG; the notes editor is a markdown textarea with a safe renderer. No build step.
2. **Notes are markdown, edited in a textarea**, not a rich-text editor.
3. **`saaras:v4` / `bulbul:v4-flash` first, v3 as fallback.** Sarvam's current docs name v4 as current; the brief named v3.
4. **Jev is never in a text chain.** `router.decide()` asks it typed questions (Noul, Choice, Score) in one call. If it is down, unsure, or answers `other`, Gemma continues.
5. **Third-party text is never pasted.** Fetched pages are used to check facts and as link snippets. A 40-word verbatim run from a fetched page is rejected in code, and last-resort lessons never copy it.
6. **Code guards in front of model gates:** fuzzy Wikipedia hits and images must share a meaningful word with the chapter topic before the relevance gate; videos pass YouTube oEmbed; links must load and be allowed by robots.txt.
7. **Images:** Wikimedia Commons with author and licence; the lead image of each linked Wikipedia article (only Commons files, never local fair-use images).
8. **Demo mode** (no `GEMINI_API_KEY`) uses the offline mock models with a visible banner. Notes there are explicit placeholders. Web lookups (Wikipedia, Commons) are real.
9. **Admin endpoints** (`/api/events`, `/api/admin/*`) need `AASAAN_ADMIN_TOKEN`, or localhost only. The brief did not ask for this; they would otherwise be public behind Caddy.
10. **Chapter "done" and "in progress"** are learning state (opened, answered, finished); chapter status (`queued` .. `ready`, `failed`) is generation state.
11. The old single-path product (PRD, `agents/`, `architecture/`) is kept for reference only; its code was replaced.

## Not verified (read this before relying on it)

- **No live calls** to Gemini, Sarvam or Jev. Open points: Gemma JSON mode (`responseMimeType`) is not confirmed in Google's docs (the client retries without it on a 400); Gemma image input and Google Search grounding are documented but untested here; Sarvam chat image input is undocumented (refused, not guessed); Sarvam TTS default speaker (set `providers.sarvam.tts_speaker` if the health check fails); `bulbul`/`saaras` ids follow the docs of 2026-10-16.
- **Acceptance skips:** (1g) no video without `YOUTUBE_API_KEY` or live Gemini grounding; (5b) models are not called in demo mode; (7c) a spoken Indian-language doubt needs a microphone and a Sarvam key.
- **Latency budgets** (outline about 15 s, chapter 1 about 40 s) were measured only in demo mode (0.4 s and 4 s) where no model runs. Real numbers depend on Gemma and rate limits.
- **Docker image build not run** (Docker Desktop was not running). The Compose file validates (`docker compose config`). Do the first build on the server and follow `DEPLOY.md` step 5.
- **Browser checks done in Chrome (desktop width, dark theme):** home, loading animation, failure state, notebook, roadmap, graph, chapter blocks, quiz with hint and misconception, review chapter appearing live. **Not checked:** phone width, light theme, file picker, microphone, Hindi/Tamil/Kannada generation, keyboard-only use.
- **Evaluation numbers** (`eval/report.md`) in demo mode prove plumbing only. The Critic metric needs `--mode live`. The expected grading labels are drafts until a teacher reviews them.
- Legacy docs (`PRD.md`, `agents/`, `architecture/` diagrams) still describe the first concept and use its wording.

## Illustrator (Milestone 2.1, step e)

Code draws, models only decide and fill small JSON. `backend/app/visuals/`: `specs.py` (nine pydantic diagram specs: flowchart, array, linked_list, stack_queue, tree, graph, table, timeline, plot; at most 12 nodes / 16 elements, labels at most 5 words, decision nodes need two labelled edges, trees validated), `render.py` (deterministic SVG, light + dark in the file, escaping, overlap/fit check), `astflow.py` (Python AST -> flowchart, no model), `mermaid.py` (parse-checked subset -> flowchart spec; Mermaid itself is never rendered), `safeexpr.py` (plot expressions through an `ast` whitelist, never `eval`), `figures.py` (Commons SVG-first -> Openverse -> none, licence allow-list, topic guard, hotlink only), `cover.py` (generated notebook cover, `GET /api/notebooks/{id}/cover.svg`).
`agents/illustrator.py` runs after the Critic approves the notes, concurrently with the quiz, with `AASAAN_ILLUSTRATOR_TIMEOUT_S` (default 90). One batched decision per chapter (Noul "would a picture help" + Choice of kind) through the `decisions` chain; with no provider answering, rules apply (Python snippet -> code flowchart, otherwise nothing). Spec kinds go to the `illustrator` role (gemma_26b, gemma_31b, sarvam_gemma4, deterministic_only), one repair per model; free-form diagrams are Mermaid, parse-checked and converted; real figures are never model-drawn. One batched faithfulness gate drops low-confidence visuals. At most 3 visuals per chapter, cached by hash(topic, kind, section) across notebooks. A visual that fails is skipped with an event and never blocks the chapter. Block type `visual`: `kind`, `payload` (svg + spec, or url), `alt_text`, `caption`, `attribution`, `source_tool`, `verified`.
Not verified: visual quality of the SVG layouts was checked by the overlap/fit test and by reading, not by eye in a browser; no live model has filled a spec yet; Openverse response fields come from the docs, not a live result; `AASAAN_IMAGE_GEN=off` is the only mode (there is no image generator).

## Decision gates (Milestone 2.1, step f)

All in `backend/app/agents/gates.py`, all through `router.decide` (Jev, Jev via OpenRouter, Mercury Decide, Gemma; one batched call per provider; unsure answers escalate; every skip and escalation is an event), each with a code fallback and none able to block work by failing.
- **page_health**: after each fetch tier (`web.fetch_page`): a cookie wall, login, CAPTCHA or error page (code pattern first, then Noul) sends the fetcher to the next tier.
- **claim_support**: before the Critic. Every claim sentence gets a Noul against the source chunks. All supported -> the Critic model is skipped (code checks still run); otherwise only the flagged claims go into the Critic prompt. No sources or no provider -> the Critic runs as before.
- **scoper**: track (Choice) and level (Score) before the Architect; the track goes into the outline prompt, the level is used when the notebook has none. Student data: Mercury is never used.
- **video_marker**: chapter markers parsed from the YouTube description (same key and budget as search), one batched Choice per chapter; the block carries `start` and the player opens at that second. Unsure or "none" -> 0:00.
- **quiz_sanity**: a Noul per question ("exactly one correct, and it is the marked one"); doubtful questions are dropped while at least 3 remain, otherwise the quiz is kept and an event says so.
- **input_gate**: on a plain goal only (never with pasted material, links or files). Only a confident "unsafe" (0.7) or "not a request" (0.85) is refused, with a friendly message; 6 s timeout; student data.
Not verified live: none of these has been run against the real Jev or Mercury Decide (no TypeSafe key is set; the OpenRouter route was only probed earlier). Thresholds (0.5 / 0.6 / 0.4 / 0.7 / 0.85) are my starting values, not tuned on data. The Mercury Decide free tier's rate limits and data policy are unconfirmed, which is why it is excluded from student-data gates.

## Operating notes

- Logs: lines prefixed `[aasaan]`; every fallback and retry has its reason. `GET /api/events` (admin) shows the same in the database.
- A failed chapter shows its friendly reason and a Try again button; the technical error is kept on the job row.
- The model-call cache is keyed by a hash of (model id, prompt, images) and only stores replies that passed validation.
- Rate limits: `AASAAN_CHAPTER_CONCURRENCY` (default 2), `AASAAN_MAX_CALLS` (default 4), `AASAAN_NB_PER_HOUR` (default 12 per IP).

## Change history

| Date | Change |
|---|---|
| 2026-10-17 | Milestone 2.1: resilience module, ToolRunner, chaos, health/tools, Pexels removed, decision chain, Illustrator, Jev gates. |
| 2026-10-16 | First version (single learning path), then the Aasaan rebuild: M1 router and health, M2-M5 notebook pipeline, worker, UI, learning loop, M6 deploy and acceptance. |
