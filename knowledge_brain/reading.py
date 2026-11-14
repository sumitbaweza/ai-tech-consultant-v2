"""Reading the knowledge: plan a sitemap, read every page once politely, recheck on schedule.

Task 2.2: adding sitemaps finds every nested sitemap and every URL, marks duplicates, and makes
a plan (totals, first and last URL per sitemap, pages per hour and per day, the finish time).
Nothing is read until the user confirms.

Task 2.3: the read runs as background jobs of 25 pages each, one job at a time per source (a
Procrastinate lock), at 1 request a second per site. Every page's state is saved as it is
read, so a job that stops halfway (a crash, a restart, the site going down) is retried and
carries on with the pages still pending: nothing is read twice, nothing is lost.

Task 2.4: a recheck reads the sitemaps again, adds new URLs, counts missing ones, and fetches
every page with its ETag and Last-Modified: an unchanged page answers 304 and costs nothing.
Changed pages are read again, versioned, re-chunked and re-embedded.
"""

import difflib
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from knowledge_brain import health
from knowledge_brain.crawler.fetch import FetchBlocked, Fetched, Fetcher, FetchError
from knowledge_brain.crawler.http import make_client
from knowledge_brain.crawler.parse import ParsedPage, parse_page, without_query
from knowledge_brain.crawler.ratelimit import HostRateLimiter
from knowledge_brain.crawler.robots import RobotsRules
from knowledge_brain.crawler.sitemap import FoundSitemap, SitemapError, discover, parse_sitemap
from knowledge_brain.models import (
    Image,
    Page,
    PageSitemap,
    PageVersion,
    RefreshRun,
    RefreshRunUrl,
    Sitemap,
    Source,
)
from knowledge_brain.search.index import index_page

BATCH_SIZE = 25
CLAIM_TIMEOUT = timedelta(minutes=10)  # a claim older than this means its job died
MAX_PAGE_ATTEMPTS = 3


@dataclass
class Crawler:
    client: httpx.AsyncClient
    limiter: HostRateLimiter
    robots: RobotsRules
    fetcher: Fetcher


