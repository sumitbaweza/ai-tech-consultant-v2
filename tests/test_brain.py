"""Feature 2: parsing any site, chunking, the polite read, freshness and URL health levels,
search with typos, and the Brain API's per-business keys. The fixture site stands in for a real
help center, in-process: no network, no model."""

import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import func, select

from benchmark.url_site.app import STATE
from benchmark.url_site.app import app as fixture_app
from core.db import tenant_session
from knowledge_brain import health
from knowledge_brain.crawler.parse import parse_page
from knowledge_brain.models import Chunk, Page, RefreshRun, Source
from knowledge_brain.reading import (
    Crawler,
    claim_batch,
    crawler,
    finish_run,
    new_run,
    plan_sitemaps,
    read_page,
    recheck_sitemaps,
    remaining,
    start_run,
)
from knowledge_brain.search.chunk import MAX_CHARS, chunk_page
from knowledge_brain.search.search import search
from tests.conftest import make_business

BASE = "http://fixture-site:8083"


def test_parse_any_page_keeps_the_article_and_drops_the_chrome() -> None:
    html = """<html><head><title>Ship faster | Acme Blog</title></head><body>
      <header><a href="/">Acme</a><nav>Products Pricing Blog</nav></header>
      <div class="cookie-banner">We use cookies</div>
      <ol class="breadcrumbs"><li><a href="/">Home</a></li><li><a href="/blog">Blog</a></li><li>Shipping</li></ol>
      <main><article><h1>How we ship orders in 2 days</h1>
        <p>Every order placed before 2 pm ships the same day from our warehouse in Ohio, and the rest the next morning.</p>
        <p>Tracking numbers are emailed as soon as the label prints, so customers can follow the parcel.</p>
        <img src="/img/label.png" alt="The label screen"><img src="/pixel.gif" width="1" height="1">
      </article></main>
      <aside class="related-posts">Related: returns policy</aside>
      <footer>Copyright Acme</footer></body></html>"""
    parsed = parse_page(html, "https://acme.test/blog/shipping")
    assert parsed.title == "How we ship orders in 2 days"
    assert parsed.breadcrumb == "Blog > Shipping"
    assert "same day from our warehouse" in parsed.markdown
    assert "cookies" not in parsed.markdown and "Copyright" not in parsed.markdown
    assert "Related" not in parsed.markdown
    assert [i.url for i in parsed.images] == [
        "https://acme.test/img/label.png"
    ]  # the pixel is skipped
    assert "[Screenshot 1: The label screen]" in parsed.markdown
    assert parsed.is_article and not parsed.needs_browser


def test_a_page_of_links_is_a_listing_and_an_app_shell_needs_a_browser() -> None:
    links = "".join(f'<li><a href="/a/{n}">Article number {n}</a></li>' for n in range(40))
    listing = parse_page(
        f"<html><body><main><h1>Folder</h1><ul>{links}</ul></main></body></html>",
        "https://x.test/f",
    )
    assert not listing.is_article
    shell = parse_page(
        '<html><body><div id="root"></div><script src="/app.js"></script></body></html>',
        "https://x.test/",
    )
    assert shell.needs_browser


def test_chunks_stay_in_size_and_remember_their_paragraphs() -> None:
    paragraphs = [f"Paragraph {n}. " + "Words about the settings page. " * 12 for n in range(30)]
    markdown = (
        "## Setup\n\n"
        + "\n\n".join(paragraphs[:15])
        + "\n\n## Troubleshooting\n\n"
        + "\n\n".join(paragraphs[15:])
    )
    chunks = chunk_page(markdown, "Help > Domains")
    assert len(chunks) > 3
    assert all(len(c.content) <= MAX_CHARS + 200 for c in chunks)
    assert all(c.content.startswith("Help > Domains") for c in chunks)
    first, last = chunks[0].anchor.split("-")[1:], chunks[-1].anchor.split("-")[1:]
    assert first[0] == "0" and int(last[1]) == 31  # 30 paragraphs + 2 headings, numbered 0..31


# ---------------------------------------------------------------- the polite read, end to end


@pytest.fixture
async def site() -> AsyncGenerator[Crawler]:
    STATE["day"] = 1
    async with crawler(rate=1000, transport=httpx.ASGITransport(app=fixture_app)) as c:
        yield c
    STATE["day"] = 1


async def read_all(business_id: uuid.UUID, run_id: uuid.UUID, c: Crawler) -> None:
    """What the read_batch job does, batch after batch, without the queue."""
    while True:
        async with tenant_session(business_id, role="brain") as session:
            batch = [page.id for page in await claim_batch(session, run_id)]
        if not batch:
            break
        for page_id in batch:
            async with tenant_session(business_id, role="brain") as session:
                page, run = await session.get(Page, page_id), await session.get(RefreshRun, run_id)
                assert page is not None and run is not None
                await read_page(session, page, run, c)
    async with tenant_session(business_id, role="brain") as session:
        assert await remaining(session, run_id) == 0
        run = await session.get(RefreshRun, run_id)
        assert run is not None
        source = await session.get(Source, run.source_id)
        assert source is not None
        await finish_run(session, run, source)


async def recheck(business_id: uuid.UUID, source_id: uuid.UUID, c: Crawler) -> uuid.UUID:
    async with tenant_session(business_id, role="brain") as session:
        source = await session.get(Source, source_id)
        assert source is not None
        run = await new_run(session, source, "recheck")
        await recheck_sitemaps(session, source, run, c)
        await start_run(session, source, kind="recheck", run=run)
        run_id = run.id
    await read_all(business_id, run_id, c)
    return run_id


