"""The knowledge behind one answer (/v1/context), and the Brain's own cited answer (/v1/ask).

context(): search (keywords + meaning + typo fixing + re-rank), then the "find facts" step: a
cheap model reads the top candidates and keeps the ones that answer this exact question (the
re-ranking of Task 2.7; without a model it keeps the re-ranked top). Then the role's
instructions, the best skill and the approved lessons that fit.

ask(): writes a cited answer from that context. Below the business's "I don't know" line it
doesn't call a writer at all: it says it doesn't know (Task 3.2).
"""

import time
import uuid
from collections.abc import Sequence

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.answers import WrittenAnswer, check_citations, source_block, strip_invented
from core.brain_api import (
    AskRequest,
    AskResponse,
    Citation,
    ContextRequest,
    ContextResponse,
    Instruction,
    LessonText,
    SearchHit,
)
from core.db import tenant_session
from core.llm.client import LlmClient
from core.llm.types import ChatTurn, LlmError, LlmRequest
from core.models import Business, ModelCall
from core.prompts import live_prompt, render
from core.tracing import Tracer
from knowledge_brain.knowledge import instructions_for_role, select_skill, skill_text
from knowledge_brain.models import KNOWLEDGE_TYPES, LessonDoc
from knowledge_brain.search.embed import embedder_for
from knowledge_brain.search.search import search

# Every knowledge type can be a cited source: an instruction or a skill may be the only place an
# answer is written down (the benchmark has 10 such questions). Lessons come in separately too.
MAIN_TYPES: list[str] = list(KNOWLEDGE_TYPES)
CANDIDATES = 16
LESSON_MIN_SCORE = 0.3


class FoundFacts(BaseModel):
    relevant: list[str]
    facts: list[dict[str, str]]


def _types_for(requested: Sequence[str] | None) -> list[str]:
    if not requested:
        return MAIN_TYPES
    return [t for t in requested if t in MAIN_TYPES] or MAIN_TYPES


async def context(
    session: AsyncSession,
    business_id: uuid.UUID,
    request: ContextRequest,
    *,
    llm: LlmClient | None,
    trace_id: uuid.UUID | None = None,
) -> ContextResponse:
    started = time.monotonic()
    business = await session.get(Business, business_id)
    local_only = request.local_only or bool(business and business.local_only)
    embedder = embedder_for(business_id, local_only=local_only)
    found = await search(
        session,
        request.question,
        knowledge_types=_types_for(request.knowledge_types),
        limit=CANDIDATES,
        embedder=embedder,
    )
    hits = found.hits
    top_score = max((hit.score for hit in hits), default=0.0)
    facts: list[dict[str, str]] = []
    if llm is not None and hits:
        hits, facts = await find_facts(session, business_id, request.question, hits, llm, trace_id)
    hits = hits[: request.limit]
    instructions = [
        Instruction(
            id=i.id,
            title=i.title,
            body=i.body,
            enabled=i.enabled,
            all_roles=i.all_roles,
            role_ids=i.role_ids,
        )
        for i in await instructions_for_role(session, request.role_id)
    ]
    if request.skill_version_id is not None:
        skill = await skill_text(session, request.skill_version_id, 1.0)
    else:
        skill = await select_skill(session, request.question, request.skill_keys)
    lessons = await relevant_lessons(session, request.question) if request.include_lessons else []
    return ContextResponse(
        hits=hits,
        top_score=round(top_score, 4),
        corrected_query=found.corrected_query,
        instructions=instructions,
        skill=skill,
        lessons=lessons,
        facts=facts,
        took_ms=int((time.monotonic() - started) * 1000),
    )


async def find_facts(
    session: AsyncSession,
    business_id: uuid.UUID,
    question: str,
    hits: list[SearchHit],
    llm: LlmClient,
    trace_id: uuid.UUID | None,
) -> tuple[list[SearchHit], list[dict[str, str]]]:
    prompt = await live_prompt(session, "chat.find_facts")
    sources, numbered = source_block(hits)
    try:
        result = await llm.call(
            LlmRequest(
                business_id=business_id,
                step="brain.find_facts",
                job="find_facts",
                system=[prompt.body],
                messages=[ChatTurn("user", f"Question: {question}\n\n{sources}")],
                schema=FoundFacts,
                max_tokens=3000,
                effort="low",
                trace_id=trace_id,
                prompt_key=prompt.key,
                prompt_version_id=prompt.version_id,
            )
        )
    except LlmError:
        return hits, []  # no model available: the re-ranked order stands
    if not isinstance(result.parsed, FoundFacts):
        return hits, []
    kept = [numbered[sid] for sid in result.parsed.relevant if sid in numbered]
    return kept, result.parsed.facts


