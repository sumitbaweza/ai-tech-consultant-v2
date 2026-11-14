# STACK.md

**Language: Python 3.14 for the whole app** (async, type hints, pyright strict, ruff).
FastAPI, SQLAlchemy 2.1, Alembic, Jinja templates with HTMX 2.0.4, PostgreSQL 18 with pgvector
0.8.7, Procrastinate 3.11, Docker Compose. One command starts it all: `make up`
(`docker compose up -d --build`).

Why Python and not TypeScript: the Knowledge Brain is the part every other feature stands on, and
it carries the 98% pass bar. The v1 Brain (crawler, freshness, screenshot descriptions, hybrid
search, cited answers, 223 tests) is Python; porting it keeps what already works. The Self-Improve
Loop uses the Claude Agent SDK, which Python has. The cost: every screen is rebuilt as
server-rendered Jinja + HTMX, and v1's Gmail code (TypeScript) is rewritten.

How to read the numbers below: **measured** comes from the v1 crawl and snapshot of the
Brilliant Directories Solution Center (1,700 URLs, 1,541 articles, 6,443 described screenshots,
10,825 chunks). **Projected** is arithmetic from published prices and measured token counts, not
a model run: it is replaced by the Eval Lab's numbers when the benchmark runs (see EVALS.md).

| Part | Pick | Version |
| --- | --- | --- |
| Web and API | FastAPI + Uvicorn | 0.162 / 0.54 |
| Screens | Jinja2 + HTMX (vendored, no build step) | 3.1 / 2.0.4 |
| ORM and migrations | SQLAlchemy (async, psycopg 3) + Alembic | 2.1.8 / 1.20 |
| Database | PostgreSQL + pgvector + pg_trgm | 18.7 / 0.8.7 / 1.6 |
| Jobs and schedules | Procrastinate (jobs in Postgres) | 3.11 |
| Cache | `core/cache.py` interface, Postgres table behind it | - |
| Claude | `anthropic` SDK | 2.8 |
| Gemini, OpenRouter, Ollama | their REST APIs through `httpx` | 0.28 |
| Embeddings | Voyage `voyage-4-lite` (1,024 dims, stored as halfvec) | - |
| Self-Improve agent | Claude Agent SDK | 0.2.182 |
| Crawling | httpx + BeautifulSoup + protego (robots.txt) + markdownify | - |
| Documents | pypdf, python-docx | 7.1 / 1.2 |
| Tests and checks | pytest, pytest-asyncio, respx, ruff, pyright (strict) | 9.2 / 1.4 / 0.23 / 0.17 / 1.1.414 |

## Stack questions

**S1. Why no Redis at the start, and when to add Valkey.** Pick: Procrastinate, which keeps jobs in
Postgres (LISTEN/NOTIFY to wake workers, `SELECT ... FOR UPDATE SKIP LOCKED` to hand out jobs).
Why: one service fewer to run, back up and secure, and a job is written in the same transaction as
the data it works on, so "page saved, embed job lost" can't happen. Our load is small: the polite
crawl reads about 1 page a second (measured), and a chat answer makes 2-4 model calls that take
seconds each, so the queue's overhead is noise. Why not Redis now: a second store to keep
consistent, for no measured gain. What tells us it's time for Valkey: (1) p95 time from defer to
start above about 100 ms with idle workers; (2) Postgres CPU above about 70% where queue and cache
queries are a large share (`pg_stat_statements`); (3) `core.cache_entries` read more than about
500 times a second, or `procrastinate_jobs` bloating faster than autovacuum clears it. The cache
and the queue sit behind small interfaces (`core/cache.py`, `core/queue.py`), so Valkey replaces
the Postgres version without touching callers.

