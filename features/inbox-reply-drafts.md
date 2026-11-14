# Inbox reply drafts with earned auto-send

> Draft written during the build. Replace or confirm it with what your Round 2 user test (THINKING question 7) shows.

**1. The problem, and for whom.** Today the team copies a customer's email into the chat, then copies the answer back into Gmail. For a support person answering 60 emails a day that is 2 copy-pastes per email, and the answers the chat would write are never measured against what the person really sent.

**2. Who pays, and why.** Support teams of 2 to 20 people who live in Gmail. A ready draft in the thread saves about 2 to 4 minutes per email; at 60 emails a day that is 2 to 4 hours a day per person.

**3. The user flow.**
1. New emails in the connected support inbox are read every few minutes.
2. Each is checked by the same shield and hand-off rules as the chat: angry, refund, legal and tricks are labelled for a person and never drafted automatically.
3. The others get a cited draft, saved as a Gmail draft in the same thread.
4. The person edits and sends from Gmail. The app compares the sent reply with its draft.
5. A kind of email (intent x product area) whose drafts are sent unchanged 20 times in a row earns auto-send, with a 5-minute undo window and 1 in 10 sampled for review. Any edit resets the count.

**4. Screens and tabs (UX.md).** A new top-level **Inbox** screen (between Chat and Lessons): queue, draft, status per kind; Settings, Keys already holds the Gmail connection (it needs the `gmail.compose` scope added).

**5. New tables and columns (DATABASE.md).** `chat.inbox_emails` (gmail ids, from, subject, kind, risk, status), `chat.inbox_drafts` (email_id, answer_id -> `chat.answers`, gmail_draft_id, sent_text, edit_distance, unchanged), `chat.autosend_kinds` (kind, streak, earned_at, paused).

**6. How it connects.** Reuses the chat pipeline (`ask_teach_chat/pipeline.py`) for drafts, the shield (`safety.py`) for risk, lessons from edits (a big edit proposes a lesson, as a correction in the chat does), the Gmail connection (`core/google.py`), and the Eval Lab: each kind's sent replies become cases in the "Real feedback" benchmark.

**7. How we measure it.** Share of drafts sent unchanged per kind, minutes saved, and complaints on auto-sent emails. **Done when:** on 200 fixture emails, no angry, refund or legal email is auto-sent (0 of the 30 in the set), drafts for the rest pass the chat benchmark's bar, and one kind earns auto-send after 20 unchanged sends in the replay.

**8. Risks.** A wrong auto-sent email reaches a customer: only kinds with a perfect 20-email streak, never risky ones, an undo window, and sampling. Gmail API quotas: batch reads, back off on 429. Personal data in emails: masked before any model sees it.

**9. Estimate.** 34 hours: Gmail read and drafts 10, kinds and streaks 8, screen 6, learning from edits 4, fixtures, evals and tests 6. Depends on the cost guardrails (auto-send multiplies volume).
