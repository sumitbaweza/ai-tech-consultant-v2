"""Sources, reading, freshness and URL health (Tasks 2.1 to 2.4)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_brain import health
from knowledge_brain.api.deps import BusinessId, Db, UserId
from knowledge_brain.documents import DocumentError, add_document
from knowledge_brain.jobs import queue_embedding, recheck_source, start_source_run
from knowledge_brain.models import (
    Document,
    Image,
    Page,
    PageVersion,
    RefreshRun,
    RefreshRunUrl,
    Source,
)
from knowledge_brain.reading import (
    add_page_url,
    check_sitemap_url,
    crawler,
    duplicate_urls,
    plan_sitemaps,
    valid_url,
)

router = APIRouter(prefix="/v1", tags=["Sources and freshness"])


class UrlIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


class SitemapsIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=20)
    recheck_days: int = Field(default=1, description="1, 7, 15 or 30")
    miss_threshold: int = Field(
        default=2, ge=1, le=60, description="X: refreshes in a row before an action is proposed"
    )
    action_rule: str = Field(
        default="quarantine_then_archive", pattern="^(quarantine|archive|quarantine_then_archive)$"
    )
    rate: float = Field(default=1.0, gt=0, le=5, description="pages per second per site")


class StartIn(BaseModel):
    starter_pages: int | None = Field(
        default=None, ge=1, le=5000, description="read only the first N pages now"
    )
    continue_after_starter: bool = True


class SourcePatch(BaseModel):
    recheck_days: int | None = None
    miss_threshold: int | None = Field(default=None, ge=1, le=60)
    action_rule: str | None = Field(
        default=None, pattern="^(quarantine|archive|quarantine_then_archive)$"
    )


class PageIds(BaseModel):
    page_ids: list[uuid.UUID]


def _source(source: Source) -> dict[str, Any]:
    return {
        "id": source.id,
        "kind": source.kind,
        "label": source.label,
        "url": source.url,
        "status": source.status,
        "recheck_days": source.recheck_days,
        "next_check_at": source.next_check_at,
        "last_synced_at": source.last_synced_at,
        "miss_threshold": source.miss_threshold,
        "action_rule": source.action_rule,
        "error": source.error,
        "created_at": source.created_at,
    }


async def _get_source(db: AsyncSession, source_id: uuid.UUID) -> Source:
    source = await db.get(Source, source_id)
    if source is None:
        raise HTTPException(404, "No such source")
    return source


@router.post("/sources/sitemaps/check", summary="Check a sitemap URL as it is typed")
async def check_sitemap(body: UrlIn, bid: BusinessId) -> dict[str, Any]:
    async with crawler() as c:
        return await check_sitemap_url(body.url.strip(), c)


@router.post(
    "/sources/sitemaps", summary="Plan the read of one or more sitemaps (nothing is read yet)"
)
async def plan(body: SitemapsIn, bid: BusinessId, db: Db, user: UserId) -> dict[str, Any]:
    urls = [u.strip() for u in body.urls if u.strip()]
    if not all(valid_url(u) for u in urls):
        raise HTTPException(422, "Every sitemap URL must start with http:// or https://")
    async with crawler(body.rate) as c:
        source = await plan_sitemaps(
            db,
            bid,
            urls,
            c,
            user_id=user,
            recheck_days=body.recheck_days,
            miss_threshold=body.miss_threshold,
            action_rule=body.action_rule,
            rate=body.rate,
        )
    return {"source": _source(source), "plan": source.settings.get("plan")}


@router.get("/sources", summary="Every source, with its pages by health level")
async def list_sources(db: Db, kind: str | None = None) -> list[dict[str, Any]]:
    query = select(Source).order_by(Source.created_at)
    if kind:
        query = query.where(Source.kind == kind)
    out: list[dict[str, Any]] = []
    for source in (await db.execute(query)).scalars():
        item = _source(source)
        item["levels"] = await health.level_counts(db, source.id)
        item["pages"] = sum(item["levels"].values())
        out.append(item)
    return out


@router.get("/sources/{source_id}", summary="One source: settings, plan, levels, latest run")
async def get_source(source_id: uuid.UUID, db: Db) -> dict[str, Any]:
    source = await _get_source(db, source_id)
    latest = (
        await db.execute(
            select(RefreshRun)
            .where(RefreshRun.source_id == source_id)
            .order_by(RefreshRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        **_source(source),
        "plan": (source.settings or {}).get("plan"),
        "levels": await health.level_counts(db, source_id),
        "latest_run": _run(latest) if latest else None,
    }


@router.patch("/sources/{source_id}", summary="Change the recheck schedule, X or the action rule")
async def patch_source(source_id: uuid.UUID, body: SourcePatch, db: Db) -> dict[str, Any]:
    source = await _get_source(db, source_id)
    for field in ("recheck_days", "miss_threshold", "action_rule"):
        value = getattr(body, field)
        if value is not None:
            setattr(source, field, value)
    return _source(source)


@router.post("/sources/{source_id}/start", summary="Confirm the plan and start reading")
async def start(source_id: uuid.UUID, body: StartIn, bid: BusinessId, db: Db) -> dict[str, Any]:
    source = await _get_source(db, source_id)
    source.settings = {
        **(source.settings or {}),
        "continue_after_starter": body.continue_after_starter,
    }
    await db.commit()
    kind = "starter" if body.starter_pages else "first_read"
    run_id = await start_source_run(str(bid), str(source_id), kind, limit=body.starter_pages)
    return {"run_id": run_id, "kind": kind}


@router.post("/sources/{source_id}/recheck", summary="Recheck now, outside the schedule")
async def recheck_now(source_id: uuid.UUID, bid: BusinessId, db: Db) -> dict[str, str]:
    await _get_source(db, source_id)
    await recheck_source.defer_async(business_id=str(bid), source_id=str(source_id), kind="manual")
    return {"status": "queued"}


@router.delete("/sources/{source_id}", status_code=204, summary="Remove a source and its pages")
async def delete_source(source_id: uuid.UUID, db: Db) -> None:
    source = await _get_source(db, source_id)
    await db.execute(delete(Page).where(Page.source_id == source_id))
    await db.delete(source)


@router.get(
    "/sources/{source_id}/progress", summary="Live progress and time left of the current read"
)
async def progress(source_id: uuid.UUID, db: Db) -> dict[str, Any]:
    source = await _get_source(db, source_id)
    run = (
        await db.execute(
            select(RefreshRun)
            .where(RefreshRun.source_id == source_id)
            .order_by(RefreshRun.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return {"status": source.status, "total": 0, "done": 0, "percent": 0}
    done = int(
        await db.scalar(
            select(func.count()).select_from(RefreshRunUrl).where(RefreshRunUrl.run_id == run.id)
        )
        or 0
    )
    total = max(run.urls_total, done)
    rate = float((source.settings or {}).get("rate", 1.0))
    left = max(total - done, 0)
    return {
        "status": run.status,
        "kind": run.kind,
        "run_id": run.id,
        "total": total,
        "done": done,
        "failed": run.failed,
        "new": run.new_pages,
        "changed": run.changed,
        "percent": round(100 * done / total) if total else 100,
        "minutes_left": round(left / rate / 60, 1),
        "started_at": run.started_at,
    }


@router.get("/sources/{source_id}/pages", summary="The source's pages, by level")
async def source_pages(
    source_id: uuid.UUID,
    db: Db,
    level: str | None = None,
    q: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    query = select(Page).where(Page.source_id == source_id)
    if level:
        query = query.where(Page.level == level)
    if q:
        query = query.where(Page.url.ilike(f"%{q}%") | Page.title.ilike(f"%{q}%"))
    total = int(await db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (
        await db.execute(query.order_by(Page.url).offset(offset).limit(min(limit, 500)))
    ).scalars()
    return {"total": total, "pages": [_page(p) for p in rows]}


def _page(page: Page) -> dict[str, Any]:
    return {
        "id": page.id,
        "url": page.url,
        "title": page.title,
        "kind": page.kind,
        "origin": page.origin,
        "level": page.level,
        "miss_count": page.miss_count,
        "http_status": page.http_status,
        "fetch_state": page.fetch_state,
        "fetched_at": page.fetched_at,
        "changed_at": page.changed_at,
        "error": page.error,
    }


def _run(run: RefreshRun) -> dict[str, Any]:
    return {
        c: getattr(run, c)
        for c in (
            "id",
            "source_id",
            "kind",
            "status",
            "urls_total",
            "fetched",
            "new_pages",
            "changed",
            "unchanged",
            "failed",
            "missing",
            "moved",
            "recovered",
            "first_fetch_at",
            "last_fetch_at",
            "started_at",
            "finished_at",
        )
    }


@router.post("/page-urls", summary="Add one page URL; it is read at once and kept fresh")
async def add_page(body: UrlIn, bid: BusinessId, db: Db, user: UserId) -> dict[str, Any]:
    if not valid_url(body.url):
        raise HTTPException(422, "Enter a full address starting with http:// or https://")
    source = await add_page_url(db, bid, body.url, user_id=user)
    await db.commit()
    run_id = await start_source_run(str(bid), str(source.id), "first_read")
    return {"source": _source(source), "run_id": run_id}


@router.post("/documents", summary="Upload a PDF, DOCX, Markdown or text file")
async def upload(
    bid: BusinessId, db: Db, user: UserId, file: Annotated[UploadFile, File()]
) -> dict[str, Any]:
    data = await file.read()
    try:
        page = await add_document(db, bid, file.filename or "document.txt", data, user_id=user)
    except DocumentError as exc:
        raise HTTPException(422, str(exc)) from exc
    await db.commit()
    await queue_embedding(str(bid))
    return {"page_id": page.id, "title": page.title}


@router.get("/documents", summary="Uploaded documents")
async def documents(db: Db) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(Document, Page.level)
        .join(Page, Page.id == Document.page_id, isouter=True)
        .order_by(Document.created_at.desc())
    )
    return [
        {
            "id": d.id,
            "page_id": d.page_id,
            "filename": d.filename,
            "size_bytes": d.size_bytes,
            "created_at": d.created_at,
            "level": level,
        }
        for d, level in rows.all()
    ]


@router.get("/runs", summary="Refresh runs, newest first")
async def runs(db: Db, source_id: uuid.UUID | None = None, limit: int = 50) -> list[dict[str, Any]]:
    query = select(RefreshRun).order_by(RefreshRun.started_at.desc()).limit(min(limit, 200))
    if source_id:
        query = query.where(RefreshRun.source_id == source_id)
    return [_run(r) for r in (await db.execute(query)).scalars()]


@router.get("/runs/{run_id}", summary="One refresh run with its URL list")
async def run_detail(
    run_id: uuid.UUID, db: Db, offset: int = 0, limit: int = 2000
) -> dict[str, Any]:
    run = await db.get(RefreshRun, run_id)
    if run is None:
        raise HTTPException(404)
    urls = (
        await db.execute(
            select(RefreshRunUrl)
            .where(RefreshRunUrl.run_id == run_id)
            .order_by(RefreshRunUrl.id)
            .offset(offset)
            .limit(min(limit, 5000))
        )
    ).scalars()
    total = int(
        await db.scalar(
            select(func.count()).select_from(RefreshRunUrl).where(RefreshRunUrl.run_id == run_id)
        )
        or 0
    )
    return {
        **_run(run),
        "url_total": total,
        "urls": [
            {"url": u.url, "status": u.http_status, "outcome": u.outcome, "at": u.fetched_at}
            for u in urls
        ],
    }


@router.get("/health/pending", summary="URLs at or past X, with the action proposed (D4)")
async def pending(db: Db) -> list[dict[str, Any]]:
    return await health.pending_actions(db)


@router.post("/health/apply", summary="Apply the proposed action to these pending URLs")
async def apply(body: PageIds, db: Db, user: UserId) -> dict[str, int]:
    return {"applied": await health.apply_actions(db, body.page_ids, user)}


@router.post(
    "/pages/{page_id}/restore", summary="Put a quarantined or archived page back into answers"
)
async def restore(page_id: uuid.UUID, db: Db, user: UserId) -> dict[str, str]:
    page = await db.get(Page, page_id)
    if page is None:
        raise HTTPException(404)
    await health.restore(db, page, user)
    return {"level": page.level}


@router.get("/pages/{page_id}", summary="A page with its versions, health history and screenshots")
async def page_detail(page_id: uuid.UUID, db: Db) -> dict[str, Any]:
    page = await db.get(Page, page_id)
    if page is None:
        raise HTTPException(404)
    versions = (
        await db.execute(
            select(
                PageVersion.id,
                PageVersion.version,
                PageVersion.change,
                PageVersion.lines_added,
                PageVersion.lines_removed,
                PageVersion.created_at,
            )
            .where(PageVersion.page_id == page_id)
            .order_by(PageVersion.version.desc())
        )
    ).mappings()
    events = await health.history(db, page_id)
    imgs = (
        await db.execute(select(Image).where(Image.page_id == page_id).order_by(Image.position))
    ).scalars()
    return {
        **_page(page),
        "breadcrumb": page.breadcrumb,
        "content_md": page.content_md,
        "versions": [dict(v) for v in versions],
        "health": [
            {
                "event": e.event,
                "from": e.from_level,
                "to": e.to_level,
                "count": e.miss_count,
                "detail": e.detail,
                "at": e.created_at,
            }
            for e in events
        ],
        "images": [
            {
                "id": i.id,
                "position": i.position,
                "alt": i.alt,
                "state": i.state,
                "description": i.description,
                "described_by": i.described_by,
            }
            for i in imgs
        ],
    }


@router.get("/pages/{page_id}/diff", summary="What changed between two versions of a page")
async def page_diff(page_id: uuid.UUID, db: Db, a: int, b: int) -> dict[str, str]:
    import difflib

    versions = {
        v.version: v
        for v in (
            await db.execute(
                select(PageVersion).where(
                    PageVersion.page_id == page_id, PageVersion.version.in_((a, b))
                )
            )
        ).scalars()
    }
    if a not in versions or b not in versions:
        raise HTTPException(404)
    diff = "".join(
        difflib.unified_diff(
            versions[a].content_md.splitlines(keepends=True),
            versions[b].content_md.splitlines(keepends=True),
            f"v{a}",
            f"v{b}",
        )
    )
    return {"diff": diff}


@router.get("/changes", summary="Change history: pages that changed, newest first")
async def changes(db: Db, limit: int = 100) -> list[dict[str, Any]]:
    rows = await db.execute(
        text(
            """SELECT v.page_id, p.url, p.title, v.version, v.change, v.lines_added, v.lines_removed, v.created_at
           FROM brain.page_versions v JOIN brain.pages p ON p.id = v.page_id
           WHERE v.change <> 'new' ORDER BY v.created_at DESC LIMIT :n"""
        ),
        {"n": min(limit, 500)},
    )
    return [dict(row) for row in rows.mappings()]


@router.get("/archived", summary="The Archived Knowledge Base")
async def archived(db: Db) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(Page).where(Page.level == "archived").order_by(Page.level_changed_at.desc())
        )
    ).scalars()
    return [_page(p) | {"archived_at": p.level_changed_at} for p in rows]


@router.get("/duplicates", summary="URLs listed in more than one sitemap, with each sitemap")
async def duplicates(db: Db) -> list[dict[str, Any]]:
    return await duplicate_urls(db)


@router.get("/stats", summary="The dashboard numbers (Task 2.4)")
async def stats(db: Db) -> dict[str, Any]:
    row = (
        (
            await db.execute(
                text(
                    """SELECT
             count(*) FILTER (WHERE fetch_state = 'fetched') AS pages_processed,
             count(*) FILTER (WHERE fetch_state = 'failed') AS pages_failed,
             count(*) FILTER (WHERE fetch_state IN ('pending', 'discovered')) AS pages_pending,
             count(*) FILTER (WHERE level = 'archived') AS pages_archived
           FROM brain.pages"""
                )
            )
        )
        .mappings()
        .one()
    )
    embedded = await db.scalar(
        text(
            "SELECT count(DISTINCT page_id) FROM brain.chunks WHERE embedding IS NOT NULL AND page_id IS NOT NULL"
        )
    )
    chunked = await db.scalar(
        text("SELECT count(DISTINCT page_id) FROM brain.chunks WHERE page_id IS NOT NULL")
    )
    described = await db.scalar(
        select(func.count()).select_from(Image).where(Image.state == "described")
    )
    waiting = await db.scalar(
        select(func.count()).select_from(Image).where(Image.state == "pending")
    )
    last_sync = await db.scalar(select(func.max(Source.last_synced_at)))
    return {
        **dict(row),
        "pages_embedded": embedded or 0,
        "pages_indexed": chunked or 0,
        "duplicates_skipped": len(await duplicate_urls(db, limit=100000)),
        "images_described": described or 0,
        "images_waiting": waiting or 0,
        "pending_actions": len(await health.pending_actions(db)),
        "last_sync": last_sync.strftime("%d %b %H:%M") if last_sync else None,
    }
