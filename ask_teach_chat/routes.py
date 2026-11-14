"""The Ask and Teach Chat (screen 7) and Lessons (screen 9)."""

import io
import uuid
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ask_teach_chat import pipeline, teach
from ask_teach_chat.models import Answer, Chat, Feedback, Lesson, Message
from core import storage
from core.llm.types import ChatTurn, LlmError
from core.models import Business, ModelCall, Role, RoleVersion, User
from core.pii import mask
from eval_lab.models import CandidateCase
from web.deps import Ctx, bid, business_ctx, db
from web.state import queue
from web.templating import render

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(business_ctx)]
DbDep = Annotated[AsyncSession, Depends(db)]

KIND_FOR = {"answer": "answer", "dont_know": "dont_know", "handoff": "handoff", "refused": "notice"}


async def live_roles(session: AsyncSession) -> list[tuple[Role, RoleVersion]]:
    rows = await session.execute(
        select(Role, RoleVersion)
        .join(RoleVersion, (RoleVersion.role_id == Role.id) & (RoleVersion.status == "live"))
        .where(Role.archived_at.is_(None))
        .order_by(Role.builtin.desc(), Role.created_at)
    )
    return [(role, version) for role, version in rows.all()]


async def chat_list(session: AsyncSession, user_id: uuid.UUID) -> list[Chat]:
    return list(
        (
            await session.execute(
                select(Chat)
                .where(Chat.user_id == user_id)
                .order_by(Chat.updated_at.desc())
                .limit(40)
            )
        ).scalars()
    )


async def chat_open(session: AsyncSession, ctx: Ctx) -> tuple[bool, str | None]:
    """The chat opens for the team when the readiness score reaches the owner's level (Task 6.3).
    The owner can always use it, to test before switching it on."""
    business = await session.get(Business, bid(ctx))
    if business is None or business.chat_enabled or ctx.is_owner:
        return True, None if business is None or business.chat_enabled else (
            f"The chat is off for your team until the readiness score reaches {business.readiness_threshold}% (Go Live). You can test it as the owner."
        )
    return (
        False,
        f"The chat is off until the readiness score reaches {business.readiness_threshold}%. Ask the owner to finish Go Live.",
    )


@router.get("/chat", response_class=HTMLResponse)
async def chat_home(request: Request, ctx: CtxDep, session: DbDep) -> Response:
    chats = await chat_list(session, ctx.user_id)
    if chats:
        return RedirectResponse(f"/chat/{chats[0].id}", status_code=303)
    return render(
        request,
        "ask_teach_chat/chat.html",
        active="chat",
        chats=[],
        chat=None,
        messages=[],
        roles=await live_roles(session),
        answers={},
        trace=None,
        gate=await chat_open(session, ctx),
    )


@router.post("/chat")
async def new_chat(
    ctx: CtxDep, session: DbDep, mode: Annotated[str, Form()], role_id: Annotated[uuid.UUID, Form()]
) -> Response:
    if mode not in ("ask", "teach"):
        raise HTTPException(400, "Pick Ask something or Teach me")
    chat = Chat(
        business_id=bid(ctx),
        user_id=ctx.user_id,
        mode=mode,
        role_id=role_id,
        title="Teaching" if mode == "teach" else "New chat",
    )
    session.add(chat)
    await session.flush()
    return RedirectResponse(f"/chat/{chat.id}", status_code=303)


async def _messages(
    session: AsyncSession, chat_id: uuid.UUID
) -> tuple[list[Message], dict[uuid.UUID, Answer]]:
    messages = list(
        (
            await session.execute(
                select(Message).where(Message.chat_id == chat_id).order_by(Message.seq)
            )
        ).scalars()
    )
    answers = {
        a.message_id: a
        for a in (
            await session.execute(
                select(Answer).where(Answer.message_id.in_([m.id for m in messages]))
            )
        ).scalars()
    }
    return messages, answers


async def _trace(session: AsyncSession, answer: Answer | None) -> dict[str, Any] | None:
    if answer is None:
        return None
    calls: Sequence[ModelCall] = (
        (
            await session.execute(
                select(ModelCall)
                .where(ModelCall.trace_id == answer.trace_id)
                .order_by(ModelCall.created_at)
            )
        )
        .scalars()
        .all()
        if answer.trace_id
        else ()
    )
    return {"answer": answer, "calls": list(calls)}