@asynccontextmanager
async def crawler(
    rate: float | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> AsyncGenerator[Crawler]:
    settings = get_settings()
    client = make_client(transport)
    limiter = HostRateLimiter(rate or settings.crawler_requests_per_second)
    robots = RobotsRules(client, settings.crawler_user_agent, limiter)
    try:
        yield Crawler(client, limiter, robots, Fetcher(client, limiter, robots))
    finally:
        await client.aclose()


def valid_url(url: str) -> bool:
    parts = urlsplit(url.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


# ---------------------------------------------------------------- Task 2.2: plan the read


async def check_sitemap_url(url: str, c: Crawler) -> dict[str, Any]:
    """Checked as soon as it's entered: does it answer, and is it a sitemap?"""
    if not valid_url(url):
        return {"ok": False, "error": "Enter a full address starting with http:// or https://"}
    try:
        async with c.client.stream("GET", url, follow_redirects=True) as response:
            if response.status_code != 200:
                return {"ok": False, "error": f"The server answered HTTP {response.status_code}"}
            data = await response.aread()
        parsed = parse_sitemap(data)
    except (httpx.HTTPError, SitemapError) as exc:
        return {"ok": False, "error": str(exc)}
    kind = "sitemap index" if parsed.is_index else "sitemap"
    count = len(parsed.children) if parsed.is_index else len(parsed.pages)
    noun = "sitemaps" if parsed.is_index else "URLs"
    return {"ok": True, "kind": kind, "count": count, "message": f"A {kind} with {count:,} {noun}."}


def speed(rate: float, total: int) -> dict[str, Any]:
    per_hour = int(rate * 3600)
    seconds = total / rate if rate > 0 else 0
    finish = datetime.now(UTC) + timedelta(seconds=seconds)
    return {
        "pages_per_hour": per_hour,
        "pages_per_day": per_hour * 24,
        "minutes": round(seconds / 60, 1),
        "finish_at": finish.isoformat(),
    }


async def plan_sitemaps(
    session: AsyncSession,
    business_id: uuid.UUID,
    urls: list[str],
    c: Crawler,
    *,
    user_id: uuid.UUID | None,
    recheck_days: int = 1,
    miss_threshold: int = 2,
    action_rule: str = "quarantine_then_archive",
    rate: float = 1.0,
) -> Source:
    """Find every nested sitemap and URL and save them as a planned source (nothing read)."""
    found = await discover(urls, c.client, c.robots, c.limiter)
    source = Source(
        business_id=business_id,
        kind="sitemap",
        label=urls[0],
        url=urls[0],
        status="planned",
        recheck_days=recheck_days,
        miss_threshold=miss_threshold,
        action_rule=action_rule,
        settings={"rate": rate},
        created_by=user_id,
    )
    session.add(source)
    await session.flush()
    await save_sitemaps(session, business_id, source, found)
    source.settings = {"rate": rate, "plan": await make_plan(session, source, found, rate)}
    return source


async def save_sitemaps(
    session: AsyncSession, business_id: uuid.UUID, source: Source, found: list[FoundSitemap]
) -> None:
    by_url: dict[str, Sitemap] = {}
    for item in found:
        existing = (
            await session.execute(select(Sitemap).where(Sitemap.url == item.url))
        ).scalar_one_or_none()
        sitemap = existing or Sitemap(business_id=business_id, source_id=source.id, url=item.url)
        sitemap.parent_id = by_url[item.parent_url].id if item.parent_url in by_url else None
        sitemap.is_index, sitemap.depth = item.is_index, item.depth
        sitemap.url_count = len(item.pages)
        sitemap.first_url = item.pages[0].url if item.pages else None
        sitemap.last_url = item.pages[-1].url if item.pages else None
        sitemap.last_checked_at = datetime.now(UTC)
        sitemap.last_error = item.error
        session.add(sitemap)
        await session.flush()
        by_url[item.url] = sitemap
        if item.pages:
            await _upsert_listed_pages(session, business_id, source, sitemap, item)


async def _upsert_listed_pages(
    session: AsyncSession,
    business_id: uuid.UUID,
    source: Source,
    sitemap: Sitemap,
    item: FoundSitemap,
) -> None:
    unique: dict[str, Any] = {}
    for entry in item.pages:
        unique.setdefault(entry.url, entry)
    rows = [
        {
            "business_id": business_id,
            "source_id": source.id,
            "url": url,
            "origin": "sitemap",
            "fetch_state": "discovered",
            "sitemap_lastmod": entry.lastmod,
        }
        for url, entry in unique.items()
    ]
    for start in range(0, len(rows), 1000):
        await session.execute(
            insert(Page)
            .values(rows[start : start + 1000])
            .on_conflict_do_nothing(index_elements=[Page.business_id, Page.url])
        )
    ids: dict[str, uuid.UUID] = {}
    urls = list(unique)
    for start in range(0, len(urls), 5000):
        rows = await session.execute(
            select(Page.url, Page.id).where(Page.url.in_(urls[start : start + 5000]))
        )
        ids.update({row[0]: row[1] for row in rows.all()})
    links = [
        {
            "business_id": business_id,
            "page_id": ids[url],
            "sitemap_id": sitemap.id,
            "position": position,
            "lastmod": unique[url].lastmod,
        }
        for position, url in enumerate(unique)
        if url in ids
    ]
    now = datetime.now(UTC)
    for start in range(0, len(links), 1000):
        statement = insert(PageSitemap).values(links[start : start + 1000])
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[PageSitemap.page_id, PageSitemap.sitemap_id],
                set_={
                    "last_seen_at": now,
                    "removed_at": None,
                    "position": statement.excluded.position,
                },
            )
        )


async def make_plan(
    session: AsyncSession, source: Source, found: list[FoundSitemap], rate: float
) -> dict[str, Any]:
    sitemaps = [
        {
            "url": s.url,
            "parent": s.parent_url,
            "depth": s.depth,
            "is_index": s.is_index,
            "urls": len(s.pages),
            "first_url": s.pages[0].url if s.pages else None,
            "last_url": s.pages[-1].url if s.pages else None,
            "error": s.error,
        }
        for s in found
    ]
    total_listed = sum(len(s.pages) for s in found)
    duplicates = await duplicate_urls(session, limit=200)
    unique_total = int(
        await session.scalar(
            select(func.count()).select_from(Page).where(Page.source_id == source.id)
        )
        or 0
    )
    return {
        "sitemaps": sitemaps,
        "sitemap_count": len(found),
        "urls_listed": total_listed,
        "unique_urls": unique_total,
        "duplicates": duplicates,
        "duplicate_count": len(duplicates),
        "first_url": next((s["first_url"] for s in sitemaps if s["first_url"]), None),
        "last_url": next((s["last_url"] for s in reversed(sitemaps) if s["last_url"]), None),
        **speed(rate, unique_total),
    }


