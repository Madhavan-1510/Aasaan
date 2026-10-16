# PRD: Swarm Tutor (working title)

Hacktoberfest Hack Day Bengaluru 2026 (MLH). Status: draft v0.1.

## 1. Summary

A personal tutor for school students that turns the student's own study material (a textbook photo, a PDF chapter, pasted notes) into a short adaptive learning path. A swarm of small, specialised AI agents does the work: one reads the material, one plans the path, one teaches, one fact-checks the lesson, one quizzes, one diagnoses mistakes, one rewrites the path, and one remembers the student across sessions.

The student sees a simple, minimal interface. The swarm is invisible in v1 (see Non-goals).

## 2. Hackathon context

| Item | Decision |
|---|---|
| Primary track | Track 1, G1: Autonomous Multi-Agent Collaboration (Gemma 4) |
| Secondary angles | G2 voice (Sarvam), G3 multimodal (photo input), O1 agent swarms, O2 memory and self-correction |
| Partners to use | Gemma 4 (core), Sarvam AI (Indic voice and translation), Jev (optional fast decisions) |
| Open question | Whether one project may be submitted to both tracks. Check the official rules. |

Rule to design for: Gemma 4 must do meaningful work. Jev and Sarvam support Gemma 4. They do not replace it.

## 3. Problem

- Students study from static material (textbook, notes) and get no feedback on what they misunderstood.
- Generic chatbots answer questions but do not build a path, remember mistakes, or check their own explanations against the student's material.
- Most good tutoring tools assume English. Many students think and ask doubts in Kannada, Hindi, Tamil or a mix with English.

## 4. Target user (v1)

- Primary: a school student, roughly Class 8 to 10, studying one chapter at a time.
- Languages for demo: English plus one Indian language (to be confirmed based on Sarvam support).
- Not in v1: teachers, parents, schools.

## 5. Goals and non-goals

### Goals
1. Upload material and get a short learning path (at most 8 steps) within about a minute.
2. Teach one step at a time, grounded in the student's own material, with every lesson checked by a Critic agent.
3. Quiz, find the underlying misconception (not just right or wrong), and visibly adjust the path.
4. Remember mistakes across sessions and use them next time.
5. Let the student ask a doubt by voice in an Indian language and hear the answer back.
6. Keep working when a model fails, using per-agent fallbacks.

### Non-goals for v1
- Swarm view or any UI that shows agents talking. Events are still logged internally so this can be added later.
- Accounts, login, payments, marketplace.
- Teacher dashboard, parent reports.
- Calendar or email integrations, reminders.
- Support for every subject. v1 targets one subject and one chapter well.
- Medical, legal or other sensitive advice.

## 6. User experience (minimal)

Single column, mobile-first, no account. Five screens.

1. Start: upload a photo or PDF, or paste text. Pick language. Optional one-line goal ("exam on Friday").
2. Path: a plain list of steps with a marker on the current step. A short "Preparing your lesson" state while agents work. No agent names or logs.
3. Lesson: short text split into small steps, a play button for audio, and a "Ask by voice" button.
4. Quiz: one question at a time, answer, then feedback with a hint if wrong.
5. Recap: what went well, what to revisit, and a "continue next time" prompt.

Design rules: large text, one action per screen, no menus, no settings beyond language. Local profile stored on the device or server session, no personal details collected.

## 7. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| F1 | Accept image, PDF or pasted text and extract topics with page references | Must |
| F2 | Generate a learning path of at most 8 steps with prerequisites (acyclic) | Must |
| F3 | Generate a lesson for the current step using only the student's material | Must |
| F4 | Critic reviews each lesson against source text and can send it back (max 2 revisions) | Must |
| F5 | Generate 3 to 5 questions per step with answer keys and concept tags | Must |
| F6 | Grade answers and identify a misconception label when wrong | Must |
| F7 | Rewrite the path after a quiz (insert review step, reorder, skip) | Must |
| F8 | Persist mistakes and progress; Companion uses them in the next session | Must |
| F9 | Voice input and spoken output in one Indian language plus English | Should |
| F10 | Per-agent model assignment with fallbacks from one config file | Must |
| F11 | Every agent call validated against a schema; invalid output triggers fallback | Must |
| F12 | Internal event log (agent, model used, fallback, latency) written to local storage | Must |
| F13 | Local mode using a locally hosted Gemma 4 E4B | Could |
| F14 | Swarm view, teacher dashboard, reminders | Won't (v1) |

## 8. System design

See `architecture/` for diagrams.

- `01-system-overview`: layers from app to models.
- `02-learning-loop`: the one flow the demo shows.
- `03-model-routing`: how fallbacks work.

Key design decisions:

