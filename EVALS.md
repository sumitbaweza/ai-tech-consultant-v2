# Evals

**Status on 14 Nov 2026: no paid model has been run on the benchmark yet,** because the spend is not approved. Every number below carries a label:

- **MEASURED**: run for real in this app, on the real data, at no cost (rules, search, URL handling, tests).
- **PROJECTED**: arithmetic: typical tokens per call (measured on the v1 prompts) times the brief's price list. A run in the Eval Lab replaces it. Quality is never projected.
- **NOT RUN**: the harness is built and tested with a stand-in model; the run needs money. Section 5 lists each one with its projected cost: about **$25 in total**.

## 1. The benchmark

| Set | Cases | Made of | File |
| --- | ---: | --- | --- |
| Knowledge Brain | 100 | 40 regular, 20 screenshot-only, 15 not in the help center, 10 only in instructions or skills, 15 synonym and typo rewordings | `benchmark/v1/brain.yaml` |
| URL cases | 10 | 301, 404, a page that goes missing, nested sitemaps, a duplicate, recovered pages | `benchmark/v1/url_cases.yaml` |
| Ask and Teach Chat | 60 | 30 regular (written like customers), 10 tricks, 10 angry, refund or legal, 10 not in the knowledge | `benchmark/v1/chat.yaml` |
| Teach tests | 10 | question, correction, the same question after the lesson | `benchmark/v1/teach.yaml` |
| Hidden check set | 20 | run only by the app, never shown to the Self-Improve agent | `benchmark/hidden/v1.yaml` |

`scripts/make_benchmark.py` drafted the cases from the v1 snapshot of the Solution Center: each expected answer is copied from its source page, with the page's URL. Tricks and hand-offs were written by hand. **Caveat:** many questions are reworded page titles, which makes finding the page easier than with real customer wording; the 15 synonym and typo cases and the 30 chat cases written like customers are the check on that. Every case still needs a human read before a score is trusted.

## 2. How a case is scored

Rules decide what has one right answer: handed off or not, said "I don't know" or not, cited the expected page, leaked nothing, no email address in the answer (`eval_lab/rules.py`). The judge (Claude Opus 5.5) only reads the facts, on cases where an answer is expected; it passes at 0.7 or more. It never judges an answer written by its own model (the free second-opinion model does then). Blind human grading on the Judge check screen measures how often the judge agrees with a person.

Pass bars set before any run: Brain 98% correct with the right source and nothing from quarantined or archived pages; chat 98% correct with the right source, no trick gets through, every angry, refund or legal question goes to a human; 8 of 10 teach tests better after the lesson.

## 3. Measured, at no cost

**3.1 URL cases: 10 of 10 pass (MEASURED).** Run on the fixture website (`benchmark/url_site`), through the Brain's API, in a throwaway business: 3 nested sitemaps found, the duplicate URL reported, 8 pages read; a 404 goes to Watch after 1 miss and is proposed for Quarantine at X = 2; a 301 quarantines the old URL and indexes the new one; the quarantined page leaves search; both pages come back to Active when they return. The first run scored 3 of 10. The cause was in the test harness, not the app: after asking for a recheck it read the previous run's "done" and looked at the pages too early. Fixed in `eval_lab/special.py` (`_wait(after_run=...)`).

**3.2 Retrieval: the right page in the top 5 for 91% of questions (MEASURED).** 75 brain cases with a source page (regular, screenshot, synonym and typo), searched through the Brain's API on the 1,700-page snapshot. No Voyage key is set, so "meaning" search (vectors) did not run: hybrid here means keywords plus typo-fixed keywords, fused and re-ranked.

| Search | Top 5 | Top 1 | Regular | Screenshot | Synonym/typo | Avg ms | p95 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Keywords only | 48% (36/75) | 21% | 50% | 45% | 47% | 127 | 257 |
| Hybrid + re-rank | **91% (68/75)** | **84%** | 98% | 75% | 93% | 124 | 242 |
| Meaning only (vectors) | not run (needs the Voyage key, about $0.0001 for 75 queries) | | | | | | |

The re-ranker doubles the hit rate. The weak spot is screenshot-only facts (75%): their words sit in screenshot descriptions, which rank below the article text. That is the first thing to fix with vectors on.

**3.3 Safety rules on every benchmark question (MEASURED).** `ask_teach_chat/safety.py` alone, before any model is asked:

| Set | Tricks caught | Sent to a human | Normal questions let through |
| --- | ---: | ---: | ---: |
| Chat benchmark | 10/10 | 10/10 | 40/40 |
| Brain benchmark | (none) | (none) | 100/100 |
| Hidden set (never used to tune anything) | 3/3 | 3/3 | 14/14 |