async def page_by_url(business_id: uuid.UUID, path: str) -> Page:
    async with tenant_session(business_id, role="brain") as session:
        page = (await session.execute(select(Page).where(Page.url == BASE + path))).scalar_one()
        return page


@pytest.mark.db
async def test_plan_read_recheck_and_every_health_level(site: Crawler) -> None:
    business_id = await make_business()
    # Plan: the index and its 2 nested sitemaps, the duplicate, nothing read yet
    async with tenant_session(business_id, role="brain") as session:
        source = await plan_sitemaps(
            session,
            business_id,
            [f"{BASE}/sitemap_index.xml"],
            site,
            user_id=None,
            recheck_days=1,
            miss_threshold=2,
            rate=1000,
        )
        plan = source.settings["plan"]
        source_id = source.id
    assert plan["sitemap_count"] == 3 and plan["urls_listed"] == 9 and plan["unique_urls"] == 8
    assert plan["duplicate_count"] == 1 and plan["duplicates"][0]["url"] == f"{BASE}/articles/1"
    assert plan["pages_per_hour"] == 3_600_000 and plan["first_url"] == f"{BASE}/articles/1"

    # First read
    async with tenant_session(business_id, role="brain") as session:
        source = await session.get(Source, source_id)
        assert source is not None
        run = await start_run(session, source, kind="first_read")
        assert run.urls_total == 8
        run_id = run.id
    await read_all(business_id, run_id, site)
    async with tenant_session(business_id, role="brain") as session:
        fetched = await session.scalar(
            select(func.count()).select_from(Page).where(Page.fetch_state == "fetched")
        )
        chunks = await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.searchable.is_(True))
        )
        found = await search(session, "custm domian")  # two typos
    assert fetched == 8 and chunks and chunks >= 8
    assert found.corrected_query is not None and "domain" in found.corrected_query
    assert found.hits and found.hits[0].title == "Add a custom domain"

    # Day 2: a 404, a 301 and a page missing from the sitemap
    STATE["day"] = 2
    await recheck(business_id, source_id, site)
    p3, p4, p5 = [await page_by_url(business_id, f"/articles/{n}") for n in (3, 4, 5)]
    new4 = await page_by_url(business_id, "/articles/4-new")
    assert (p3.level, p3.miss_count, p3.http_status) == ("watch", 1, 404)
    assert p4.level == "quarantined" and p4.moved_to_page_id == new4.id and new4.level == "active"
    assert (p5.level, p5.miss_count) == ("watch", 1)

    # Day 3: second miss in a row: X = 2, so actions are proposed, not taken
    STATE["day"] = 3
    await recheck(business_id, source_id, site)
    async with tenant_session(business_id, role="brain") as session:
        pending = await health.pending_actions(session)
    proposed = {row["url"]: row["proposed_action"] for row in pending}
    assert proposed == {f"{BASE}/articles/3": "quarantine", f"{BASE}/articles/5": "quarantine"}
    p3 = await page_by_url(business_id, "/articles/3")
    async with tenant_session(business_id, role="brain") as session:
        assert await health.apply_actions(session, [p3.id], None) == 1
        hits = (await search(session, "export the member list csv")).hits
    assert all(hit.page_id != p3.id for hit in hits)  # quarantined: never in answers

    # Day 4: /articles/3 works again, /articles/5 is back in the sitemap
    STATE["day"] = 4
    await recheck(business_id, source_id, site)
    p3, p5 = (
        await page_by_url(business_id, "/articles/3"),
        await page_by_url(business_id, "/articles/5"),
    )
    assert (p3.level, p3.miss_count) == ("active", 0)
    assert (p5.level, p5.miss_count) == ("active", 0)
    async with tenant_session(business_id, role="brain") as session:
        hits = (await search(session, "export the member list csv")).hits
    assert hits and hits[0].page_id == p3.id


# ---------------------------------------------------------------- the API: one key, one business


@pytest.mark.db
async def test_brain_api_keys_decide_the_business() -> None:
    from knowledge_brain.api.main import app as brain_app
    from knowledge_brain.worker import app as jobs_app
    from workspace.service import create_api_key

    a, b = await make_business("Alpha"), await make_business("Beta")
    keys: dict[uuid.UUID, str] = {}
    for business_id in (a, b):
        async with tenant_session(business_id) as session:
            keys[business_id] = (
                await create_api_key(session, business_id, name="test", user_id=None)
            ).key
    async with (
        jobs_app.open_async(),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=brain_app), base_url="http://brain"
        ) as http,
    ):
        assert (await http.get("/v1/sources")).status_code == 401
        assert (
            await http.get("/v1/sources", headers={"Authorization": "Bearer btk_wrong"})
        ).status_code == 401
        made = await http.post(
            "/v1/instructions",
            headers={"Authorization": f"Bearer {keys[a]}"},
            json={"title": "Alpha secret", "body": "Alpha's private refund rule about zebras."},
        )
        assert made.status_code == 200, made.text
        instruction_id = made.json()["id"]
        b_auth = {"Authorization": f"Bearer {keys[b]}"}
        assert (await http.get("/v1/instructions", headers=b_auth)).json() == []
        hits = (
            await http.post("/v1/search", headers=b_auth, json={"query": "zebras refund rule"})
        ).json()["hits"]
        assert hits == []
        # B can't change A's instruction even knowing its id
        response = await http.put(
            f"/v1/instructions/{instruction_id}",
            headers=b_auth,
            json={"title": "hacked", "body": "hacked"},
        )
        assert response.status_code == 404
        a_hits = (
            await http.post(
                "/v1/search",
                headers={"Authorization": f"Bearer {keys[a]}"},
                json={"query": "zebras refund rule"},
            )
        ).json()["hits"]
        assert a_hits and a_hits[0]["knowledge_type"] == "instruction"
