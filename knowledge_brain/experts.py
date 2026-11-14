"""Ask a human when the agent can't answer (Task 3.2): the expert's answer flows back into the
memory as a page (origin expert), so the next search finds and cites it."""

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_brain.crawler.parse import content_hash
from knowledge_brain.models import ExpertTask, Page
from knowledge_brain.search.index import index_page


def question_key(question: str) -> str:
    """Lower case, no punctuation, single spaces: the same question asked again matches."""
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", question.lower()).split())[:500]


async def open_task(
    session: AsyncSession,
    business_id: uuid.UUID,
    question: str,
    reason: str,
    context: dict[str, str],
) -> ExpertTask:
    key = question_key(question)
    task = (
        await session.execute(
            select(ExpertTask).where(ExpertTask.question_key == key, ExpertTask.status == "open")
        )
    ).scalar_one_or_none()
    if task is not None:
        task.asked_count += 1
        task.last_asked_at = datetime.now(UTC)
        return task
    task = ExpertTask(
        business_id=business_id,
        question=question[:4000],
        question_key=key,
        reason=reason,
        context=context,
    )
    session.add(task)
    await session.flush()
    return task


async def answer_task(
    session: AsyncSession, task: ExpertTask, answer: str, user_id: uuid.UUID | None
) -> Page:
    title = f"Expert answer: {task.question[:150]}"
    body = f"**Question:** {task.question}\n\n**Answer:** {answer.strip()}"
    page = Page(
        business_id=task.business_id,
        url=f"expert://{task.id}",
        origin="expert",
        kind="article",
        title=title,
        content_md=body,
        fetch_state="fetched",
        content_hash=content_hash(title, body, []),
    )
    session.add(page)
    await session.flush()
    await index_page(session, page)
    task.status, task.answer, task.answered_by = "answered", answer.strip(), user_id
    task.answered_at, task.answer_page_id = datetime.now(UTC), page.id
    return page
