"""Search, the knowledge for one answer, the Brain's own cited answer, and the source viewer."""

import uuid

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from core import storage
from core.brain_api import (
    AskRequest,
    AskResponse,
    ContextRequest,
    ContextResponse,
    SearchRequest,
    SearchResponse,
    SourceView,
)
from core.llm.client import LlmClient
from core.models import Business
from knowledge_brain import answer
from knowledge_brain.api.deps import BusinessId, Db
from knowledge_brain.models import (
    Chunk,
    EmailMessage,
    EmailThread,
    Image,
    Instruction,
    LessonDoc,
    Page,
    Skill,
    SkillVersion,
)
from knowledge_brain.search.chunk import blocks
from knowledge_brain.search.embed import embedder_for
from knowledge_brain.search.search import search

router = APIRouter(prefix="/v1", tags=["Search and answers"])


@router.post("/search", response_model=SearchResponse, summary="Search every knowledge type")
async def search_route(body: SearchRequest, bid: BusinessId, db: Db) -> SearchResponse:
    """Keywords and meaning together, typo fixing, then re-ranking (mode=hybrid, rerank=true).
    mode=keyword or mode=vector, and rerank=false, exist so the Eval Lab can compare."""
    business = await db.get(Business, bid)
    embedder = embedder_for(bid, local_only=bool(business and business.local_only))
    return await search(
        db,
        body.query,
        knowledge_types=list(body.knowledge_types) if body.knowledge_types else None,
        limit=body.limit,
        mode=body.mode,
        rerank=body.rerank,
        embedder=embedder,
    )


@router.post("/context", response_model=ContextResponse, summary="Everything one answer needs")
async def context_route(body: ContextRequest, bid: BusinessId, db: Db) -> ContextResponse:
    """The re-ranked knowledge (after the find-facts step), the role's instructions, the best
    skill and the approved lessons that fit. The chat writes its own prompt from these."""
    return await answer.context(
        db, bid, body, llm=LlmClient(bid, role="brain", force_local=body.local_only)
    )


@router.post("/ask", response_model=AskResponse, summary="A cited answer from the Knowledge Brain")
async def ask_route(body: AskRequest, bid: BusinessId) -> AskResponse:
    """Says "I don't know" below the business's line instead of guessing. model_key forces one
    of the 14 models (the Eval Lab uses it)."""
    return await answer.ask(bid, body)


@router.get(
    "/chunks/{chunk_id}", response_model=SourceView, summary="What one click on a citation opens"
)
async def chunk_route(chunk_id: int, db: Db) -> SourceView:
    chunk = await db.get(Chunk, chunk_id)
    if chunk is None:
        raise HTTPException(404, "No such source")
    view = SourceView(
        chunk_id=chunk.id,
        knowledge_type=chunk.knowledge_type,  # type: ignore[arg-type]
        title=chunk.heading,
        heading=chunk.heading,
        content=chunk.content,
        url=None,
        anchor=chunk.anchor,
    )
    if chunk.page_id:
        page = await db.get(Page, chunk.page_id)
        if page is not None:
            view.title = page.title or page.url
            view.url = page.url if page.origin in ("sitemap", "page_url") else None
            view.page_blocks = blocks(page.content_md or "")
            view.level = page.level
    if chunk.image_id:
        image = await db.get(Image, chunk.image_id)
        if image is not None:
            view.image_url = f"/v1/images/{image.id}/file" if image.storage_path else image.src_url
            view.image_description = image.description
    if chunk.instruction_id and (item := await db.get(Instruction, chunk.instruction_id)):
        view.title = f"Instruction: {item.title}"
    if chunk.skill_version_id:
        row = (
            await db.execute(
                select(Skill.name, SkillVersion.version)
                .join(SkillVersion, SkillVersion.skill_id == Skill.id)
                .where(SkillVersion.id == chunk.skill_version_id)
            )
        ).first()
        if row is not None:
            view.title = f"Skill: {row.name} (v{row.version})"
    if chunk.lesson_doc_id and (lesson := await db.get(LessonDoc, chunk.lesson_doc_id)):
        view.title = f"Lesson: {lesson.title}"
    if chunk.email_message_id and (message := await db.get(EmailMessage, chunk.email_message_id)):
        thread = await db.get(EmailThread, message.thread_id)
        view.title = f"Past email: {thread.subject if thread else ''}"
    return view


@router.get("/images/{image_id}/file", summary="A stored screenshot")
async def image_file(image_id: uuid.UUID, db: Db) -> FileResponse:
    image = await db.get(Image, image_id)
    if image is None or not image.storage_path:
        raise HTTPException(404)
    return FileResponse(storage.absolute(image.storage_path))


@router.delete(
    "/business/files", status_code=204, summary="Delete the business's files (before deleting it)"
)
async def purge_files(bid: BusinessId) -> None:
    storage.delete_business(bid, ("images", "documents"))
