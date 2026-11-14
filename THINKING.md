# Thinking

Three rounds, each committed on its own day. An earlier round is never edited later: Round 3 judges Round 1, and a wrong guess shown honestly is worth more than a right one hidden. Each answer: 3 to 8 lines or a small table, with a reason and a number wherever possible.

> **These answers are yours to write.** The notes under some questions are facts from the build (numbers and file names) to check your own answer against. Delete each note once you have answered.

## Round 1: plan before you code (date: ____ )

**1. Your app on one page.** One diagram of the whole app (screens, API, database, jobs, models) with arrows. Which part takes the whole app down if it stops, and how do you protect it?

_Your answer._

> Note: DESIGN.md section 1 has the built architecture; Postgres is the single point of failure by choice (data, search and the job queue).

**2. Your time guess.** Hours for Task 0 and each of the 6 features, in a table.

_Your answer._

**3. Your model guess.** For each job (find facts, pick the skill, write the answer, read a screenshot, turn a correction into a lesson, judge), the cheapest of the 14 models good enough, with one line of reason.

_Your answer._

> Note: the defaults in the code are in DESIGN.md section 3, with projected $ per 100 calls; none is measured yet.

**4. A sitemap you have never seen.** 3 ways another website could differ from the Solution Center, and what your design does about each.

_Your answer._

**5. Same kind of question, different risk.** 3 pairs like "copy of my invoice" vs "refund me now or I take legal action", and how the app tells them apart.

_Your answer._

> Note: `ask_teach_chat/safety.py`: rules for legal, refund and angry; unsure words (billing, cancel, my data...) go to a cheap model; measured 10 of 10 hand-offs and 0 false alarms on 140 normal questions (EVALS.md 3.3).

**6. 50 questions a day, or 5,000?** For 5,000 a day and 50,000 pages: what breaks first, gets slow or costs too much, with rough numbers.

_Your answer._

> Note: a projected Sonnet answer costs $0.0121 with fact-finding: 5,000 a day is about $60 a day before routing and caching. A polite crawl of 50,000 pages at 1 page per second takes about 14 hours.

## Round 2: while you build (date: ____ )

**7. A real person tries your app.** 10 minutes, one real job, no help. The 3 places they got stuck, and what you changed.

_Your answer._

**8. Your first feature idea.** From question 7: one feature this person would pay for. Name, who pays, why.

_Your answer._

> Note: features/ has 4 drafts; replace or confirm the first one with what question 7 shows.

**9. Your costliest step.** From Logs and costs: the step with the highest $ per 100 runs, the number, and how you will halve it.

_Your answer._

> Note: projected: writing the answer on Sonnet, $1.17 per 100; lessons and skill changes on Opus, $2.80 per 100 but rare. Caching projects to 39% less, routing to 55% less (EVALS.md 4.3 and 4.4).

**10. Try to trick your own agent.** One message, what the agent did, how it was stopped.

_Your answer._

**11. From a past email to a better skill.** A note on a thread, the skill change it made (Task 2.6), and one better (or worse) answer.

_Your answer._

**12. Your 4 feature ideas.** One line each; which first and why; which is riskiest and why.

_Your answer._

## Round 3: check your own answers (date: ____ )

**13. Back to question 1.** Your real architecture next to the Round 1 drawing. What changed, what was wrong, and how many hours it cost.

_Your answer._

**14. Back to question 2.** Guessed hours next to real hours per feature. Where were you most wrong, and why?

_Your answer._

**15. Back to question 3.** Model guesses next to Eval Lab results: wrong where, by how much in quality and $?

_Your answer._

**16. Back to question 9.** Did you halve the costliest step? Cost and quality, before and after.

_Your answer._

**17. Did Opus cheat?** Did Opus ever "fix" a case without really making the answer better? What did you see, and what rule did you add? If never, how do you know?

_Your answer._

> Note: rules already in place: protected paths put back after each round, a hidden set Opus never sees, a check that rejects copying test questions into the change, and every tool call logged (DESIGN.md section 4).

**18. 10 more days.** Which 3 things, in what order, and why that order?

_Your answer._

**19. The part you would rather not explain.** Which part of the code, and why? Fix it now, or say honestly why not.

_Your answer._
