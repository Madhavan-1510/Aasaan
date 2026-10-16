# Build log

What has been built so far, phase by phase, and what every file does. Updated after each piece of work.
Spec sources: `PRD.md`, `agents/*.md`, `config/models.yaml`, `architecture/*`.

## Aasaan rebuild (brief: notebooks, progressive generation)

The product is now called **Aasaan**. The first version (below, "Phases 1 to 6") is a single learning path.
The new brief asks for notebooks: a full course per goal with a roadmap and a graph, built progressively.

| Milestone | Scope | Status |
|---|---|---|
| M1 | Rename, model config, real Gemini/Sarvam/Jev clients, visible fallbacks, `/api/health/models`, startup self-test | **Done** |
| M2 | Notebook data model, Intake with URL tiers, Architect outline (8 to 20 chapters), Curator, chapter 1 fully built, Loading screen | Next |
| M3 | Background job worker, concurrency limit, live updates (SSE), priority bump, resumable jobs, caching | |
| M4 | Notebook UI: roadmap and graph views, chapter blocks, slides, quiz, progress | |
| M5 | Diagnoser (Jev), Rewriter, Companion, doubts and voice on the notebook model | |
| M6 | Docker, Compose, Caddy, `DEPLOY.md`, README, the 8 acceptance tests | |

### M1 report
- **Rename:** Aasaan in the UI title, header, env vars (`AASAAN_MODE`, `AASAAN_DB`), DB file (`aasaan.db`) and code (`Engine`). Old name removed from code. Still present: the user's planning docs (`PRD.md` body, `agents/`, architecture images) use the old concept wording; not touched.
- **Models (`config/models.yaml`):** `gemma_small` (e4b) is gone. Gemini API models are only `gemma-4-31b-it` (planner, checker, vision) and `gemma-4-26b-a4b-it` (writer), confirmed in Google's Gemma docs. Added Sarvam `gemma4` and `sarvam-105b` (chat), Jev (decision), Sarvam speech/translate. Chains follow the brief's table. `teacher`, `companion` and `doubt` switch to their `_indic` chain (Sarvam-105B first) when the language is not English.
- **Clients written from the official docs (not guessed):** Jev `POST /v1/systemone` (Bearer, `state`, `questions`, `jev-latest`); Sarvam chat `/v2/chat/completions`; STT `/speech-to-text` with `model` and `mode` (codemix); TTS `/text-to-speech` with `text`, `language_code`, `model`; translate `/translate` with `mayura:v1`. The earlier Sarvam client used wrong fields and was replaced.
- **Docs vs brief:** Sarvam docs now list `saaras:v4` as current (v3 legacy) and `bulbul:v4-flash` as latest TTS. Config uses v4 first with v3 as the fallback.
- **Jev is a decision model:** it is never in a text chain. `router.decide()` sends several typed questions in one call. Diagnoser asks: is it correct (Noul), which known misconception (Choice with an `other` option), how complete (Score). If Jev is unavailable, unsure, or answers `other`, the Gemma chain proposes the label.
- **No silent fallback:** every retry, fallback and last resort writes an event with the error text (`events.error`) and a WARNING in the server log. Example (no keys): `architect step0 gemma_31b fallback GEMINI_API_KEY is not set`.
- **Self test:** `GET /api/health/models` calls each model once (cached 60 s, `?refresh=true` forces) and returns `ok` or `failed` plus the error. In live mode it also runs at startup and logs one line per model. `GET /health` is the plain liveness check. Verified with no keys: all keyed models report `failed` with the variable name.
- **JSON output:** Gemini calls ask for `responseMimeType: application/json`. Google's docs do not confirm this for Gemma, so a 400 about it is logged and the call is retried once without it.
- **Tests:** 49 pass offline (provider request shapes with a recorded `httpx.post`, Jev decisions, fallback reasons, self test, config sanity).
- **Not verified against live services (no keys yet):** Gemma JSON mode and vision on the Gemini API, Sarvam TTS speaker default (`providers.sarvam.tts_speaker` in config if the health check fails), Sarvam chat image input (undocumented, so refused), Sarvam browser-recorded audio format (STT wants WAV/MP3/AAC/FLAC/OGG, under 30 s), Jev behaviour.
- **Missing input:** `aasaan-models-keys-deploy.md` was not provided; the brief's tables and the live docs were used instead.

