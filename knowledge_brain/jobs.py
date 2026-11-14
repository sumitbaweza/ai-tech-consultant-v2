"""The Knowledge Brain's background jobs (Procrastinate, queue "brain").

Every job retries with backoff (core/jobs.py RETRY). Reads run one batch at a time per source
(the lock), each page in its own transaction, so progress is saved page by page.
"""

import contextlib
import uuid

from procrastinate import Blueprint
from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy import text

from core.db import engine, tenant_session
from core.jobs import RETRY
from core.models import Business
from knowledge_brain import emails, images
from knowledge_brain.models import Page, RefreshRun, Source
from knowledge_brain.reading import (
    claim_batch,
    crawler,
    finish_run,
    new_run,
    read_page,
    recheck_sitemaps,
    remaining,
    start_run,
)
from knowledge_brain.search.embed import embedder_for
from knowledge_brain.search.index import embed_pending

bp = Blueprint()


def lock_for(source_id: uuid.UUID | str) -> str:
    return f"read:{source_id}"


async def _rate(business_id: uuid.UUID) -> tuple[float, bool]:
    async with tenant_session(business_id, role="brain") as session:
        business = await session.get(Business, business_id)
        advanced = business.advanced if business else {}
        return float(advanced.get("crawl_rate", 1.0)), bool(business and business.local_only)


@bp.task(name="read_batch", queue="brain", retry=RETRY)
async def read_batch(business_id: str, run_id: str) -> None:
    """Read the next 25 pages of a run, then queue the next batch (or finish the run)."""
    bid, rid = uuid.UUID(business_id), uuid.UUID(run_id)
    rate, _ = await _rate(bid)
    async with tenant_session(bid, role="brain") as session:
        run = await session.get(RefreshRun, rid)
        if run is None or run.status != "running":
            return
        pages = [page.id for page in await claim_batch(session, rid)]
        source_id = run.source_id
    async with crawler(rate) as c:
        for page_id in pages:
            async with tenant_session(bid, role="brain") as session:
                page, run = await session.get(Page, page_id), await session.get(RefreshRun, rid)
                if page is not None and run is not None:
                    await read_page(session, page, run, c)
    async with tenant_session(bid, role="brain") as session:
        left = await remaining(session, rid)
        run, source = await session.get(RefreshRun, rid), await session.get(Source, source_id)
        if run is None or source is None:
            return
        if left:
            await read_batch.configure(lock=lock_for(source_id)).defer_async(
                business_id=business_id, run_id=run_id
            )
            return
        await finish_run(session, run, source)
        continue_after_starter = run.kind == "starter" and bool(
            (source.settings or {}).get("continue_after_starter")
        )
    await queue_descriptions(business_id)
    await queue_embedding(business_id)
    if continue_after_starter:
        await start_source_run(business_id, str(source_id), "first_read")


async def start_source_run(
    business_id: str, source_id: str, kind: str, limit: int | None = None
) -> str:
    """Queue a run for a source and its first batch. Used by the API (confirm, recheck now)."""
    bid = uuid.UUID(business_id)
    async with tenant_session(bid, role="brain") as session:
        source = await session.get(Source, uuid.UUID(source_id))
        assert source is not None
        run = await start_run(session, source, kind=kind, limit=limit)
        run_id = str(run.id)
    await read_batch.configure(lock=lock_for(source_id)).defer_async(
        business_id=business_id, run_id=run_id
    )
    return run_id


@bp.task(name="recheck_source", queue="brain", retry=RETRY)
async def recheck_source(business_id: str, source_id: str, kind: str = "recheck") -> None:
    """Task 2.4: read the sitemaps again (new and missing URLs), then fetch every listed page."""
    bid = uuid.UUID(business_id)
    rate, _ = await _rate(bid)
    async with tenant_session(bid, role="brain") as session:
        source = await session.get(Source, uuid.UUID(source_id))
        if source is None:
            return
        run = await new_run(session, source, kind)
        async with crawler(rate) as c:
            await recheck_sitemaps(session, source, run, c)
        await start_run(session, source, kind=kind, run=run)
        run_id = str(run.id)
    await read_batch.configure(lock=lock_for(source_id)).defer_async(
        business_id=business_id, run_id=run_id
    )


@bp.periodic(cron="* * * * *")
@bp.task(name="schedule_rechecks", queue="brain")
async def schedule_rechecks(timestamp: int) -> None:
    """Every minute: queue a recheck for each source whose schedule is due, in every business."""
    async with engine("brain").connect() as connection:
        due = (
            await connection.execute(
                text("SELECT business_id, source_id FROM brain.due_sources(now())")
            )
        ).all()
    for business_id, source_id in due:
        async with tenant_session(business_id, role="brain") as session:
            source = await session.get(Source, source_id)
            if source is None:
                continue
            source.next_check_at = None  # set again when the run finishes
            source.status = "reading"
        await recheck_source.defer_async(business_id=str(business_id), source_id=str(source_id))


@bp.task(name="describe_images", queue="brain", retry=RETRY)
async def describe_images(business_id: str) -> None:
    bid = uuid.UUID(business_id)
    rate, _ = await _rate(bid)
    async with crawler(rate) as c:
        for _ in range(500):  # 20 screenshots per round; stops early when none are left
            done, stopped = await images.describe_pending(bid, c)
            if stopped or done == 0:
                break
    await queue_embedding(business_id)


@bp.task(name="embed_chunks", queue="brain", retry=RETRY)
async def embed_chunks(business_id: str) -> None:
    bid = uuid.UUID(business_id)
    _, local_only = await _rate(bid)
    embedder = embedder_for(bid, local_only=local_only)
    if embedder is None:
        return  # no Voyage key, or local-only: search works on keywords and typo fixing
    while True:
        async with tenant_session(bid, role="brain") as session:
            done = await embed_pending(session, embedder)
        if done == 0:
            return


@bp.task(name="sync_gmail", queue="brain", retry=RETRY)
async def sync_gmail(business_id: str) -> None:
    await emails.sync_threads(uuid.UUID(business_id))
    await queue_embedding(business_id)


@bp.task(name="improve_from_notes", queue="brain", retry=RETRY)
async def improve_from_notes(business_id: str, user_id: str | None = None) -> None:
    await emails.improve_from_notes(
        uuid.UUID(business_id), user_id=uuid.UUID(user_id) if user_id else None
    )


async def queue_embedding(business_id: str) -> None:
    """Embed the business's new chunks soon. One waiting job per business is enough: if one is
    already queued, it will pick these chunks up too."""
    with contextlib.suppress(AlreadyEnqueued):
        await embed_chunks.configure(queueing_lock=f"embed:{business_id}").defer_async(
            business_id=business_id
        )


async def queue_descriptions(business_id: str) -> None:
    """Describe the business's waiting screenshots soon (one waiting job per business)."""
    with contextlib.suppress(AlreadyEnqueued):
        await describe_images.configure(queueing_lock=f"describe:{business_id}").defer_async(
            business_id=business_id
        )