@router.get("/chat/{chat_id}", response_class=HTMLResponse)
async def chat_page(
    request: Request, chat_id: uuid.UUID, ctx: CtxDep, session: DbDep
) -> HTMLResponse:
    chat = await session.get(Chat, chat_id)
    if chat is None or chat.user_id != ctx.user_id:
        raise HTTPException(404, "No such chat")
    messages, answers = await _messages(session, chat_id)
    last = next((answers[m.id] for m in reversed(messages) if m.id in answers), None)
    return render(
        request,
        "ask_teach_chat/chat.html",
        active="chat",
        chats=await chat_list(session, ctx.user_id),
        chat=chat,
        messages=messages,
        answers=answers,
        roles=await live_roles(session),
        trace=await _trace(session, last),
        gate=await chat_open(session, ctx),
    )


async def _add(session: AsyncSession, chat: Chat, sender: str, kind: str, body: str) -> Message:
    seq = (
        int(
            await session.scalar(
                select(func.coalesce(func.max(Message.seq), 0)).where(Message.chat_id == chat.id)
            )
            or 0
        )
        + 1
    )
    message = Message(
        business_id=chat.business_id,
        chat_id=chat.id,
        seq=seq,
        sender=sender,
        kind=kind,
        body=body,
        role_id=chat.role_id,
    )
    session.add(message)
    await session.flush()
    return message


@router.post("/chat/{chat_id}/messages", response_class=HTMLResponse)
async def send(
    request: Request, chat_id: uuid.UUID, ctx: CtxDep, session: DbDep, text: Annotated[str, Form()]
) -> HTMLResponse:
    chat = await session.get(Chat, chat_id)
    if chat is None or chat.user_id != ctx.user_id:
        raise HTTPException(404)
    allowed, why = await chat_open(session, ctx)
    if not allowed:
        return HTMLResponse(f'<div class="notice warn">{why}</div>')
    text = text.strip()
    if not text:
        return HTMLResponse("")
    roles = {role.id: (role, version) for role, version in await live_roles(session)}
    chosen = roles.get(chat.role_id) if chat.role_id else None
    role, version = chosen or next(iter(roles.values()))
    settings = pipeline.RoleSettings.from_version(role.name, version)
    earlier, _ = await _messages(session, chat_id)
    teaching = chat.mode == "teach" or (
        teach.is_correction(text) and any(m.kind == "answer" for m in earlier)
    )
    user_message = await _add(
        session, chat, "user", "teaching" if teaching else "question", mask(text)
    )
    if chat.title in ("New chat", "Teaching"):
        chat.title = mask(text)[:60]
    chat.updated_at = datetime.now(UTC)
    await session.commit()  # the person's words are saved before any model runs
    history = [ChatTurn("user" if m.sender == "user" else "assistant", m.body) for m in earlier]
    answer_row: Answer | None = None
    try:
        if teaching:
            last_answer = next((m.id for m in reversed(earlier) if m.kind == "answer"), None)
            reply, lesson = await teach.teach_turn(
                bid(ctx),
                chat_id=chat.id,
                history=[*history, ChatTurn("user", text)],
                user_id=ctx.user_id,
                answer_id=last_answer,
            )
            agent = await _add(
                session, chat, "agent", "lesson_draft" if lesson else "clarifying", reply
            )
        else:
            outcome = await pipeline.answer(
                bid(ctx), text=text, role=settings, history=history, chat_id=chat.id
            )
            agent = await _add(session, chat, "agent", KIND_FOR[outcome.kind], outcome.text)
            answer_row = Answer(
                business_id=bid(ctx),
                message_id=agent.id,
                question_message_id=user_message.id,
                question=mask(outcome.question),
                answered=outcome.answered,
                needs_human=outcome.needs_human,
                handoff_reason=outcome.handoff_reason,
                expert_task_id=outcome.expert_task_id,
                confidence=outcome.confidence,
                top_score=outcome.top_score,
                citations=outcome.citations,
                knowledge_types=outcome.knowledge_types,
                chunk_ids=outcome.chunk_ids,
                skill_version_ids=outcome.skill_version_ids,
                lesson_ids=outcome.lesson_ids,
                instruction_ids=outcome.instruction_ids,
                prompt_versions=outcome.prompt_versions,
                role_version_id=version.id,
                model_key=outcome.model_key,
                trace_id=outcome.trace_id,
                cost_usd=outcome.cost_usd,
                latency_ms=outcome.latency_ms,
                safety=outcome.safety,
                summary={
                    "skill": outcome.skill_name,
                    "lessons": outcome.lesson_titles,
                    "steps": outcome.steps,
                    "role": role.name,
                },
            )
            session.add(answer_row)
    except LlmError as exc:
        agent = await _add(
            session,
            chat,
            "agent",
            "notice",
            f"The model isn't available right now ({exc}). Your message is saved: send it again, or add a key in Settings > Keys and connections.",
        )
    await session.flush()
    answers = {answer_row.message_id: answer_row} if answer_row else {}
    return render(
        request,
        "ask_teach_chat/_exchange.html",
        chat=chat,
        messages=[user_message, agent],
        answers=answers,
        trace=await _trace(session, answer_row) if answer_row else None,
    )