## Current status of the first version (2026-10-16)

- Phases 1 to 4 (skeleton, core pipeline, adaptive loop, UI), the code for phase 5 (voice) and the tooling for phase 6 (eval, demo cache, preflight, runbook) are built.
- **31 automated tests pass, all offline.** Nothing has talked to a real model yet.
- The app runs fully in **mock mode** (no keys). API keys are the last step.
- Not yet done: real Gemma 4 / Sarvam / Jev calls, the live evaluation run, the real demo cache, teacher review of the draft grading labels, and a full browser pass in every language and with voice (a partial click-through in Chrome was done, see change history).

## How to run

```
pip install -r backend/requirements.txt
python -m pytest backend -q                                                  # 31 tests, no keys needed
python -m uvicorn app.main:create_app --factory --app-dir backend --port 8000   # then open http://localhost:8000
```

Mode is chosen automatically: `live` if `GEMINI_API_KEY` is set, else `mock`. Force with `AASAAN_MODE=mock|live`.
Keys go in `.env` (copy `.env.example`) at the end.

## Phases

### Phase 1: Skeleton (router, config, event log, stub providers)
Done. The router reads `config/models.yaml`, walks each role's fallback chain, validates every output against
a schema, retries invalid JSON once on the same model, logs one event per attempt, and runs a code-only last
resort if every model fails. A `MockProvider` returns valid JSON for every role so the whole loop runs offline.
SQLite holds the profile, mistakes, paths, lessons, quizzes, answers, sessions and events.

### Phase 2: Core pipeline (Intake, Architect, Teacher, Critic, Quizzer)
Done in code, verified in mock mode only.
- Intake: text and PDF extraction and chunking happen in code (chunk ids like `s1-p12-c3`); the model groups topics. Photos go to the vision model.
- Architect: path of at most 8 nodes, acyclic, every node has source refs, review node from mistake memory.
- Teacher and Critic loop: Critic can send a lesson back, max 2 revisions, then the best draft ships marked unverified.
- Quizzer: 3 to 5 MCQs with key, concept tag and hint. Keys and hints are never sent to the browser up front.

### Phase 3: Adaptive loop (Diagnoser, Rewriter, Companion, memory)
Done in code, verified in mock mode only.
- Diagnoser: grading is always done in code; the model only names the misconception and writes the hint. Low confidence gives a retry instead of a verdict.
- Rewriter: patch (insert/reorder/skip) applied in code and re-validated. At most 2 inserted nodes per session, path never above 10 nodes, no review of a review node, skipped nodes cannot be prerequisites.
- Companion: greets with stored mistake labels at session start, summarises at the end, writes allowed memory fields only.

### Phase 4: Minimal UI (redesigned)
Done. Plain HTML/CSS/JS, no framework, no agent names shown. Redesigned to be chat-first with a course page:
- **Home:** one big chat composer (paste any text, or attach/drop a photo or PDF) under a serif greeting. Toolbar slot holds Attach and a language picker. No community, groups or other extras. Your own courses appear below as cards with progress.
- **Composer:** vanilla port of the layout of the 21st.dev "Input Bar / toolbar actions" component (MIT): auto-growing textarea, image/file attachment chips with size and remove, left toolbar slot, round send button. Only the public demo and usage were read; the component's source is behind an unlock and was not used.
- **Course page:** hero with generated gradient cover and serif title, facts row (lessons, minutes, done), progress bar, description, numbered lessons with status chips (Done, Up next, Review), sticky Study/Continue and Finish for today.
- **Lesson, quiz, recap:** restyled to match, with a step bar and Back to course.
- Look: warm paper background with dot grid, serif headings (Newsreader), Hanken Grotesk body, navy primary, light and dark themes. Layout is inspired by paradigm.study's home and course overview; no logo, mascot, name or artwork was copied.
- Backend support: `course` title and description (from the goal or first topic), `progress`, and `GET /api/courses?learner_id=`.
- A bare topic ("build me a course on X") is refused with a message, because the tutor teaches only from the student's own material.

