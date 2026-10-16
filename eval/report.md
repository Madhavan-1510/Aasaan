# Evaluation report

Run: 2026-10-16 23:32  |  mode: **mock**

| Metric | Result | Status | Note |
|---|---|---|---|
| Grading agreement (20 answers) | 20/20 = 100% | PASS | target >= 80% (labels are drafts until a teacher confirms) |
| Critic catches seeded errors | SKIPPED | SKIPPED | mock Critic always approves; run with --mode live |
| Chapters with valid source refs | 4/4 chapters | PASS | target 100% |
| Fallback: kill models, notebook still opens | primary dead: pass; all models dead: pass | PASS | notebook opens (with source material, code-only fallbacks allowed) |
| Course gains a review item after a wrong answer | 4 -> 5 chapters (['Review: misunderstands Motion and Rest A']) | PASS | course gains a review item after a wrong answer |
| Second session mentions earlier mistake | "Welcome back! Last time you mixed up 'misunderstands Motion and Rest A" | PASS | second session references an earlier mistake |

> Mock mode: this proves the plumbing only. Model-quality metrics need `--mode live`.
