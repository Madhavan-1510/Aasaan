# Evaluation report

Run: 2026-10-16 16:06  |  mode: **mock**

| Metric | Result | Status | Note |
|---|---|---|---|
| Grading agreement (20 answers) | 20/20 = 100% | PASS | target >= 80% (labels are drafts until a teacher confirms) |
| Critic catches seeded errors | SKIPPED | SKIPPED | mock Critic always approves; run with --mode live |
| Lessons with valid source refs | 4/4 lessons | PASS | target 100% |
| Fallback: kill models, flow completes | primary dead: pass; all models dead: pass | PASS | flow completes |
| Path changes after a wrong answer | 4 -> 5 nodes | PASS | path visibly changes after a wrong answer |
| Second session mentions earlier mistake | "Welcome back! Last time you mixed up 'misunderstands Motion and Rest A" | PASS | second session references an earlier mistake |

> Mock mode: this proves the plumbing only. Model-quality metrics need `--mode live`.
