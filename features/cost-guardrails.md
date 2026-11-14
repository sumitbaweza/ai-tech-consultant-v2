# Cost guardrails

> Draft written during the build. Replace or confirm it with what your Round 2 user test (THINKING question 7) shows.

**1. The problem, and for whom.** A business owner can't predict their model bill. One busy week, a long document or a loop of retries can cost 10 times a normal week, and they only find out from the invoice. Owners of small businesses are the most afraid of this, and fear of the bill stops them switching the chat on for the whole team.

**2. Who pays, and why.** The owner of a small or mid-size business on a fixed monthly budget, and agencies that run the app for several clients and resell it at a fixed price: a cap protects their margin.

**3. The user flow.**
1. In Settings, Business, the owner sets a monthly budget (e.g. $40) and what happens at 80% and 100%.
2. At 80% the owner gets an email (the weekly-email sender) and a banner shows "$32 of $40 used, 9 days left".
3. At 100%, the chosen rule applies: switch answers to the cheapest model that passed the last eval run, switch to local-only, or pause the chat with a polite message.
4. The Logs and costs screen shows the forecast for the month from the last 7 days.

**4. Screens and tabs (UX.md).** Settings, Business (screen 2) gets the budget fields; Logs and costs (screen 13) gets a forecast card; the banner sits in the shared layout.

**5. New tables and columns (DATABASE.md).** `core.businesses`: `monthly_budget_usd numeric(10,2)`, `budget_rule text` (downgrade, local_only, pause), `budget_state text`. `core.budget_events` (business_id, month, level 80 or 100, at, action taken).

**6. How it connects.** Reads the call ledger `core.model_calls` (every call already has its cost). The router (`core/llm/router.py`) gets one more rule: when the budget state is "over", pick the cheapest model that passed the latest Eval Lab run for that step (the Compare models data), or force local-only. The email uses the Gmail sender from Task 5.4.

**7. How we measure it.** Replay a month of logged calls with a budget at half their cost. **Done when:** the replay stays within 5% of the budget, the 80% email is sent once, and the downgraded answers still pass the chat benchmark at the release gate's level (no category drops more than the gate allows).

**8. Risks.** Quality silently drops at month end: the banner and the email say so, and the downgrade uses only models that passed the evals. A model call already running can overshoot: check before each call, accept a small overshoot. Prices change: prices live in one registry file with a date.

**9. Estimate.** 14 hours: ledger sums and forecast 4, router rule 3, settings and banner 3, email 1, tests and replay 3.