**S2. Raw SQL instead of the ORM.** Pick: SQLAlchemy ORM for ordinary reads and writes; raw SQL
(`text()` with bound parameters, results checked by Pydantic) for: hybrid search (one statement
with `websearch_to_tsquery`, `ts_rank_cd`, pgvector's `<=>` and reciprocal rank fusion), typo
fixing (`similarity()` from pg_trgm), the pending-URLs query (D4), the read queue's claim
(`UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING`), the cost dashboard
(`percentile_cont(0.95)` for p95 times), and the row-level security setup. Why: these use Postgres
features the ORM can't say clearly, they decide the app's speed, and as plain SQL they can be read,
explained and run in `psql` as they are. Why not raw SQL everywhere: typed models catch column
mistakes before runtime, and CRUD in SQL is just noise.

**S3. Crawling.** Pick: a plain HTTP client (httpx, HTTP/1.1, keep-alive) with BeautifulSoup, robots.txt
through protego, conditional requests (ETag and Last-Modified, so an unchanged page answers 304),
1 request a second per host, 2 workers, backoff on 429 and 5xx. Measured: the Solution Center's
1,700 URLs read in about 30 minutes with no page needing JavaScript. Why not a crawler framework
(Scrapy): its own event loop and pipeline model fight our async jobs, and we need only fetch,
parse and store. Why not a paid crawler: a cost per page and a third party that sees every
customer's help center. When a real browser is needed: when the main text is built by JavaScript
in the browser. The parser flags a page as "needs a browser" when the HTML has less than about
200 characters of main text but a script-only app shell (`<div id="root">`, `__NEXT_DATA__`). That
flag is shown on the Knowledge screen; reading those pages with Playwright is in ROADMAP.md, not
built.

**S4. Screenshots and documents.** Pick: files on local disk (the `storage` Docker volume), paths
and hashes in Postgres, behind a small storage interface. Why: 6,458 screenshots of about 100 KB
(measured) are about 650 MB; inside Postgres (bytea) they would bloat every backup and push hot
index pages out of memory, for no benefit, since we never query image bytes. Why not S3-type storage
yet: one more service and credentials, while one server is enough. When the app runs on two or more
servers, the same interface points at Cloudflare R2 (about $0.015 per GB-month with no egress
fees: 650 MB is about $0.01 a month) or MinIO.

**S5. Past emails.** Pick: the Gmail API, not IMAP. IMAP with OAuth needs the full-mail scope
(`https://mail.google.com/`: read, send and delete everything); the API lets us ask for less.
Scopes, exactly: `gmail.readonly` (read threads for Task 2.6; can't send or change anything),
`gmail.send` (only to send the weekly email to the owner, Task 5.4; can't read), `openid` and
`email` (to show which account is connected). How many at a time: `threads.list` in pages of 50,
then `threads.get` for at most 5 threads at once, the newest 200 threads per sync by default.
Never twice: each thread is stored with Gmail's `historyId` (unique on business and thread id);
a thread is fetched again only when its `historyId` changed.

**S6. Login.** Pick: our own email + password sign-in: scrypt hashes (`hashlib.scrypt`, n=2^14,
r=8, p=1, 16-byte salt), sessions stored in Postgres (the cookie holds a random token, the
database only its SHA-256), `HttpOnly` and `SameSite=Lax` cookies, a CSRF token on every form and
HTMX request. Why: a few hundred lines we can explain line by line, no extra service or bill, and
user data stays in our database. Why not Better Auth: it's TypeScript only. Why not Clerk or Supabase
Auth: an outside dependency with its own pricing, user records stored elsewhere, and another
connection beyond the brief's five. Google tokens: Google sign-in is used only to connect Gmail.
The refresh token is encrypted with AES-256-GCM (the `cryptography` package): the key comes from the
`SECRET_KEY` environment variable and is never in the database, every value gets a random 12-byte
nonce, and the business id is bound in as associated data, so a token copied into another
business's row fails to decrypt. Access tokens (1 hour) are never stored.

**S7. Hosting.** Pick for now: one VPS running the same Docker Compose file as development (4 vCPU,
8 GB RAM; about $8-20 a month at Hetzner, DigitalOcean or similar). Same command, same images, no
platform lock-in, and Postgres 18 with pgvector 0.8.7 is ours to pick. Render: a web service, two
workers and a managed Postgres with pgvector come to about $40-60 a month. Railway: usage-based,
about $20-50 a month at this size. Neon or Supabase for the database alone: about $19-25 a month for
paid plans; Procrastinate's LISTEN/NOTIFY needs a direct (not pooled) connection there. Move to a
managed Postgres when backups, point-in-time recovery and failover matter more than the bill.
(List prices, approximate; check before buying.)

## Model questions

The 14 models and prices are the brief's (checked by the brief on 12 Nov 2026). Token counts per
call are measured from v1 prompts: a cited answer reads about 4,100 tokens (8 chunks of about 317
tokens, the system prompt, role and instructions) and writes about 350.

**M1. Embeddings.** Pick: Voyage `voyage-4-lite`, $0.02 per million tokens with the first 200 million
free. Measured: the whole Solution Center (1,541 articles plus 6,443 screenshot descriptions) is
3.44 million tokens, so **about 2.2 million tokens and $0.045 per 1,000 pages**, and it embedded
without errors. Why not OpenAI: not one of the allowed providers. Why not Gemini embeddings: a second
paid dependency on the same Google key the Eval Lab already uses for free-tier tests, and Google's
free tier may keep data. Open-source (Qwen3-Embedding, BGE-M3) on Ollama: $0, private, but on this
laptop (2-core i3, 7.8 GB RAM, no GPU) embedding 10,825 chunks would take hours; it's the local-only
option to add when a GPU machine is available. Quality on our tests: the benchmark's retrieval cases
compare keyword-only, vector-only and hybrid + re-rank (`make eval-retrieval`); EVALS.md has the
numbers when that runs. Local-only mode skips vectors and uses keywords plus typo fixing.

**M2. Best model per step (projected until the Eval Lab runs).** Cost per 100 runs is arithmetic:
tokens per call x the brief's prices. Quality comes from the Eval Lab's judge, so it's left as
"to measure".

| Step | Candidates | Cost per 100 runs (projected) | Starting pick and why |
| --- | --- | --- | --- |
| Tag the job (300 in, 30 out) | Haiku 5.5, Qwen 3.7 Flash, Flash-Lite, Nemotron Lightning, qwen3.5:4b | Haiku $0.0045, Qwen 3.7 Flash $0.0013, Flash-Lite $0.0042, Lightning $0, qwen3.5:4b $0 | Rules first (most jobs need no model); Haiku for the rest: cheap, and no free-tier limits |
| Find facts (2,800 in, 150 out) | Haiku 5.5, Flash-Lite, qwen3.5:4b | Haiku $0.036, Flash-Lite $0.034, local $0 | Haiku: same price as Flash-Lite, and real customer text can go to it |
| Write the answer (4,100 in, 350 out) | Sonnet 5.5, Qwen 3.8 Max, Gemini 3.8 Flash, Gemini 3 Flash, Nemotron 3 Ultra, Gemma 4 31B, gemma4:e4b | Sonnet $1.17 ($0.87 with caching), Qwen 3.8 Max $1.03, Gemini 3.8 Flash $0.44, Gemini 3 Flash $0.31, Nemotron Ultra and Gemma 31B $0 | Sonnet: support replies carry the 98% bar; the router sends easy questions lower once the Eval Lab shows where quality holds |
| Read a screenshot | see M3 | see M3 | see M3 |
| Write a lesson / improve a skill (3,000 in, 800 out) | Opus 5.5 only | $2.80 | Opus, as the brief says; it runs a few times a day, not per question |
| Judge (1,200 in, 80 out) | Opus 5.5, Nemotron 3 Ultra (second opinion) | Opus $0.64, Nemotron $0 | Opus; Nemotron as the free second opinion; checked against 20 hand grades |

**M3. Reading screenshots.** Per 100 screenshots of about 1,200 x 800 pixels, with a 200-token prompt
and a 150-token description (projected from each provider's image-token rules and the brief's prices):
Claude Haiku 5.5 about $0.022 ($0.011 with batches; measured in v1: 6,443 screenshots described this
way); Gemini 3.8 Flash about $0.15; Gemini 3 Flash about $0.11; Nemotron 3.5 Super VL $0 (free,
rate-limited); gemma4:e4b $0, but slow on this laptop. The brief compares Gemini 3.8 Flash, Gemini 3
Flash, Super VL and gemma4:e4b; the Eval Lab's 20 screenshot-only questions decide. Starting pick:
Haiku 5.5, because the snapshot's 6,443 descriptions already come from it and it is the cheapest
paid option; Super VL is the free challenger.

**M4. Agents.** Pick: our own plain async Python for every pipeline (shield -> risk check -> question
finder -> search -> skill pick -> answer -> citation check), each step a function with typed inputs
and outputs, so every line can be explained and every step shows in the trace. The Claude Agent SDK
only for the Self-Improve Loop, which needs what a framework is worth having for: reading and
editing files, running tests, a sandboxed working directory, permission rules, a budget and long
autonomous loops. Why not LangGraph: a graph runtime and LangChain dependencies hide control flow in
pipelines that are straight lines with two branches (hand off, don't know).

**M5. Prompt caching.** What never changes between calls: the master system prompt (about 900
tokens), the role's prompt and tone (about 400), the business's instructions for that role (200 to
1,000). They go first, in a fixed order, with one cache breakpoint after them; the retrieved
knowledge and the question go after it. The newest Claude models cache prefixes from 512 tokens.
Projected saving on a Sonnet answer with 1,600 cached tokens out of 4,100: $0.0117 -> $0.0087 per
answer (cache reads cost $0.10 per million instead of $2), **about 26%**, more in long chats where the
history is cached too. The caching proof (Settings > Advanced > Caching proof) runs the same 100
answers with caching off, then on, and shows both totals and the % saved from the ledger.
