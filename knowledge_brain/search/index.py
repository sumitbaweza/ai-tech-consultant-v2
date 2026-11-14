"""Keep the search index (brain.chunks) in step with the knowledge.

Every knowledge type ends up as chunks with a knowledge_type, so one search covers them all and
every answer can say which type it used (Task 2.1). Building chunks is free; embedding them
(Voyage) is a separate step, embed_pending(), that a job runs in batches.
"""

import uuid
from typing import Any

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from knowledge_brain.models import (
    SEARCHABLE_LEVELS,
    Chunk,
    EmailMessage,
    EmailThread,
    Image,
    Instruction,
    LessonDoc,
    Page,
    SkillVersion,
)
from knowledge_brain.search.chunk import ChunkText, chunk_page, image_chunk, place, text_chunk
from knowledge_brain.search.embed import Embedder

ORIGIN_TYPE = {
    "sitemap": "sitemap",
    "page_url": "page_url",
    "document": "document",
    "expert": "expert",
}


def page_searchable(page: Page) -> bool:
    return page.level in SEARCHABLE_LEVELS and page.kind == "article"


async def _add(
    session: AsyncSession,
    business_id: uuid.UUID,
    items: list[tuple[ChunkText, dict[str, Any]]],
    knowledge_type: str,
    searchable: bool,
) -> list[int]:
    rows = [
        Chunk(
            business_id=business_id,
            knowledge_type=knowledge_type,
            position=position,
            heading=chunk.heading[:500],
            anchor=chunk.anchor,
            content=chunk.content,
            token_count=chunk.token_count,
            searchable=searchable,
            **refs,
        )
        for position, (chunk, refs) in enumerate(items)
    ]
    session.add_all(rows)
    await session.flush()
    ids = [row.id for row in rows]
    await update_terms(session, business_id, ids)
    return ids


async def index_page(session: AsyncSession, page: Page) -> int:
    """(Re)build a page's text chunks and its described screenshots' chunks."""
    await session.execute(delete(Chunk).where(Chunk.page_id == page.id))
    if page.kind != "article" or not page.content_md:
        page.embedded_hash = page.content_hash
        return 0
    where = place(page.breadcrumb, page.title)
    items: list[tuple[ChunkText, dict[str, Any]]] = [
        (chunk, {"page_id": page.id}) for chunk in chunk_page(page.content_md, where)
    ]
    images = (
        (
            await session.execute(
                select(Image)
                .where(Image.page_id == page.id, Image.state == "described")
                .order_by(Image.position)
            )
        )
        .scalars()
        .all()
    )
    for image in images:
        if image.description and image.description.strip().lower() != "decorative":
            items.append(
                (
                    image_chunk(where, image.position, image.alt, image.description),
                    {"page_id": page.id, "image_id": image.id},
                )
            )
    ids = await _add(
        session, page.business_id, items, ORIGIN_TYPE[page.origin], page_searchable(page)
    )
    page.embedded_hash = page.content_hash
    return len(ids)


async def set_page_searchable(session: AsyncSession, page: Page) -> None:
    """After a level change: quarantined and archived pages leave answers at once (Task 2.4)."""
    await session.execute(
        update(Chunk).where(Chunk.page_id == page.id).values(searchable=page_searchable(page))
    )


async def index_instruction(session: AsyncSession, instruction: Instruction) -> None:
    await session.execute(delete(Chunk).where(Chunk.instruction_id == instruction.id))
    chunk = text_chunk(f"Instruction: {instruction.title}", instruction.body)
    await _add(
        session,
        instruction.business_id,
        [(chunk, {"instruction_id": instruction.id})],
        "instruction",
        instruction.enabled,
    )


async def index_skill_version(session: AsyncSession, version: SkillVersion, name: str) -> None:
    """Only the live version of a skill is searchable; older versions keep no chunks."""
    await session.execute(
        delete(Chunk).where(
            Chunk.skill_version_id.in_(
                select(SkillVersion.id).where(SkillVersion.skill_id == version.skill_id)
            )
        )
    )
    if version.status == "live":
        chunk = text_chunk(f"Skill: {name}", version.markdown)
        await _add(
            session, version.business_id, [(chunk, {"skill_version_id": version.id})], "skill", True
        )


async def index_email_thread(session: AsyncSession, thread: EmailThread) -> None:
    messages = (
        (
            await session.execute(
                select(EmailMessage)
                .where(EmailMessage.thread_id == thread.id)
                .order_by(EmailMessage.position)
            )
        )
        .scalars()
        .all()
    )
    await session.execute(delete(Chunk).where(Chunk.email_message_id.in_([m.id for m in messages])))
    items = [
        (
            text_chunk(
                f"Past email: {thread.subject} > message {m.position + 1} from {m.from_addr}",
                m.body_text[:4000],
                f"msg-{m.position}",
            ),
            {"email_message_id": m.id},
        )
        for m in messages
        if m.body_text.strip()
    ]
    if items:
        await _add(session, thread.business_id, items, "past_email", True)


async def index_lesson(session: AsyncSession, lesson: LessonDoc) -> None:
    await session.execute(delete(Chunk).where(Chunk.lesson_doc_id == lesson.id))
    chunk = text_chunk(f"Lesson: {lesson.title}", lesson.markdown)
    await _add(session, lesson.business_id, [(chunk, {"lesson_doc_id": lesson.id})], "lesson", True)


async def update_terms(session: AsyncSession, business_id: uuid.UUID, chunk_ids: list[int]) -> None:
    """Add the chunks' words to the business's vocabulary, which typo fixing compares with.

    The 'simple' text-search config keeps words as written (no stemming), so a misspelled
    question word can be matched to a real word from the knowledge.
    """
    if not chunk_ids:
        return
    await session.execute(
        text(
            """
            INSERT INTO brain.terms (business_id, term, doc_count)
            SELECT :business, word, count(*)
            FROM (
                SELECT DISTINCT c.id, w.word
                FROM brain.chunks c,
                     unnest(tsvector_to_array(to_tsvector('simple', c.content))) AS w(word)
                WHERE c.id = ANY(:ids)
            ) words
            WHERE length(word) BETWEEN 3 AND 30 AND word ~ '^[a-z]+$'
            GROUP BY word
            ON CONFLICT (business_id, term)
            DO UPDATE SET doc_count = brain.terms.doc_count + EXCLUDED.doc_count
            """
        ),
        {"business": business_id, "ids": chunk_ids},
    )


async def embed_pending(session: AsyncSession, embedder: Embedder, limit: int = 256) -> int:
    """Embed up to `limit` chunks that have no vector yet. Returns how many were embedded."""
    rows = (
        await session.execute(
            select(Chunk.id, Chunk.content)
            .where(Chunk.embedding.is_(None))
            .order_by(Chunk.id)
            .limit(limit)
        )
    ).all()
    if not rows:
        return 0
    vectors = await embedder.embed([row.content for row in rows], "document")
    for row, vector in zip(rows, vectors, strict=True):
        await session.execute(
            update(Chunk)
            .where(Chunk.id == row.id)
            .values(embedding=vector, embedding_model=embedder.model)
        )
    return len(rows)