1. Agents never call a model directly. They request a role from the model router, which reads `config/models.yaml`.
2. Orchestration uses LangGraph (explicit loops for Teacher and Critic, and for the quiz-to-path loop). A plain Python loop is an acceptable fallback if time runs short.
3. Agents exchange structured JSON, validated on every hop. Free-text between agents is avoided.
4. Voice (speech to text, text to speech, translation) is a service behind the router, not an agent.
5. Memory is a small local database (SQLite) holding the learner profile, mistake labels and path history.
6. Agent specs live in `agents/`.

### Why this is a swarm, not a pipeline
- The Critic can reject the Teacher's work and force a revision.
- The Diagnoser's findings change the Rewriter's output, which changes what the Teacher does next.
- The Companion's memory changes how every other agent behaves in later sessions.

A fixed three-prompt chain is not enough for the hackathon criteria. These feedback loops are the point.

## 9. Models

Assignments are a starting guess. Tune after testing latency and quality. All model identifiers are placeholders until confirmed in the provider console.

| Agent | Primary | Fallback 1 | Fallback 2 |
|---|---|---|---|
| Intake | Gemma 4 26B A4B | Gemma 4 E4B | Plain text extraction only |
| Architect | Gemma 4 26B A4B | Gemma 4 E4B | Linear path from topics |
| Teacher | Gemma 4 26B A4B | Gemma 4 E4B | Cached lesson |
| Critic | Gemma 4 26B A4B | Gemma 4 E4B | Rule checks only |
| Quizzer | Gemma 4 E4B | Gemma 4 26B A4B | Cached questions |
| Diagnoser | Jev (if accessible) | Gemma 4 E4B | Exact-match grading |
| Rewriter | Gemma 4 E4B | Gemma 4 26B A4B | Insert a review step |
| Companion | Gemma 4 E4B | Gemma 4 26B A4B | Template message |
| Voice and translation | Sarvam | Gemma 4 for translation | Text-only mode |

Notes:
- Free-tier rate limits for Gemma 4 on the Gemini API vary between sources. Verify in the console, and cache the demo scenario.
- Free-tier prompts may be used for product improvement. Use sample data only, never real student data.
- Confirm whether Jev and Sarvam are accessible during the hackathon and whether open-weight requirements apply to partner tools.

## 10. Non-functional requirements

- Latency: path in about 60 seconds or less; a lesson in about 20 seconds or less. Show a calm loading state.
- Reliability: a single model outage must not break the demo (fallbacks, cached demo content).
- Privacy: no real student data in cloud calls during the hackathon; memory stays local.
- Safety: the tutor guides with hints before answers; no sensitive-topic advice; the Critic blocks lessons not supported by the source material.
- Cost: runs within free tiers for the demo; E4B used wherever quality allows.

## 11. Success metrics

Measured on a small test set prepared before the demo.

| Metric | Target |
|---|---|
| Grading agreement with a teacher on 20 sample answers | 80% or higher |
| Critic catches seeded errors (10 deliberately wrong lessons) | 80% or higher |
| Lessons with valid source references | 100% |
| Fallback test: kill primary model, flow still completes | Pass |
| Path visibly changes after a wrong answer | Pass |
| Second session references an earlier mistake | Pass |

## 12. Demo script (3 minutes)

1. Upload a photo of one textbook page (pre-tested sample). Choose language.
2. Path appears.
3. Lesson plays. Student asks a doubt by voice in an Indian language and hears the answer.
4. Quiz: answer one question wrongly. Feedback names the misconception.
5. Path updates with an extra review step.
6. Start a new session: Companion greets with "last time you mixed up X".
7. Optional: kill the primary model and repeat one step to show fallback.

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Rate limits during demo | Cache sample outputs, use E4B for cheap steps, fallback chain |
| Handwriting or poor photo breaks Intake | Pre-test samples, retake prompt, text-only path |
| Wrong explanation reaches student | Critic plus source references, flag unverified lessons internally |
| Partner access (Jev, Sarvam) unavailable | Every role has a Gemma-only fallback |
| Scope creep | Non-goals list above; one subject, one chapter |
| Similar projects at the event | Lead with Indic voice, mistake memory, and the Critic loop |

## 14. Milestones (fill in once time is known)

| Milestone | Target |
|---|---|
| Skeleton: router, config, stub agents, event log | |
| Intake, Architect, Teacher working with real Gemma 4 | |
| Critic loop and Quizzer | |
| Diagnoser, Rewriter, Memory | |
| Voice with Sarvam | |
| Minimal UI complete | |
| Test set and metrics | |
| Demo rehearsal and cached fallback content | |

## 15. Open questions

1. Team size and total hours available?
2. Which Indian language first (depends on Sarvam support)?
3. Which subject and chapter for the demo?
4. Gemma 4 hosting: Gemini API, local, or both?
5. Can one project be submitted to both tracks?
6. Are partner tools (Jev, Sarvam) allowed to count toward the open-source track?
