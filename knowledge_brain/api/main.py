"""The Knowledge Brain's API (Task 2.8): the only door to the Brain's data.

http://localhost:8081/docs documents every endpoint. Every request needs a business's API key
(Settings > Members and API keys): `Authorization: Bearer btk_...`. The key decides the business.

Quick start for another developer:

    curl -X POST http://localhost:8081/v1/ask \\
      -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \\
      -d '{"question": "How do I connect my own domain?"}'
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.db import dispose_engines
from knowledge_brain.api import answers, knowledge, sources
from knowledge_brain.worker import app as jobs_app

DESCRIPTION = """
The memory of one business: its sitemaps and page URLs, documents, instructions, skills, past
emails, approved lessons and experts' answers, kept fresh, searchable and citable.

**Authentication.** Send `Authorization: Bearer <key>` on every request. Create keys in the web
app under Settings > Members and API keys. A key belongs to one business and can only ever see
that business's knowledge.

**Main calls.** `POST /v1/ask` answers a question with citations (or says it doesn't know).
`POST /v1/search` finds knowledge. `GET /v1/chunks/{id}` opens what a citation points at.
`POST /v1/sources/sitemaps` then `POST /v1/sources/{id}/start` reads a whole site.

**Knowledge types** in every hit: sitemap, page_url, instruction, skill, past_email, document,
lesson, expert.

**Errors** are JSON `{"detail": "..."}` with 401 (bad key), 404 (not this business's, or gone),
422 (invalid input), 503 (a model or Gmail is unavailable; retry later).
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with jobs_app.open_async():  # so endpoints can start background jobs
        yield
    await dispose_engines()


app = FastAPI(
    title="Knowledge Brain API", version="2.0", description=DESCRIPTION, lifespan=lifespan
)
app.include_router(sources.router)
app.include_router(knowledge.router)
app.include_router(answers.router)


@app.get("/health", tags=["Health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