### Phase 5: Voice (Sarvam)
Code written, **untested against the real service**. Speech to text, text to speech and translation sit behind the router as services. With no Sarvam key they degrade to typed input and browser speech.

### Phase 6: Hardening
Tooling done; the parts that need real models are waiting for keys.
- Sample chapter `samples/motion_chapter.txt` (original text, 4 pages on motion).
- `eval/run_eval.py` runs the PRD section 11 metrics and writes `eval/report.md`. Mock run: grading 20/20, source refs 4/4, fallback (primary dead and all models dead) pass, path changes after a wrong answer, session 2 mentions the mistake. **Critic metric is SKIPPED in mock mode** because the mock always approves. It needs a live run.
- Data: 20 graded answers and 20 seeded lessons (10 with one deliberate factual error, 10 faithful) in `eval/data/`. **The expected labels are my drafts; a teacher must review them** before the 80% numbers mean anything.
- Demo cache: lesson and quiz caches are now keyed by a hash of the source text (not the random source id), so `scripts/warm_cache.py` output still hits when the same chapter is uploaded again. The app loads `data/demo_cache.json` at startup. The script refuses to run on mock output unless forced.
- `scripts/preflight.py` walks the whole demo over HTTP against a running server and then kills the primary model to show the fallback. 9/9 checks pass in mock mode.
- `build-log/demo-runbook.md`: before-demo steps, the 3-minute script, the fallback demo and a troubleshooting table.
- Not done: live metrics, teacher-reviewed labels, real cache, rehearsal with real models.

## What every file does

| Path | What it does |
|---|---|
| `backend/app/schemas.py` | Pydantic schemas for every agent output plus code checks (acyclic path, caps, key resolution). Invalid output raises and triggers fallback. |
| `backend/app/router.py` | Model router: loads `config/models.yaml`, fallback chains, retries, JSON repair, event logging, "kill a model" hook, voice service chains. |
| `backend/app/db.py` | SQLite store: learners, mistakes, sources, paths and history, node status, lessons, quizzes, answers, sessions, events, cache. |
| `backend/app/orchestrator.py` | The learning loop as a plain-Python state machine: start, open/close session, lesson (Teacher/Critic loop), quiz, answer, finish node (Rewriter), doubt. |
| `backend/app/voice.py` | Speech to text, text to speech, translate with fallbacks. Returns None when unavailable so callers degrade. |
| `backend/app/main.py` | FastAPI app, `.env` loader, JSON endpoints, static frontend, admin kill/revive for the fallback demo. |
| `backend/app/agents/common.py` | Shared prompt builder and small helpers. |
| `backend/app/agents/intake.py` | Text/PDF to pages and chunks, topic grouping, text-only last resort. |
| `backend/app/agents/architect.py` | Builds and validates the path; linear-path last resort. |
| `backend/app/agents/teacher.py` | Lesson writer; cached/extract last resort; language and source-ref checks. |
| `backend/app/agents/critic.py` | Reviews lesson; rule-checks last resort (refs, length, answer leakage). |
| `backend/app/agents/quizzer.py` | Question writer; cached-questions last resort. |
| `backend/app/agents/diagnoser.py` | Code-first grading; model labels the misconception; exact-match last resort. |
| `backend/app/agents/rewriter.py` | `apply_patch` (insert/reorder/skip with validation) and the Rewriter call; review-step last resort. |
| `backend/app/agents/companion.py` | Session open/close messages; template last resort. |
| `backend/app/agents/doubt.py` | Extra small role for spoken/typed doubts; keyword-match last resort. |
| `backend/app/providers/base.py` | Provider interface and error types. |
| `backend/app/providers/mock.py` | Offline provider with canned valid output per role, scripting and call tracking for tests. |
| `backend/app/providers/gemini.py` | Gemma 4 via Gemini API (REST). Untested until a key exists. |
| `backend/app/providers/ollama.py` | Local Gemma via Ollama (optional). |
| `backend/app/providers/jev.py` | Placeholder: reports "not configured" so the chain falls to gemma_small. |
| `backend/app/providers/sarvam.py` | Sarvam speech/translate REST client. Endpoints unverified. |
| `backend/tests/` | 31 tests: schemas, router fallbacks, patch rules, end-to-end flow, API, demo cache. |
| `samples/motion_chapter.txt` | Demo chapter (4 pages, form-feed separated). |
| `eval/run_eval.py`, `eval/data/*.json`, `eval/report.md` | Metric runner, datasets, latest report. |
| `scripts/warm_cache.py` | Builds `data/demo_cache.json` from approved lessons/quizzes. |
| `scripts/preflight.py` | End-to-end demo check against a running server. |
| `build-log/demo-runbook.md` | Demo steps, fallback demo, troubleshooting. |
| `frontend/` | `index.html`, `style.css`, `app.js`: the five-screen UI. |
| `config/models.yaml` | Existing config, plus one added role (`doubt`) and one non-model step (`template_answer`). |
| `.env.example`, `.gitignore` | Key template (fill last) and ignore rules. |