@router.post("/chat/{chat_id}/role")
async def switch_role(
    chat_id: uuid.UUID, ctx: CtxDep, session: DbDep, role_id: Annotated[uuid.UUID, Form()]
) -> Response:
    chat = await session.get(Chat, chat_id)
    if chat is None or chat.user_id != ctx.user_id:
        raise HTTPException(404)
    chat.role_id = role_id
    role = await session.get(Role, role_id)
    await _add(
        session,
        chat,
        "agent",
        "notice",
        f"Role switched to {role.name if role else 'another role'}.",
    )
    return RedirectResponse(f"/chat/{chat_id}", status_code=303)


@router.get("/chat/cite/{message_id}/{source_id}")
async def open_citation(
    message_id: uuid.UUID, source_id: str, ctx: CtxDep, session: DbDep
) -> Response:
    """A [S2] in an answer's text: one click opens the exact source (Task 3.2)."""
    answer = await session.get(Answer, message_id)
    citation = next(
        (c for c in (answer.citations if answer else []) if c.get("n") == source_id), None
    )
    if citation is None:
        raise HTTPException(404, "That source isn't in this answer")
    return RedirectResponse(f"/sources/{citation['chunk_id']}", status_code=303)


@router.get("/chat/answers/{message_id}/trace", response_class=HTMLResponse)
async def trace_panel(
    request: Request, message_id: uuid.UUID, ctx: CtxDep, session: DbDep
) -> HTMLResponse:
    return render(
        request,
        "ask_teach_chat/_trace.html",
        trace=await _trace(session, await session.get(Answer, message_id)),
    )


@router.post("/chat/answers/{message_id}/feedback", response_class=HTMLResponse)
async def feedback(
    request: Request,
    message_id: uuid.UUID,
    ctx: CtxDep,
    session: DbDep,
    verdict: Annotated[str, Form()],
    why: Annotated[str, Form()] = "",
    correct_answer: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Tasks 3.4 and 4.4: right or wrong, why, the right answer. A wrong answer becomes a lesson
    (written in the background) and a proposed test case."""
    answer = await session.get(Answer, message_id)
    message = await session.get(Message, message_id)
    if answer is None or message is None or verdict not in ("right", "wrong"):
        raise HTTPException(404)
    row = Feedback(
        business_id=bid(ctx),
        answer_id=message_id,
        user_id=ctx.user_id,
        verdict=verdict,
        why=mask(why.strip()) or None,
        correct_answer=mask(correct_answer.strip()) or None,
    )
    session.add(row)
    await session.flush()
    note = "Thanks: noted as right."
    if verdict == "wrong":
        case = CandidateCase(
            business_id=bid(ctx),
            feedback_id=row.id,
            question=answer.question,
            bad_answer=message.body,
            expected=row.correct_answer or "",
            reason=row.why,
        )
        session.add(case)
        await session.flush()
        row.candidate_case_id = case.id
        note = "Thanks. It is now a proposed test case (Eval Lab > Feedback cases)."
        if row.why or row.correct_answer:
            await session.commit()
            await queue().enqueue(
                "chat:lesson_from_feedback",
                queue="web",
                business_id=str(bid(ctx)),
                feedback_id=str(row.id),
            )
            note += " A lesson is being written from your correction; approve it in Lessons."
    return HTMLResponse(f'<div class="notice ok small">{note}</div>')


# ---------------------------------------------------------------- lessons (screen 9)


@router.get("/lessons/download")
async def download_lessons(ctx: CtxDep) -> Response:
    """Every lesson file of this business as one .zip, for the repo's lessons/ folder."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(storage.folder(bid(ctx), "lessons").glob("*.md")):
            archive.write(path, f"lessons/{path.name}")
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="lessons.zip"'},
    )


