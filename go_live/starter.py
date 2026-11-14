"""Task 6.2: the starting test set, made from the business's own pages.

A spread of the pages read so far (sitemap pages, page URLs and documents) goes to the model one
page at a time; it writes questions a customer would ask, the correct answer from the page, and
the key facts. Each test keeps its source, so the Eval Lab can check the answer cites it.
The page text is the business's content: treated as data inside <page> tags, never as orders.
"""

import uuid
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from core.brain_client import BrainClient
from core.db import tenant_session
from core.llm.client import LlmClient
from core.llm.types import ChatTurn, LlmError, LlmRequest
from core.prompts import live_prompt, render
from eval_lab.models import Benchmark, Case

PAGE_CHARS = 6000  # enough for a help article; keeps one call around 2,000 tokens in


class StarterTest(BaseModel):
    question: str = Field(min_length=5, max_length=500)
    answer: str = Field(min_length=5, max_length=2000)
    key_facts: list[str] = Field(default_factory=list[str], max_length=8)


class StarterTests(BaseModel):
    tests: list[StarterTest] = Field(max_length=10)


def spread(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """`count` items evenly spread over the list, so the tests cover the whole help center."""
    if len(items) <= count:
        return items
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


async def pick_pages(
    brain: BrainClient, source_ids: list[uuid.UUID], count: int
) -> list[dict[str, Any]]:
    """Read pages (active, fetched) from the setup's sources, plus every uploaded document."""
    candidates: list[dict[str, Any]] = []
    for source_id in source_ids:
        listing = await brain.get(f"/v1/sources/{source_id}/pages", level="active", limit=500)
        candidates += [
            p
            for p in listing["pages"]
            if p.get("fetch_state") == "fetched" and p.get("kind") in ("article", "page")
        ]
    documents = [d for d in await brain.get("/v1/documents") if d.get("page_id")]
    picked = spread(candidates, max(count - len(documents), 1)) + [
        {"id": d["page_id"]} for d in documents
    ]
    pages: list[dict[str, Any]] = []
    for item in picked[:count]:
        page = await brain.get(f"/v1/pages/{item['id']}")
        if len(str(page.get("content_md") or "")) >= 200:
            pages.append(page)
    return pages


async def make_starter_tests(
    business_id: uuid.UUID,
    source_ids: list[uuid.UUID],
    *,
    pages: int = 15,
    per_page: int = 2,
    llm: LlmClient | None = None,
) -> tuple[uuid.UUID | None, int]:
    """Writes the tests and saves them as a "starter" benchmark. Returns (benchmark id, count)."""
    async with tenant_session(business_id) as session:
        system = await live_prompt(session, "eval.make_tests")
    client = llm or LlmClient(business_id)
    async with await BrainClient.for_business(business_id) as brain:
        chosen = await pick_pages(brain, source_ids, pages)
    rows: list[tuple[dict[str, Any], StarterTest]] = []
    for page in chosen:
        title, url = str(page.get("title") or "Untitled"), str(page.get("url") or "")
        content = str(page["content_md"])[:PAGE_CHARS]
        try:
            result = await client.call(
                LlmRequest(
                    business_id=business_id,
                    step="golive.make_tests",
                    job="make_tests",
                    system=[render(system.body, {"count": str(per_page)})],
                    messages=[
                        ChatTurn("user", f'<page title="{title}" url="{url}">\n{content}\n</page>')
                    ],
                    schema=StarterTests,
                    max_tokens=2000,
                    prompt_key=system.key,
                    prompt_version_id=system.version_id,
                    real_customer_data=True,
                )
            )
        except LlmError:
            continue  # one page failing doesn't stop the set; the count shows it
        if isinstance(result.parsed, StarterTests):
            rows += [(page, test) for test in result.parsed.tests[:per_page]]
    if not rows:
        return None, 0
    async with tenant_session(business_id) as session:
        made = int(
            await session.scalar(
                select(func.count()).select_from(Benchmark).where(Benchmark.kind == "starter")
            )
            or 0
        )
        benchmark = Benchmark(
            business_id=business_id,
            name="Starter tests",
            version=f"v{made + 1}",
            kind="starter",
            case_count=len(rows),
        )
        session.add(benchmark)
        await session.flush()
        for n, (page, test) in enumerate(rows, start=1):
            url = str(page.get("url") or "")
            facts = "; ".join(test.key_facts)
            checks: dict[str, Any] = {"must_answer": True}
            if url.startswith(("http://", "https://")):
                checks["cite_any"] = [url]
            session.add(
                Case(
                    business_id=business_id,
                    benchmark_id=benchmark.id,
                    case_key=f"ST-{n:03d}",
                    category="regular",
                    question=test.question,
                    expected=test.answer + (f" Key facts: {facts}." if facts else ""),
                    expected_sources=[url] if url else [],
                    checks=checks,
                )
            )
        return benchmark.id, len(rows)