## Deviations from the spec (all small, on purpose)

1. **No LangGraph.** A plain Python loop, which the PRD allows as a fallback. The loops are explicit in `orchestrator.py`. Can be swapped later.
2. **Extra `doubt` role** in `models.yaml`, because voice Q&A (F9) needs something to answer the question. It is not an agent and is not shown in the UI.
3. **Diagnoser grades MCQs in code and asks the model only for the misconception.** This follows the spec line "numeric and multiple-choice answers are always checked in code first".
4. **Last-resort Diagnoser returns `misconception: null`** even for a wrong answer, as the spec says "no misconception label" there. The "wrong needs a label" rule is enforced on model output only.
5. **Text and PDF chunking is done in code**, not by the model, so source refs are always real. Photos still depend on the vision model.
6. **In mock mode every model name (including `jev_fast`) is served by the mock.** Tests that need the code-only fallback kill all three.

## Known gaps and things to verify when keys arrive

- Gemma 4 model ids in `config/models.yaml` are placeholders (`TODO verify id`). The Gemini request shape for image input is untested.
- Gemma models may ignore "JSON only" instructions. The router strips code fences and retries once, but real-output tuning is still needed.
- Sarvam endpoint paths, field names and model ids, and the Jev API, are unconfirmed.
- Mock quiz and lesson text is crude (it quotes source sentences). Quality of real output is unmeasured.
- Browser check (old UI) covered Start, Path, Lesson, Quiz (wrong answer with hint), Recap, the returning-student greeting and a memory-driven review step. New UI checked: home composer, language menu, send enable, course page. Not re-checked in the new UI: lesson, quiz and recap screens, voice buttons, file attach via the picker, mobile width, light theme.
- Paradigm's real study page was not seen: the community course needs a Fork, and a fork was started in the user's account ("generating your adaptive version") but never inspected. The account may hold one or two forked items to delete.
- Browser recordings are sent as the browser's native format labelled `.wav`; Sarvam may need real WAV.

## Change history

| Date | Change |
|---|---|
| 2026-10-16 | Initial build: phases 1 to 5 code, UI, 27 tests, this log. |
| 2026-10-16 | Phase 6 tooling: sample chapter, eval harness and data, content-keyed demo cache, warm_cache, preflight, runbook, 2 new tests. |
| 2026-10-16 | Chrome click-through found a bug: a wrong first answer fixed on retry left the recap at 3/3 and the path unchanged, because only the last attempt was counted. Fixed in `finish_node` (any non-correct attempt is a gap; score uses the first attempt). Test added. |
| 2026-10-16 | UI redesign: chat-first home with 21st.dev-style composer, course page, restyled lesson/quiz/recap, `/api/courses`, course title/progress. Fixed stale goal titling a new course. No-cache headers for UI files. |
