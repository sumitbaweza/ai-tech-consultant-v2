"""Teaching becomes lessons and skills (Tasks 3.1 and 3.4, DATABASE.md D10).

Three ways in: a "Teach me" chat, "This is wrong, the right answer is ..." typed in an Ask
chat, and the comment box on an answer. Each ends in a lesson: a Markdown file (one per topic,
lessons/<topic>.md) and a record (who, when, the example, how sure the writer is). Nothing is
used until a human approves it; approval copies the lesson into the Brain (through its API) so
answers can use and cite it, newest first when two disagree. When the correction fits a whole
kind of work, a skill change is proposed too, for the Skills screen.

Lessons are written by the "Turn a correction into a lesson" step: Claude Opus 5.5 by default.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ask_teach_chat.models import Answer, Feedback, Lesson, LessonVersion, Message
from core import storage
from core.brain_client import BrainClient, BrainError
from core.db import tenant_session
from core.llm.client import LlmClient
from core.llm.types import ChatTurn, LlmRequest
from core.models import Business, User
from core.prompts import live_prompt, render

CORRECTION = re.compile(
    r"^\s*(no\b|nope\b|wrong\b|incorrect\b|that'?s (not right|wrong|incorrect)|this is (wrong|incorrect|not right)|"
    r"actually\b|the (right|correct) answer is|you should (have )?(said|say|tell|told)|it should (be|say)|"
    r"not quite|that'?s outdated)",
    re.IGNORECASE,
)


class LessonDraft(BaseModel):
    topic: str
    title: str
    rule: str
    example_question: str
    right_answer: str
    confidence: float
    scope: str
    skill_hint: str


class TeachReply(BaseModel):
    understood: bool
    question: str
    lesson: LessonDraft


class WrittenLesson(BaseModel):
    topic: str
    title: str
    rule: str
    markdown: str
    confidence: float
    scope: str
    skill_hint: str


def is_correction(text: str) -> bool:
    """Teaching inside an Ask chat (Task 3.1)."""
    return bool(CORRECTION.search(text))


def topic_slug(topic: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:40] or "general"


def lesson_markdown(title: str, rule: str, question: str, answer: str) -> str:
    return (
        f"# {title}\n\n{rule}\n\n## When it applies\n{question and 'Questions like: ' + question}\n\n"
        f"## Example\n**Customer:** {question}\n\n**Right answer:** {answer}\n"
    )


async def _business_name(business_id: uuid.UUID) -> str:
    async with tenant_session(business_id) as session:
        business = await session.get(Business, business_id)
        return business.name if business else "the business"


async def teach_turn(
    business_id: uuid.UUID,
    *,
    chat_id: uuid.UUID,
    history: list[ChatTurn],
    user_id: uuid.UUID | None,
    answer_id: uuid.UUID | None = None,
) -> tuple[str, Lesson | None]:
    """One turn of teaching: the agent asks one question back, or drafts the lesson.
    Returns (the agent's message, the lesson when one was drafted)."""
    async with tenant_session(business_id) as session:
        prompt = await live_prompt(session, "chat.teach_dialogue")
    result = await LlmClient(business_id).call(
        LlmRequest(
            business_id=business_id,
            step="chat.teach",
            job="write_lesson",
            system=[render(prompt.body, {"business_name": await _business_name(business_id)})],
            messages=[ChatTurn("user", "\n".join(f"{t.role}: {t.text}" for t in history[-12:]))],
            schema=TeachReply,
            max_tokens=12000,
            effort="medium",
            prompt_key=prompt.key,
            prompt_version_id=prompt.version_id,
            real_customer_data=True,
        )
    )
    reply = result.parsed
    assert isinstance(reply, TeachReply)
    if not reply.understood or not reply.lesson.rule.strip():
        return reply.question or "Could you say a little more about when this applies?", None
    draft = reply.lesson
    markdown = lesson_markdown(draft.title, draft.rule, draft.example_question, draft.right_answer)
    async with tenant_session(business_id, user_id=user_id) as session:
        lesson = await save_lesson(
            session,
            business_id,
            topic=draft.topic,
            title=draft.title,
            rule=draft.rule,
            markdown=markdown,
            confidence=draft.confidence,
            taught_by=user_id,
            chat_id=chat_id,
            answer_id=answer_id,
            example={"question": draft.example_question, "right_answer": draft.right_answer},
        )
    if draft.scope == "skill":
        await propose_skill_change(business_id, lesson, draft.skill_hint)
    text = (
        f"Got it. I drafted a lesson: **{draft.title}**\n\n{draft.rule}\n\n"
        "It waits for approval in Lessons; nothing is used before a human approves it."
    )
    return text, lesson


async def lesson_from_feedback(business_id: uuid.UUID, feedback_id: uuid.UUID) -> uuid.UUID | None:
    """The comment box said "wrong": write the lesson (a background job; Opus can take a minute)."""
    async with tenant_session(business_id) as session:
        feedback = await session.get(Feedback, feedback_id)
        if feedback is None or feedback.lesson_id is not None:
            return None
        answer = await session.get(Answer, feedback.answer_id)
        message = await session.get(Message, feedback.answer_id)
        prompt = await live_prompt(session, "chat.write_lesson")
        chat_id = message.chat_id if message else None
        question, given = (answer.question if answer else ""), (message.body if message else "")
        why, right = feedback.why or "", feedback.correct_answer or ""
        taught_by = feedback.user_id
    result = await LlmClient(business_id).call(
        LlmRequest(
            business_id=business_id,
            step="chat.write_lesson",
            job="write_lesson",
            system=[render(prompt.body, {"business_name": await _business_name(business_id)})],
            messages=[
                ChatTurn(
                    "user",
                    f"<question>{question}</question>\n<answer_given>{given}</answer_given>\n"
                    f'<correction verdict="wrong">\nWhy: {why}\nRight answer: {right}\n</correction>',
                )
            ],
            schema=WrittenLesson,
            max_tokens=12000,
            effort="medium",
            prompt_key=prompt.key,
            prompt_version_id=prompt.version_id,
            real_customer_data=True,
        )
    )
    written = result.parsed
    assert isinstance(written, WrittenLesson)
    async with tenant_session(business_id) as session:
        lesson = await save_lesson(
            session,
            business_id,
            topic=written.topic,
            title=written.title,
            rule=written.rule,
            markdown=written.markdown,
            confidence=written.confidence,
            taught_by=taught_by,
            chat_id=chat_id,
            answer_id=answer.message_id if answer else None,
            example={"question": question, "wrong_answer": given, "right_answer": right},
        )
        feedback = await session.get(Feedback, feedback_id)
        if feedback is not None:
            feedback.lesson_id = lesson.id
    if written.scope == "skill":
        await propose_skill_change(business_id, lesson, written.skill_hint)
    return lesson.id


async def save_lesson(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    topic: str,
    title: str,
    rule: str,
    markdown: str,
    confidence: float,
    taught_by: uuid.UUID | None,
    chat_id: uuid.UUID | None,
    answer_id: uuid.UUID | None,
    example: dict[str, Any],
) -> Lesson:
    lesson = Lesson(
        business_id=business_id,
        topic=topic_slug(topic),
        title=title[:200],
        status="pending",
        chat_id=chat_id,
        answer_id=answer_id,
        taught_by=taught_by,
        confidence=max(0.0, min(confidence, 1.0)),
    )
    session.add(lesson)
    await session.flush()
    session.add(
        LessonVersion(
            business_id=business_id,
            lesson_id=lesson.id,
            version=1,
            markdown=markdown,
            rule=rule,
            example=example,
            author="agent",
        )
    )
    await session.flush()
    lesson.file_path = await write_topic_file(session, business_id, lesson.topic)
    return lesson


async def latest_version(session: AsyncSession, lesson_id: uuid.UUID) -> LessonVersion:
    return (
        await session.execute(
            select(LessonVersion)
            .where(LessonVersion.lesson_id == lesson_id)
            .order_by(LessonVersion.version.desc())
            .limit(1)
        )
    ).scalar_one()


async def write_topic_file(session: AsyncSession, business_id: uuid.UUID, topic: str) -> str:
    """lessons/<topic>.md: every lesson on the topic, with its record (who, when, how sure, status)."""
    lessons = (
        (
            await session.execute(
                select(Lesson)
                .where(Lesson.topic == topic, Lesson.status != "rejected")
                .order_by(Lesson.created_at)
            )
        )
        .scalars()
        .all()
    )
    parts = [f"# Lessons: {topic}\n"]
    for lesson in lessons:
        version = await latest_version(session, lesson.id)
        who = await session.get(User, lesson.taught_by) if lesson.taught_by else None
        before, after = await check_scores(session, lesson.id)
        scores = (
            f" | judge score before: {before:.2f}, after: {after:.2f}"
            if before is not None and after is not None
            else " | not tested before and after yet"
        )
        parts.append(
            f"<!-- lesson {lesson.id} | status: {lesson.status} | taught by: {who.name if who else 'unknown'} | "
            f"when: {lesson.created_at:%Y-%m-%d %H:%M} | confidence: {lesson.confidence:.2f} | version: {version.version}{scores} -->\n"
            f"{version.markdown.strip()}\n"
        )
    return storage.save_text(business_id, "lessons", f"{topic}.md", "\n".join(parts))


async def check_scores(
    session: AsyncSession, lesson_id: uuid.UUID
) -> tuple[float | None, float | None]:
    """The latest "Test before and after" of a lesson: the judge's score without and with it."""
    from eval_lab.models import Case, Result

    rows = (
        await session.execute(
            select(Result.model_key, Result.judge_score)
            .join(Case, Case.id == Result.case_id)
            .where(Case.case_key == f"LESSON-{str(lesson_id)[:8]}")
            .order_by(Result.created_at.desc())
        )
    ).all()
    latest: dict[str, float | None] = {}
    for label, score in rows:
        latest.setdefault(label, score)
    return latest.get("before"), latest.get("after")


async def approve(session: AsyncSession, lesson: Lesson, user_id: uuid.UUID | None) -> None:
    version = await latest_version(session, lesson.id)
    async with await BrainClient.for_business(lesson.business_id, user_id) as brain:
        await brain.put(
            f"/v1/lessons/{lesson.id}",
            {
                "topic": lesson.topic,
                "title": lesson.title,
                "markdown": version.markdown,
                "version": version.version,
            },
        )
    lesson.status, lesson.approved_by, lesson.approved_at = "approved", user_id, datetime.now(UTC)
    await write_topic_file(session, lesson.business_id, lesson.topic)


async def switch_off(session: AsyncSession, lesson: Lesson) -> None:
    async with await BrainClient.for_business(lesson.business_id) as brain:
        await brain.delete(f"/v1/lessons/{lesson.id}")
    lesson.status = "off"
    await write_topic_file(session, lesson.business_id, lesson.topic)


async def delete(session: AsyncSession, lesson: Lesson) -> None:
    try:
        async with await BrainClient.for_business(lesson.business_id) as brain:
            await brain.delete(f"/v1/lessons/{lesson.id}")
    except BrainError:
        pass  # never indexed
    topic, business_id = lesson.topic, lesson.business_id
    await session.delete(lesson)
    await session.flush()
    await write_topic_file(session, business_id, topic)


async def edit(
    session: AsyncSession, lesson: Lesson, markdown: str, user_id: uuid.UUID | None
) -> None:
    last = await latest_version(session, lesson.id)
    number = (
        int(
            await session.scalar(
                select(func.max(LessonVersion.version)).where(LessonVersion.lesson_id == lesson.id)
            )
            or 0
        )
        + 1
    )
    session.add(
        LessonVersion(
            business_id=lesson.business_id,
            lesson_id=lesson.id,
            version=number,
            markdown=markdown,
            rule=last.rule,
            example=last.example,
            author="human",
            created_by=user_id,
        )
    )
    await session.flush()
    if lesson.status == "approved":
        await approve(session, lesson, user_id)  # the Brain's copy follows the edit
    else:
        await write_topic_file(session, lesson.business_id, lesson.topic)


async def conflicts(lesson: Lesson, markdown: str) -> list[dict[str, Any]]:
    """Approved lessons that may say something different (shown before approving)."""
    try:
        async with await BrainClient.for_business(lesson.business_id) as brain:
            found = await brain.post("/v1/lessons/similar", {"text": markdown[:2000]})
    except BrainError:
        return []
    return [item for item in found if str(item.get("lesson_id")) != str(lesson.id)]


async def propose_skill_change(business_id: uuid.UUID, lesson: Lesson, hint: str) -> None:
    """A lesson about a whole kind of work becomes a proposed skill change, for a human."""
    async with tenant_session(business_id) as session:
        version = await latest_version(session, lesson.id)
        rule = version.rule
    try:
        async with await BrainClient.for_business(business_id) as brain:
            found = await brain.post(
                "/v1/search",
                {
                    "query": f"{hint} {rule}",
                    "knowledge_types": ["skill"],
                    "limit": 1,
                    "mode": "keyword",
                },
            )
            hits = found["hits"]
            if hits and hits[0]["score"] >= 0.25 and hits[0].get("skill_version_id"):
                skills = await brain.get("/v1/skills")
                skill = next(
                    (
                        s
                        for s in skills
                        if s["live"] and str(s["live"]["id"]) == str(hits[0]["skill_version_id"])
                    ),
                    None,
                )
                if skill is not None:
                    markdown = (
                        skill["live"]["markdown"].rstrip()
                        + f"\n\n## From a lesson ({datetime.now(UTC):%Y-%m-%d})\n- {rule}\n"
                    )
                    proposal = await brain.post(
                        "/v1/skill-proposals",
                        {
                            "skill_key": skill["key"],
                            "name": skill["name"],
                            "markdown": markdown,
                            "rationale": f'From the lesson "{lesson.title}": it applies to every {hint or "such"} request.',
                            "changes": [f"Added the rule: {rule}"],
                            "lesson_id": str(lesson.id),
                        },
                    )
                    await _link_proposal(business_id, lesson.id, proposal["id"])
                    return
            markdown = (
                f"# {hint or lesson.title}\n\n## When to use\n{hint or lesson.title}\n\n## Steps\n1. {rule}\n\n"
                f"## Examples\n{version.markdown.split('## Example', 1)[-1].strip()}\n"
            )
            proposal = await brain.post(
                "/v1/skill-proposals",
                {
                    "skill_key": None,
                    "name": hint or lesson.title,
                    "markdown": markdown,
                    "rationale": f'From the lesson "{lesson.title}", which fits a whole kind of work.',
                    "changes": ["A new skill"],
                    "lesson_id": str(lesson.id),
                },
            )
            await _link_proposal(business_id, lesson.id, proposal["id"])
    except BrainError:
        return


async def _link_proposal(business_id: uuid.UUID, lesson_id: uuid.UUID, proposal_id: object) -> None:
    async with tenant_session(business_id) as session:
        lesson = await session.get(Lesson, lesson_id)
        if lesson is not None:
            lesson.skill_proposal_id = uuid.UUID(str(proposal_id))
