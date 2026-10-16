# Demo runbook (3 minutes, PRD section 12)

## Before the demo (once keys exist)
1. Put keys in `.env`. Check real model ids in `config/models.yaml` (all marked `TODO verify`).
2. `python eval/run_eval.py --mode live` and read `eval/report.md`. Fix what fails.
3. `python scripts/warm_cache.py --mode live` writes `data/demo_cache.json` (approved lessons and quizzes). The app loads it at startup, so a dead model still returns pre-checked content for the sample chapter.
4. Start the server: `python -m uvicorn app.main:create_app --factory --app-dir backend --port 8000`
5. `python scripts/preflight.py` should print all PASS. Re-run right before going on stage.
6. Open http://localhost:8000 in a fresh browser profile (clears the saved learner), allow the microphone.

## Script
| # | Do | Expect |
|---|---|---|
| 1 | Paste `samples/motion_chapter.txt` (or upload the pre-tested photo), choose language | "Preparing your lesson" |
| 2 | Path appears | 4 to 6 steps, first one marked |
| 3 | Open lesson, press Listen, then Ask by voice | spoken doubt answered from the book |
| 4 | Take quiz, answer the first question wrongly | feedback names the misconception and gives a hint |
| 5 | Finish quiz | recap says the path changed, new "Review:" step |
| 6 | Finish for today, then reload and press Continue | greeting mentions the earlier mistake |
| 7 | Optional fallback demo | see below |

## Fallback demo (step 7)
```
curl -X POST localhost:8000/api/admin/kill -H "Content-Type: application/json" -d '{"model":"gemma_big"}'
# repeat one lesson in the UI, then show the events:
curl localhost:8000/api/events?limit=10
curl -X POST localhost:8000/api/admin/revive -H "Content-Type: application/json" -d '{}'
```
Events show `model_used: gemma_small` and `status: fallback`.

## If something goes wrong
| Symptom | Likely cause | Action |
|---|---|---|
| Lessons look like plain extracted text | model ids wrong or key missing; fell to last resort | check `/api/events` for `status: fallback` and the error summary |
| "Voice is unavailable" | Sarvam key/endpoint | type the doubt instead; text mode is built in |
| Photo gives "could not read" | blurry or vision request failed | paste text or use the pre-tested photo |
| Everything slow | free-tier rate limit | rely on `demo_cache.json`; avoid fresh material |