async def duplicate_urls(session: AsyncSession, limit: int = 500) -> list[dict[str, Any]]:
    """URLs listed in more than one sitemap, with every sitemap they're in (Task 2.2, D3)."""
    rows = await session.execute(
        text(
            """
        SELECT p.url, array_agg(s.url ORDER BY s.url) AS sitemaps
        FROM brain.page_sitemaps ps
        JOIN brain.pages p ON p.id = ps.page_id
        JOIN brain.sitemaps s ON s.id = ps.sitemap_id
        WHERE ps.removed_at IS NULL
        GROUP BY p.url HAVING count(*) > 1
        ORDER BY p.url LIMIT :n
        """
        ),
        {"n": limit},
    )
    return [{"url": row.url, "sitemaps": list(row.sitemaps)} for row in rows]


async def add_page_url(
    session: AsyncSession,
    business_id: uuid.UUID,
    url: str,
    *,
    user_id: uuid.UUID | None,
    recheck_days: int = 7,
) -> Source:
    """Task 2.1: a single page, read, kept fresh and health-checked like sitemap pages."""
    url = url.strip()
    source = Source(
        business_id=business_id,
        kind="page_url",
        label=url,
        url=url,
        status="planned",
        recheck_days=recheck_days,
        created_by=user_id,
    )
    session.add(source)
    await session.flush()
    await session.execute(
        insert(Page)
        .values(
            business_id=business_id,
            source_id=source.id,
            url=url,
            origin="page_url",
            fetch_state="discovered",
        )
        .on_conflict_do_nothing()
    )
    return source


# ---------------------------------------------------------------- Task 2.3: read politely


async def new_run(session: AsyncSession, source: Source, kind: str) -> RefreshRun:
    run = RefreshRun(business_id=source.business_id, source_id=source.id, kind=kind)
    session.add(run)
    await session.flush()
    return run


async def start_run(
    session: AsyncSession,
    source: Source,
    *,
    kind: str,
    limit: int | None = None,
    run: RefreshRun | None = None,
) -> RefreshRun:
    """Confirm the plan (or a recheck): queue the source's pages for this run.

    A first read takes the pages not read yet. A recheck takes every page the sitemap still
    lists (or the single page URL): a URL missing from the sitemap recovers only by coming back.
    """
    run = run or await new_run(session, source, kind)
    waiting = select(Page.id).where(
        Page.source_id == source.id, Page.origin.in_(("sitemap", "page_url"))
    )
    if kind in ("first_read", "starter"):
        waiting = waiting.where(Page.fetch_state.in_(("discovered", "failed")))
    else:
        listed = select(PageSitemap.page_id).where(PageSitemap.removed_at.is_(None))
        waiting = waiting.where(
            Page.level != "archived", or_(Page.origin == "page_url", Page.id.in_(listed))
        )
    # Sitemap order (so a starter slice takes the site's own first pages); a page listed in two
    # sitemaps counts once
    ordered = (
        waiting.join(PageSitemap, PageSitemap.page_id == Page.id, isouter=True)
        .group_by(Page.id, Page.url)
        .order_by(func.min(PageSitemap.position).nulls_last(), Page.url)
    )
    if limit:
        ordered = ordered.limit(limit)
    ids = [row[0] for row in (await session.execute(ordered)).all()]
    for start in range(0, len(ids), 1000):
        await session.execute(
            update(Page)
            .where(Page.id.in_(ids[start : start + 1000]))
            .values(fetch_state="pending", run_id=run.id, claimed_at=None, attempts=0)
        )
    run.urls_total = len(ids)
    source.status = "reading"
    return run