async def relevant_lessons(session: AsyncSession, question: str) -> list[LessonText]:
    found = await search(session, question, knowledge_types=["lesson"], limit=3, mode="keyword")
    lessons: list[LessonText] = []
    for hit in found.hits:
        if hit.lesson_id is None or hit.score < LESSON_MIN_SCORE:
            continue
        doc = await session.get(LessonDoc, hit.lesson_id)
        if doc is not None:
            lessons.append(
                LessonText(
                    id=doc.id,
                    topic=doc.topic,
                    title=doc.title,
                    markdown=doc.markdown,
                    version=doc.version,
                    updated_at=doc.updated_at,
                )
            )
    # Newer lessons win when two disagree: they come last, closest to the question
    return sorted(lessons, key=lambda lesson: lesson.updated_at or lesson.id.hex)


def lessons_text(lessons: list[LessonText]) -> str:
    return (
        "\n\n".join(
            f"Lesson ({lesson.updated_at:%Y-%m-%d}): {lesson.markdown}"
            if lesson.updated_at
            else lesson.markdown
            for lesson in lessons
        )
        or "(none)"
    )


async def ask(business_id: uuid.UUID, request: AskRequest) -> AskResponse:
    """The Brain's own cited answer, for the API and the Knowledge Brain benchmark."""
    started = time.monotonic()
    tracer = await Tracer.start(business_id, "brain_ask", request.question[:120], role="brain")
    llm = LlmClient(business_id, role="brain")
    async with tenant_session(business_id, role="brain") as session:
        business = await session.get(Business, business_id)
        threshold = business.dont_know_threshold if business else 0.3
        with tracer.span("context") as span:
            ctx = await context(
                session,
                business_id,
                ContextRequest(
                    question=request.question,
                    role_id=request.role_id,
                    skill_version_id=request.skill_version_id,
                    include_lessons=request.include_lessons,
                ),
                llm=llm,
                trace_id=tracer.trace_id,
            )
            span.update(hits=len(ctx.hits), top_score=ctx.top_score)
        system = await live_prompt(session, "brain.answer_system")
        user = await live_prompt(session, "brain.answer_user")
        overrides = request.prompt_overrides or {}
        system_body = overrides.get("brain.answer_system", system.body)
        user_body = overrides.get("brain.answer_user", user.body)
        business_name = business.name if business else "the business"
    if ctx.top_score < threshold or not ctx.hits:
        await tracer.finish()
        return AskResponse(
            answer="I don't know: nothing in the knowledge base answers this.",
            answered=False,
            citations=[],
            knowledge_types=[],
            confidence=0.0,
            top_score=ctx.top_score,
            model_key=None,
            cost_usd=0.0,
            latency_ms=int((time.monotonic() - started) * 1000),
            trace_id=tracer.trace_id,
        )
    sources, numbered = source_block(ctx.hits)
    message = render(
        user_body,
        {
            "instructions": "\n".join(f"- {i.title}: {i.body}" for i in ctx.instructions)
            or "(none)",
            "skill": ctx.skill.markdown if ctx.skill else "(none)",
            "lessons": lessons_text(ctx.lessons),
            "knowledge_results": sources,
            "question": request.question,
        },
    )
    with tracer.span("write_answer"):
        result = await llm.call(
            LlmRequest(
                business_id=business_id,
                step="brain.ask",
                job="write_answer",
                system=[render(system_body, {"business_name": business_name})],
                messages=[ChatTurn("user", message)],
                schema=WrittenAnswer,
                max_tokens=8000,
                effort="medium",
                model_key=request.model_key,
                trace_id=tracer.trace_id,
                prompt_key=system.key,
                prompt_version_id=system.version_id,
            )
        )
    assert isinstance(result.parsed, WrittenAnswer)
    written = result.parsed
    valid, invented = check_citations(written, numbered)
    citations = [
        Citation(
            n=sid,
            chunk_id=numbered[sid].chunk_id,
            knowledge_type=numbered[sid].knowledge_type,
            title=numbered[sid].title,
            url=numbered[sid].url,
            anchor=numbered[sid].anchor,
            quote=numbered[sid].content[:300],
        )
        for sid in valid
    ]
    await tracer.finish()
    async with tenant_session(business_id, role="brain") as session:
        costs = await session.execute(
            select(ModelCall.cost_usd).where(ModelCall.trace_id == tracer.trace_id)
        )
        cost = sum(float(value) for value in costs.scalars())
    return AskResponse(
        answer=strip_invented(written.answer, invented),
        answered=written.answered and bool(valid),
        citations=citations,
        knowledge_types=list(dict.fromkeys(c.knowledge_type for c in citations)),
        confidence=written.confidence,
        top_score=ctx.top_score,
        model_key=result.model_key,
        cost_usd=round(cost, 6),
        latency_ms=int((time.monotonic() - started) * 1000),
        trace_id=tracer.trace_id,
    )