This check found a real bug: a regex in the "shouting" rule held two invisible backspace characters where `\b` was meant (a shell had turned them into control characters), so "I have asked THREE TIMES and NOBODY HELPS. FIX IT!!!" was missed (9 of 10). Fixed, and `tests/test_boundaries.py` now fails the build if any source file holds a control character.

**3.4 The test suite (MEASURED):** 91 tests pass (`docker compose run --rm tools pytest -q`), with no network and no cost. They cover business isolation (one business can't read another's rows, even with a bad query), the import line between the web app and the Brain, cited answers and hand-offs through the chat pipeline, the full URL-health scenario, typo search, the Opus fence (paths, shell, secrets), protected files and copied questions, merge and undo on a real git repo, the release gate, Go Live end to end, and the branch scorer running in its own process.

## 4. Projected, before any run

**4.1 One call, per step and model ($ per 100 calls, PROJECTED).** Full table: `docker compose exec web python -m eval_lab.cli projections`, or the Compare models tab.

| Step | Claude Haiku 5.5 | Claude Sonnet 5.5 | Claude Opus 5.5 | Gemini 3 Flash | Qwen 3.8 Max | Free and local models |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Write the answer (4,100 in / 350 out) | $0.06 | $1.17 | $2.34 | $0.31 | $1.03 | $0 |
| Find facts (2,800 / 150) | $0.04 | $0.71 | | | | $0 |
| Read a screenshot (1,480 / 150) | $0.02 | $0.45 | | $0.12 | | $0 |
| Judge (1,200 / 80) | | | $0.64 | | | $0 |

**4.2 The full benchmark on all 14 models: about $18.57 (PROJECTED).** 160 answers per model (100 brain, 60 chat with fact-finding), and Opus judging the 115 cases that need facts read (Nemotron judges Opus's own answers).

| Model | 160 answers | Judging | Total |
| --- | ---: | ---: | ---: |
| Claude Opus 5.5 | $3.77 | $0.00 | $3.77 |
| Claude Sonnet 5.5 | $1.90 | $0.74 | $2.63 |
| Qwen 3.8 Max | $1.67 | $0.74 | $2.41 |
| Gemini 3.8 Flash / 3 Flash | $0.73 / $0.52 | $0.74 | $1.46 / $1.26 |
| Claude Haiku 5.5, Gemini 2.5 Flash-Lite | $0.12 / $0.11 | $0.74 | $0.85 / $0.85 |
| Qwen 3.7 Flash | $0.05 | $0.74 | $0.79 |
| 4 free OpenRouter models and 2 local models (each) | $0.02 | $0.74 | $0.76 |

**4.3 Routing (PROJECTED).** A chat answer costs $1.21 per 100 on Sonnet (answer, fact-finding and tagging). If the router sends 60% of questions to Haiku and quality holds, it costs $0.54 per 100: **55% less**. Both "60%" and "quality holds" are assumptions; the router proof (section 5) measures them.

**4.4 Caching (PROJECTED).** With 2,500 tokens of system prompt read from Claude's cache, each Sonnet answer saves $0.0048: $0.48 per 100 answers, about 39%.

## 5. Not run yet, and what each costs (PROJECTED)

| Check | Where | Projected cost |
| --- | --- | ---: |
| 14 models on the 160 cases: quality, $ and speed per model | Eval Lab, Benchmarks: tick all 14 | $18.57 |
| Router proof: 30 cases on always-Sonnet, then through the router | Eval Lab, Routing | $0.91 |
| Caching proof: 100 tasks with caching off, then on | Settings, Advanced | $3.70 |
| Teach tests: 10 corrections, before and after | Eval Lab, Benchmarks | $0.65 |
| Hidden set on Sonnet | Eval Lab, Benchmarks | $0.36 |
| Local-only: 20 chat cases on the 2 Ollama models (only the judge costs) | Eval Lab, Local-only | $0.13 |
| Go Live readiness: 30 starter tests from 15 pages | Go Live, Build | $0.76 |
| Human notes on 100+ cases per model, and blind grading of 20+ answers | run pages, Judge check | time, not money |
| **Total** | | **about $25** |

The order that spends least to learn most: the hidden set first ($0.36: does the default setup pass at all?), then Haiku, Sonnet and the free models on the 160 cases (about $5), then the router proof, then the rest of the 14 models only if a cheaper model looks close. Then: grade 20 answers blind to check the judge, use the "I don't know" sweep on the run page to set the line, and apply "Use the cheapest model that passed", which goes through the release gate.
