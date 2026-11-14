# Answers in the customer's language

> Draft written during the build. Replace or confirm it with what your Round 2 user test (THINKING question 7) shows.

**1. The problem, and for whom.** The help center is in English; many of a business's customers and support staff are not. A Spanish question today gets an English answer, or a poor one, because keyword search can't match Spanish words to English pages.

**2. Who pays, and why.** Businesses selling in several countries with one English help center: translating the help center costs thousands of dollars per language and goes stale; answering in the customer's language from the English source costs a few cents per question.

**3. The user flow.**
1. The owner ticks the languages the team and customers use (Settings, Business).
2. A question in Spanish is detected as Spanish, searched in English (translated query plus meaning search), and answered in Spanish, citing the English page.
3. The answer shows "answered in Spanish from English sources" and offers the English original.
4. Corrections in Spanish make lessons like any other.

**4. Screens and tabs (UX.md).** Settings, Business gets a language list; the chat message (screen 7) shows the language badge and an "English original" toggle; the Eval Lab gets a language filter on runs.

**5. New tables and columns (DATABASE.md).** `core.businesses.languages text[]`; `chat.answers.language`, `chat.answers.source_language`; `evals.cases.language` (default 'en').

**6. How it connects.** One new cheap step `translate_query` (Haiku) before search, the existing `write_answer` prompt gets the answer language as a variable (a new prompt version through the release gate), meaning search works across languages with Voyage's multilingual embeddings, and the benchmark gets a translated copy of 30 chat cases per language.

**7. How we measure it.** Per language, the same pass rate as English on the translated cases. **Done when:** on 30 Spanish and 30 German translations of the chat cases, the pass rate is within 5 points of English, every trick and hand-off case is still caught (the shield must work in those languages too), and the cost per answer rises by less than 10%.

**8. Risks.** The shield's rules are English-only: tricks and angry messages in other languages need the cheap risk model, or translated rules, before this ships. Wrong-language answers in mixed messages. Terms that must not be translated (product names): a glossary per business.

**9. Estimate.** 26 hours: detection and query translation 5, prompt and answer language 4, shield in other languages 7, benchmark translations and evals 6, screens 4. Riskiest of the four, because of the shield.