@router.get("/lessons", response_class=HTMLResponse)
async def lessons(
    request: Request, ctx: CtxDep, session: DbDep, status: str = "pending"
) -> HTMLResponse:
    rows = (
        (
            await session.execute(
                select(Lesson).where(Lesson.status == status).order_by(Lesson.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    items: list[dict[str, Any]] = []
    for lesson in rows:
        version = await teach.latest_version(session, lesson.id)
        who = await session.get(User, lesson.taught_by) if lesson.taught_by else None
        items.append({"lesson": lesson, "version": version, "who": who.name if who else "unknown"})
    counts = {
        s: int(n)
        for s, n in (
            await session.execute(select(Lesson.status, func.count()).group_by(Lesson.status))
        ).all()
    }
    tabs = [
        (f"Pending ({counts.get('pending', 0)})", "/lessons?status=pending", "pending"),
        (f"Approved ({counts.get('approved', 0)})", "/lessons?status=approved", "approved"),
        (f"Off ({counts.get('off', 0)})", "/lessons?status=off", "off"),
    ]
    return render(
        request,
        "ask_teach_chat/lessons.html",
        active="lessons",
        tabs=tabs,
        tab=status,
        items=items,
        notice=request.query_params.get("msg"),
    )


async def _lesson(session: AsyncSession, lesson_id: uuid.UUID) -> Lesson:
    lesson = await session.get(Lesson, lesson_id)
    if lesson is None:
        raise HTTPException(404, "No such lesson")
    return lesson


@router.post("/lessons/{lesson_id}/approve")
async def approve(
    lesson_id: uuid.UUID, ctx: CtxDep, session: DbDep, confirm: Annotated[bool, Form()] = False
) -> Response:
    lesson = await _lesson(session, lesson_id)
    version = await teach.latest_version(session, lesson_id)
    clashes = await teach.conflicts(lesson, version.markdown)
    if clashes and not confirm:
        lesson.conflicts_with = [
            uuid.UUID(str(c["lesson_id"])) for c in clashes if c.get("lesson_id")
        ]
        titles = ", ".join(str(c["title"]) for c in clashes)
        return RedirectResponse(
            f"/lessons?status=pending&msg=This may disagree with: {titles}. If both stay, the newer lesson wins. Approve again to confirm.",
            status_code=303,
        )
    await teach.approve(session, lesson, ctx.user_id)
    return RedirectResponse(
        "/lessons?status=approved&msg=Approved: answers use it from now on.", status_code=303
    )


@router.post("/lessons/{lesson_id}/off")
async def switch_off(lesson_id: uuid.UUID, ctx: CtxDep, session: DbDep) -> Response:
    await teach.switch_off(session, await _lesson(session, lesson_id))
    return RedirectResponse(
        "/lessons?status=off&msg=Switched off: answers no longer use it.", status_code=303
    )


@router.post("/lessons/{lesson_id}/delete")
async def delete(lesson_id: uuid.UUID, ctx: CtxDep, session: DbDep) -> Response:
    await teach.delete(session, await _lesson(session, lesson_id))
    return RedirectResponse("/lessons?msg=Deleted.", status_code=303)


@router.post("/lessons/{lesson_id}/edit")
async def edit(
    lesson_id: uuid.UUID, ctx: CtxDep, session: DbDep, markdown: Annotated[str, Form()]
) -> Response:
    lesson = await _lesson(session, lesson_id)
    await teach.edit(session, lesson, markdown, ctx.user_id)
    return RedirectResponse(
        f"/lessons?status={lesson.status}&msg=Saved as a new version.", status_code=303
    )


@router.post("/lessons/{lesson_id}/test")
async def test_before_after(lesson_id: uuid.UUID, ctx: CtxDep, session: DbDep) -> Response:
    """Task 3.5: the same question before and after the lesson, scored in the Eval Lab."""
    await _lesson(session, lesson_id)
    await queue().enqueue(
        "evals:lesson_check", queue="web", business_id=str(bid(ctx)), lesson_id=str(lesson_id)
    )
    return RedirectResponse(
        "/evals/runs?msg=Before-and-after run started for the lesson.", status_code=303
    )
