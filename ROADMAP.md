# Roadmap: 4 feature ideas, in build order

> Draft written during the build. Confirm the order after your Round 2 user test (THINKING question 7).

| Order | Idea | Hours | Depends on | Why this place |
| ---: | --- | ---: | --- | --- |
| 1 | [Help-center gap finder](features/help-center-gap-finder.md) | 22 | nothing new | Uses data the app already collects ("I don't know" answers and expert tasks), has the lowest risk (a person approves every page), and every page it adds raises the readiness score for all the other features. |
| 2 | [Cost guardrails](features/cost-guardrails.md) | 14 | the call ledger and the Eval Lab (built) | Small, and it removes the owner's biggest fear before volume grows. It must exist before auto-send (idea 3) multiplies the number of calls. |
| 3 | [Inbox reply drafts with earned auto-send](features/inbox-reply-drafts.md) | 34 | idea 2; Gmail (built) | The biggest time saver, but the first feature that can send something to a customer without a person: it needs the guardrails and a mature benchmark first. |
| 4 | [Answers in the customer's language](features/multilingual-answers.md) | 26 | ideas 1 and 3 benefit | The riskiest: the shield's rules are English-only, so tricks and angry messages in other languages need new work and new evals before it can ship. |

Total: 96 hours, about 2.5 weeks for one developer. The riskiest idea is last on purpose: by then the benchmark, the gap finder and the guardrails make a regression visible before customers see it.