async def claim_batch(session: AsyncSession, run_id: uuid.UUID) -> list[Page]:
    """Take the next pages of the run. A claim older than CLAIM_TIMEOUT belonged to a job that
    died, so its pages are taken again: that is how a stopped read resumes."""
    stale = datetime.now(UTC) - CLAIM_TIMEOUT
    ids = (
        (
            await session.execute(
                text(
                    """
        UPDATE brain.pages SET claimed_at = now()
        WHERE id IN (
            SELECT id FROM brain.pages
            WHERE run_id = :run AND fetch_state = 'pending' AND (claimed_at IS NULL OR claimed_at < :stale)
            ORDER BY url LIMIT :n FOR UPDATE SKIP LOCKED
        )
        RETURNING id
        """
                ),
                {"run": run_id, "stale": stale, "n": BATCH_SIZE},
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        return []
    return list((await session.execute(select(Page).where(Page.id.in_(ids)))).scalars())


async def read_page(session: AsyncSession, page: Page, run: RefreshRun, c: Crawler) -> str:
    """Fetch and store one page; returns its outcome for the run's URL list."""
    now = datetime.now(UTC)
    run.first_fetch_at = run.first_fetch_at or now
    try:
        fetched = await c.fetcher.get(page.url, etag=page.etag, last_modified=page.last_modified)
    except FetchBlocked as exc:
        page.fetch_state, page.error = "failed", str(exc)
        return _log_url(session, run, page, None, "failed")
    except FetchError as exc:
        page.attempts += 1
        page.error = str(exc)
        page.fetch_state = "failed" if page.attempts >= MAX_PAGE_ATTEMPTS else "pending"
        page.claimed_at = None
        if page.fetch_state == "failed":
            run.failed += 1
            return _log_url(session, run, page, None, "failed")
        return "retry"
    run.last_fetch_at = datetime.now(UTC)
    run.fetched += 1
    page.http_status, page.fetched_at, page.fetch_state, page.error = (
        fetched.status,
        now,
        "fetched",
        None,
    )
    if fetched.status == 304:
        run.unchanged += 1
        outcome = (
            "recovered" if await health.recovered(session, page, run_id=run.id) else "unchanged"
        )
        run.recovered += outcome == "recovered"
        return _log_url(session, run, page, 304, outcome)
    if fetched.status in (301, 308) and fetched.location:
        new_page = await _follow_move(session, page, fetched.location, run)
        await health.moved(session, page, new_page, run_id=run.id)
        run.moved += 1
        return _log_url(session, run, page, fetched.status, "moved")
    if fetched.status in (404, 410):
        await health.missed(
            session, page, "not_found", run_id=run.id, detail=f"HTTP {fetched.status}"
        )
        return _log_url(session, run, page, fetched.status, "not_found")
    if fetched.status != 200:
        page.fetch_state, page.error = "failed", f"HTTP {fetched.status}"
        run.failed += 1
        return _log_url(session, run, page, fetched.status, "failed")
    outcome = await store_content(session, page, fetched, run)
    if await health.recovered(session, page, run_id=run.id):
        run.recovered += 1
        outcome = "recovered" if outcome == "unchanged" else outcome
    return _log_url(session, run, page, 200, outcome)


async def _follow_move(session: AsyncSession, page: Page, location: str, run: RefreshRun) -> Page:
    """Task 2.4: a 301 is followed: the new URL becomes its own page, read in this run."""
    await session.execute(
        insert(Page)
        .values(
            business_id=page.business_id,
            source_id=page.source_id,
            url=location,
            origin=page.origin,
            fetch_state="pending",
            run_id=run.id,
        )
        .on_conflict_do_update(
            index_elements=[Page.business_id, Page.url],
            set_={"fetch_state": "pending", "run_id": run.id, "claimed_at": None},
        )
    )
    new_page = (await session.execute(select(Page).where(Page.url == location))).scalar_one()
    run.urls_total += 1
    return new_page


async def store_content(
    session: AsyncSession, page: Page, fetched: Fetched, run: RefreshRun | None
) -> str:
    if "html" not in fetched.content_type and fetched.content_type:
        page.kind = "listing"  # a PDF or an image in a sitemap: tracked, not read as a page
        return "unchanged"
    parsed = parse_page(fetched.body, fetched.url)
    page.etag, page.last_modified = fetched.etag, fetched.last_modified
    if page.content_hash == parsed.content_hash:
        if run is not None:
            run.unchanged += 1
        return "unchanged"
    is_new = page.content_hash is None
    await save_version(session, page, parsed, run)
    await sync_images(session, page, parsed)
    page.title, page.breadcrumb, page.content_md = parsed.title, parsed.breadcrumb, parsed.markdown
    page.kind = "article" if parsed.is_article else "listing"
    page.content_hash, page.changed_at = parsed.content_hash, datetime.now(UTC)
    if parsed.needs_browser:
        page.error = (
            "This page builds its text with JavaScript; it needs a browser to read (not built yet)."
        )
    await index_page(session, page)
    if run is not None:
        if is_new:
            run.new_pages += 1
        else:
            run.changed += 1
    return "new" if is_new else "changed"


async def save_version(
    session: AsyncSession, page: Page, parsed: ParsedPage, run: RefreshRun | None
) -> None:
    """D5: a full copy each time the hash changes, with lines added and removed counted."""
    last = (
        await session.execute(
            select(PageVersion)
            .where(PageVersion.page_id == page.id)
            .order_by(PageVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    added = removed = 0
    if last is not None:
        for line in difflib.ndiff(last.content_md.splitlines(), parsed.markdown.splitlines()):
            added += line.startswith("+ ")
            removed += line.startswith("- ")
    session.add(
        PageVersion(
            business_id=page.business_id,
            page_id=page.id,
            version=(last.version + 1) if last else 1,
            change="changed" if last else "new",
            content_hash=parsed.content_hash,
            title=parsed.title,
            content_md=parsed.markdown,
            lines_added=added,
            lines_removed=removed,
            run_id=run.id if run else None,
        )
    )


async def sync_images(session: AsyncSession, page: Page, parsed: ParsedPage) -> None:
    """Keep each screenshot's description when the image is unchanged; queue new ones."""
    old = {
        without_query(image.src_url): image
        for image in (
            await session.execute(select(Image).where(Image.page_id == page.id))
        ).scalars()
    }
    for image in old.values():
        await session.delete(image)
    await session.flush()
    for ref in parsed.images:
        before = old.get(without_query(ref.url))
        session.add(
            Image(
                business_id=page.business_id,
                page_id=page.id,
                position=ref.position,
                src_url=ref.url,
                alt=ref.alt,
                context=ref.context,
                sha256=before.sha256 if before else None,
                storage_path=before.storage_path if before else None,
                description=before.description if before else None,
                described_by=before.described_by if before else None,
                state=before.state if before and before.state == "described" else "pending",
            )
        )
    await session.flush()


def _log_url(
    session: AsyncSession, run: RefreshRun, page: Page, status: int | None, outcome: str
) -> str:
    session.add(
        RefreshRunUrl(
            business_id=page.business_id,
            run_id=run.id,
            page_id=page.id,
            url=page.url,
            http_status=status,
            outcome=outcome,
        )
    )
    return outcome


async def remaining(session: AsyncSession, run_id: uuid.UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Page)
            .where(Page.run_id == run_id, Page.fetch_state == "pending")
        )
        or 0
    )


async def finish_run(session: AsyncSession, run: RefreshRun, source: Source) -> None:
    run.status, run.finished_at = "done", datetime.now(UTC)
    source.status, source.last_synced_at = "ready", datetime.now(UTC)
    if source.recheck_days:
        source.next_check_at = datetime.now(UTC) + timedelta(days=source.recheck_days)


# ---------------------------------------------------------------- Task 2.4: recheck


async def recheck_sitemaps(
    session: AsyncSession, source: Source, run: RefreshRun, c: Crawler
) -> None:
    """Read the sitemaps again: new URLs are added, missing ones counted (missing = +1, Watch)."""
    if source.kind != "sitemap" or not source.url:
        return
    found = await discover([source.url], c.client, c.robots, c.limiter)
    if any(
        item.error and not item.is_index and not item.pages
        for item in found
        if item.parent_url is None
    ):
        return  # the sitemap itself didn't answer: don't count every URL as missing
    listed = {entry.url for item in found for entry in item.pages}
    now = datetime.now(UTC)
    await save_sitemaps(session, source.business_id, source, found)
    gone = (
        (
            await session.execute(
                select(Page).where(
                    Page.source_id == source.id,
                    Page.origin == "sitemap",
                    Page.url.not_in(listed) if listed else Page.id.is_not(None),
                    Page.level != "archived",
                )
            )
        )
        .scalars()
        .all()
    )
    for page in gone:
        await session.execute(
            update(PageSitemap)
            .where(PageSitemap.page_id == page.id, PageSitemap.removed_at.is_(None))
            .values(removed_at=now)
        )
        await health.missed(
            session, page, "missing", run_id=run.id, detail="no longer in the sitemap"
        )
        _log_url(session, run, page, None, "missing")
        run.missing += 1
