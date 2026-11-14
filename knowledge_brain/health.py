"""URL health levels (Task 2.4): Active, Watch, Quarantined, Archived.

- A URL that answers 404, or is missing from the sitemap, gets 1 added to its count at each
  refresh and moves to Watch (still in answers: it may be a hiccup).
- A 301 is followed: the new URL is indexed, the old one is quarantined at once.
- At X refreshes in a row (the source's miss_threshold) an action is *proposed*, never taken:
  the owner sees the pending URLs with their counts and applies them (D4's query below).
  With "quarantine, then archive", quarantine is proposed at X and archiving at 2X.
- A URL that works again, or comes back in the sitemap, goes back to 0 and Active.
- Quarantined and archived URLs never appear in answers; nothing is deleted: archived pages
  stay in the Archived Knowledge Base for audits and recovery.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_brain.models import HealthEvent, Page, Source
from knowledge_brain.search.index import set_page_searchable

# D4: the one query that shows pending URLs with their counts
PENDING_SQL = """
SELECT p.id, p.url, p.title, p.level, p.miss_count, p.http_status, p.moved_to_page_id,
       s.miss_threshold, s.action_rule, s.label AS source_label,
       CASE
         WHEN p.level = 'watch' AND s.action_rule = 'archive' THEN 'archive'
         WHEN p.level = 'watch' THEN 'quarantine'
         ELSE 'archive'
       END AS proposed_action
FROM brain.pages p
JOIN brain.sources s ON s.id = p.source_id
WHERE (p.level = 'watch' AND p.miss_count >= s.miss_threshold)
   OR (p.level = 'quarantined' AND s.action_rule = 'quarantine_then_archive'
       AND p.moved_to_page_id IS NULL AND p.miss_count >= 2 * s.miss_threshold)
ORDER BY p.miss_count DESC, p.url
"""


@dataclass
class Change:
    page: Page
    event: str
    to_level: str
    detail: str | None = None


async def record(
    session: AsyncSession,
    change: Change,
    *,
    run_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> None:
    page = change.page
    session.add(
        HealthEvent(
            business_id=page.business_id,
            page_id=page.id,
            run_id=run_id,
            event=change.event,
            from_level=page.level,
            to_level=change.to_level,
            miss_count=page.miss_count,
            detail=change.detail,
            created_by=user_id,
        )
    )
    if change.to_level != page.level:
        page.level = change.to_level
        page.level_changed_at = datetime.now(UTC)
        await set_page_searchable(session, page)


async def missed(
    session: AsyncSession,
    page: Page,
    event: str,
    *,
    run_id: uuid.UUID | None,
    detail: str | None = None,
) -> None:
    """A 404 or a URL missing from the sitemap: +1, and Watch if it was Active."""
    page.miss_count += 1
    to_level = "watch" if page.level == "active" else page.level
    await record(session, Change(page, event, to_level, detail), run_id=run_id)


async def recovered(session: AsyncSession, page: Page, *, run_id: uuid.UUID | None) -> bool:
    """It works again: back to 0 and Active. Returns True if the page had been unhealthy."""
    if page.level == "active" and page.miss_count == 0:
        return False
    page.miss_count = 0
    page.moved_to_page_id = None
    await record(session, Change(page, "recovered", "active"), run_id=run_id)
    return True


async def moved(
    session: AsyncSession, page: Page, new_page: Page, *, run_id: uuid.UUID | None
) -> None:
    """A 301: the old URL is quarantined at once, pointing at the new page."""
    page.moved_to_page_id = new_page.id
    await record(
        session, Change(page, "moved", "quarantined", f"301 to {new_page.url}"), run_id=run_id
    )


async def pending_actions(session: AsyncSession) -> list[dict[str, Any]]:
    return [dict(row) for row in (await session.execute(text(PENDING_SQL))).mappings()]


async def apply_actions(
    session: AsyncSession, page_ids: list[uuid.UUID], user_id: uuid.UUID | None
) -> int:
    """The owner approved these pending URLs: take the proposed action for each."""
    wanted = {str(page_id) for page_id in page_ids}
    applied = 0
    for row in await pending_actions(session):
        if str(row["id"]) not in wanted:
            continue
        page = await session.get(Page, row["id"])
        if page is None:
            continue
        action = str(row["proposed_action"])
        to_level = "archived" if action == "archive" else "quarantined"
        await record(
            session,
            Change(
                page,
                "archived" if action == "archive" else "quarantined",
                to_level,
                f"approved at {page.miss_count} misses",
            ),
            user_id=user_id,
        )
        applied += 1
    return applied


async def restore(session: AsyncSession, page: Page, user_id: uuid.UUID | None) -> None:
    """From the Archived Knowledge Base (or quarantine) back into answers."""
    page.miss_count = 0
    await record(session, Change(page, "restored", "active", "restored by hand"), user_id=user_id)


async def source_of(session: AsyncSession, page: Page) -> Source | None:
    return await session.get(Source, page.source_id) if page.source_id else None


async def level_counts(session: AsyncSession, source_id: uuid.UUID | None = None) -> dict[str, int]:
    query = text(
        "SELECT level, count(*) AS n FROM brain.pages "
        "WHERE (CAST(:source AS uuid) IS NULL OR source_id = :source) GROUP BY level"
    )
    rows = await session.execute(query, {"source": source_id})
    return {str(row.level): int(row.n) for row in rows}


async def history(session: AsyncSession, page_id: uuid.UUID) -> list[HealthEvent]:
    return list(
        (
            await session.execute(
                select(HealthEvent)
                .where(HealthEvent.page_id == page_id)
                .order_by(HealthEvent.created_at.desc())
            )
        ).scalars()
    )
