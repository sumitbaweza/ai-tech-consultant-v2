# Help-center gap finder

> Draft written during the build. Replace or confirm it with what your Round 2 user test (THINKING question 7) shows.

**1. The problem, and for whom.** Every "I don't know" and every expert task is a question the help center doesn't answer. Today they sit in the Expert tasks list, one by one. A support lead can't see that 40 people asked about the same missing topic this month, so the help center never gets the page it needs, and the same questions keep reaching people.

**2. Who pays, and why.** The head of support or documentation at a software company with a public help center. Each answered-by-a-person question costs them about $3 to $8 of staff time; one new page that answers 40 questions a month pays for the feature many times over.

**3. The user flow.**
1. Once a week (or on demand) the app groups the last 30 days of "I don't know" answers and expert tasks into topics.
2. The Gaps screen lists topics by how often they were asked, with 3 example questions each and what the agent tried.
3. The owner opens a topic and clicks **Draft a page**: the app writes a help-page draft from the experts' answers in that topic, with the questions it must answer.
4. The owner edits and approves; the page is published into the Brain as a document (or exported as Markdown for their help center).
5. The app adds the topic's questions to the "Real feedback" benchmark, so the next eval run proves the gap is closed.

**4. Screens and tabs (Task 0 map, UX.md).** A new tab **Gaps** under Knowledge (screen 6), next to Documents; a "Gaps this week" card on the Dashboard (screen 5); the draft opens in the same editor as skills (6d).

**5. New tables and columns (DATABASE.md).** `brain.gap_topics` (id, business_id, title, question_count, first_seen, last_seen, status: open, drafted, closed); `brain.gap_questions` (topic_id, expert_task_id or chat answer id, question text); `brain.gap_drafts` (topic_id, markdown, status, published_page_id -> `brain.pages`). Row-level security like every Brain table.

**6. How it connects.** Reads `brain.expert_tasks` and the chat's "I don't know" answers (via a new Brain endpoint `GET /v1/gaps`); groups questions with the existing Voyage embeddings (or trigram similarity without a key); drafts with a new prompt `brain.draft_gap_page` on Sonnet; publishes through `POST /v1/documents`; adds cases to the feedback benchmark like Task 4.4.

**7. How we measure it.** Before and after a page is published: the share of that topic's questions answered with a citation. **Done when:** on the Solution Center demo, 3 topics are found from at least 20 seeded questions, one page is drafted and published, and the "I don't know" rate on that topic's questions drops from 100% to under 20% in an Eval Lab run.

**8. Risks.** Grouping noise (two topics merged): show the example questions so a person can split them. Drafts that invent facts: the draft may only use the experts' answers and cites them; a person approves every page. Personal data in questions: the same masking as the chat.

**9. Estimate.** 22 hours: grouping and tables 8, screens 6, draft prompt and publish 4, evals and tests 4.
