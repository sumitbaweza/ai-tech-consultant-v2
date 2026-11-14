"""Instructions, skills, skill proposals, synonyms, past emails, lessons and expert tasks."""

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert

from core.brain_api import ExpertTaskIn
from core.llm.client import LlmClient
from core.llm.types import ChatTurn, LlmError, LlmRequest
from core.models import Business
from core.prompts import live_prompt, render
from knowledge_brain import knowledge
from knowledge_brain.api.deps import BusinessId, Db, UserId
from knowledge_brain.experts import answer_task, open_task
from knowledge_brain.jobs import improve_from_notes, queue_embedding, sync_gmail
from knowledge_brain.models import (
    EmailMessage,
    EmailNote,
    EmailThread,
    ExpertTask,
    Instruction,
    LessonDoc,
    Skill,
    SkillProposal,
    SkillVersion,
    Synonym,
)
from knowledge_brain.search.index import index_lesson
from knowledge_brain.search.search import search

router = APIRouter(prefix="/v1", tags=["Instructions, skills, past emails, lessons, experts"])


class InstructionIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000)
    enabled: bool = True
    all_roles: bool = True
    role_ids: list[uuid.UUID] = Field(default_factory=list[uuid.UUID])


class SkillIn(BaseModel):
    markdown: str = Field(min_length=10, max_length=40000)
    status: str = Field(default="live", pattern="^(live|gating|draft)$")
    note: str | None = None


class StatusIn(BaseModel):
    status: str = Field(pattern="^(live|blocked|retired|gating)$")


class RestoreIn(BaseModel):
    version_id: uuid.UUID
    status: str = Field(default="live", pattern="^(live|gating)$")


class DraftIn(BaseModel):
    description: str = Field(min_length=5, max_length=2000)


class ProposalIn(BaseModel):
    skill_key: str | None = None
    name: str
    markdown: str
    rationale: str
    changes: list[str] = Field(default_factory=list)
    lesson_id: uuid.UUID | None = None


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    message_id: uuid.UUID | None = None


class SynonymIn(BaseModel):
    term: str = Field(min_length=2, max_length=60)
    synonyms: list[str]


class LessonIn(BaseModel):
    topic: str
    title: str
    markdown: str
    version: int = 1


class SimilarIn(BaseModel):
    text: str


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=20000)


# ---------------------------------------------------------------- instructions


def _instruction(i: Instruction) -> dict[str, Any]:
    return {
        "id": i.id,
        "title": i.title,
        "body": i.body,
        "enabled": i.enabled,
        "all_roles": i.all_roles,
        "role_ids": i.role_ids,
        "updated_at": i.updated_at,
    }


@router.get("/instructions", summary="Every instruction")
async def instructions(db: Db) -> list[dict[str, Any]]:
    return [
        _instruction(i)
        for i in (await db.execute(select(Instruction).order_by(Instruction.created_at))).scalars()
    ]


@router.post("/instructions", summary="Write an instruction")
async def add_instruction(
    body: InstructionIn, bid: BusinessId, db: Db, user: UserId
) -> dict[str, Any]:
    saved = await knowledge.save_instruction(
        db, bid, instruction_id=None, user_id=user, **body.model_dump()
    )
    await db.commit()
    await queue_embedding(str(bid))
    return _instruction(saved)


@router.put("/instructions/{instruction_id}", summary="Edit, switch on or off, change its roles")
async def edit_instruction(
    instruction_id: uuid.UUID, body: InstructionIn, bid: BusinessId, db: Db, user: UserId
) -> dict[str, Any]:
    if await db.get(Instruction, instruction_id) is None:
        raise HTTPException(404)
    saved = await knowledge.save_instruction(
        db, bid, instruction_id=instruction_id, user_id=user, **body.model_dump()
    )
    await db.commit()
    await queue_embedding(str(bid))
    return _instruction(saved)


@router.delete("/instructions/{instruction_id}", status_code=204, summary="Delete an instruction")
async def delete_instruction(instruction_id: uuid.UUID, db: Db) -> None:
    await db.execute(delete(Instruction).where(Instruction.id == instruction_id))


# ---------------------------------------------------------------- skills


def _version(v: SkillVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "version": v.version,
        "status": v.status,
        "author": v.author,
        "note": v.note,
        "markdown": v.markdown,
        "when_to_use": v.when_to_use,
        "created_at": v.created_at,
    }


@router.get("/skills", summary="Every skill with its live version")
async def skills(db: Db) -> list[dict[str, Any]]:
    rows = await db.execute(
        select(Skill, SkillVersion)
        .join(
            SkillVersion,
            (SkillVersion.skill_id == Skill.id) & (SkillVersion.status == "live"),
            isouter=True,
        )
        .where(Skill.archived_at.is_(None))
        .order_by(Skill.name)
    )
    waiting = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(SkillProposal.skill_id, func.count())
                .where(SkillProposal.status == "pending")
                .group_by(SkillProposal.skill_id)
            )
        ).all()
    }
    return [
        {
            "id": s.id,
            "key": s.key,
            "name": s.name,
            "live": _version(v) if v else None,
            "proposals": waiting.get(s.id, 0),
        }
        for s, v in rows.all()
    ]


@router.post("/skills", summary="Create a skill (its first version)")
async def create_skill(body: SkillIn, bid: BusinessId, db: Db, user: UserId) -> dict[str, Any]:
    version = await knowledge.create_skill(
        db, bid, body.markdown, author="human", status=body.status, user_id=user, note=body.note
    )
    await db.commit()
    await queue_embedding(str(bid))
    return {"skill_id": version.skill_id, "version": _version(version)}


@router.get("/skills/{skill_id}", summary="A skill with every version and its waiting proposals")
async def skill_detail(skill_id: uuid.UUID, db: Db) -> dict[str, Any]:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(404)
    versions = (
        await db.execute(
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill_id)
            .order_by(SkillVersion.version.desc())
        )
    ).scalars()
    proposals = (
        await db.execute(
            select(SkillProposal)
            .where(SkillProposal.skill_id == skill_id)
            .order_by(SkillProposal.created_at.desc())
        )
    ).scalars()
    return {
        "id": skill.id,
        "key": skill.key,
        "name": skill.name,
        "versions": [_version(v) for v in versions],
        "proposals": [_proposal(p) for p in proposals],
    }


@router.post("/skills/{skill_id}/versions", summary="Save a change as a new version")
async def new_version(
    skill_id: uuid.UUID, body: SkillIn, bid: BusinessId, db: Db, user: UserId
) -> dict[str, Any]:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(404)
    version = await knowledge.add_skill_version(
        db, skill, body.markdown, author="human", status=body.status, user_id=user, note=body.note
    )
    await db.commit()
    await queue_embedding(str(bid))
    return _version(version)


@router.post(
    "/skills/{skill_id}/versions/{version_id}/status",
    summary="Set a version's status (the release gate's verdict)",
)
async def set_status(
    skill_id: uuid.UUID,
    version_id: uuid.UUID,
    body: StatusIn,
    bid: BusinessId,
    db: Db,
    user: UserId,
) -> dict[str, Any]:
    version = await db.get(SkillVersion, version_id)
    if version is None or version.skill_id != skill_id:
        raise HTTPException(404)
    if body.status == "live":
        await knowledge.make_live(db, version, approved_by=user)
    else:
        version.status = body.status
    await db.commit()
    await queue_embedding(str(bid))
    return _version(version)


@router.post("/skills/{skill_id}/restore", summary="Bring back an older version, as a new version")
async def restore_skill(
    skill_id: uuid.UUID, body: RestoreIn, bid: BusinessId, db: Db, user: UserId
) -> dict[str, Any]:
    skill, old = await db.get(Skill, skill_id), await db.get(SkillVersion, body.version_id)
    if skill is None or old is None or old.skill_id != skill_id:
        raise HTTPException(404)
    version = await knowledge.add_skill_version(
        db,
        skill,
        old.markdown,
        author="human",
        status=body.status,
        user_id=user,
        note=f"Restored v{old.version}",
    )
    await db.commit()
    await queue_embedding(str(bid))
    return _version(version)


@router.post(
    "/skills/draft", summary="Describe a skill in a few words; the agent drafts it (not saved)"
)
async def draft_skill(body: DraftIn, bid: BusinessId, db: Db) -> dict[str, Any]:
    found = await search(db, body.description, limit=5, mode="keyword")
    snippets = "\n\n".join(hit.content[:800] for hit in found.hits) or "(no knowledge found)"
    prompt = await live_prompt(db, "brain.draft_skill")
    business = await db.get(Business, bid)
    try:
        result = await LlmClient(bid, role="brain").call(
            LlmRequest(
                business_id=bid,
                step="brain.draft_skill",
                job="improve_skill",
                system=[render(prompt.body, {"business_name": business.name if business else ""})],
                messages=[
                    ChatTurn(
                        "user",
                        f"Description: {body.description}\n\nKnowledge snippets:\n{snippets}",
                    )
                ],
                schema=knowledge.SkillDraft,
                max_tokens=12000,
                effort="medium",
                prompt_key=prompt.key,
                prompt_version_id=prompt.version_id,
            )
        )
    except LlmError as exc:
        raise HTTPException(503, f"The drafting model isn't available: {exc}") from exc
    assert isinstance(result.parsed, knowledge.SkillDraft)
    return result.parsed.model_dump()


def _proposal(p: SkillProposal) -> dict[str, Any]:
    return {
        "id": p.id,
        "skill_id": p.skill_id,
        "origin": p.origin,
        "name": p.proposed_name,
        "markdown": p.proposed_markdown,
        "rationale": p.rationale,
        "changes": p.changes,
        "note_ids": p.note_ids,
        "lesson_id": p.lesson_id,
        "status": p.status,
        "model_key": p.model_key,
        "created_at": p.created_at,
    }


@router.get("/skill-proposals", summary="Skill changes waiting for a human")
async def proposals(db: Db, status: str = "pending") -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(SkillProposal)
            .where(SkillProposal.status == status)
            .order_by(SkillProposal.created_at.desc())
        )
    ).scalars()
    return [_proposal(p) for p in rows]


@router.post(
    "/skill-proposals",
    summary="Propose a skill change (from a lesson that fits a whole kind of work)",
)
async def propose(body: ProposalIn, bid: BusinessId, db: Db) -> dict[str, Any]:
    skill = (
        (await db.execute(select(Skill).where(Skill.key == body.skill_key))).scalar_one_or_none()
        if body.skill_key
        else None
    )
    proposal = SkillProposal(
        business_id=bid,
        skill_id=skill.id if skill else None,
        origin="lesson",
        proposed_name=body.name,
        proposed_markdown=body.markdown,
        rationale=body.rationale,
        changes=body.changes,
        lesson_id=body.lesson_id,
    )
    db.add(proposal)
    await db.flush()
    return _proposal(proposal)


@router.post(
    "/skill-proposals/{proposal_id}/approve",
    summary="Approve: the change becomes a new skill version",
)
async def approve(
    proposal_id: uuid.UUID, body: StatusIn, bid: BusinessId, db: Db, user: UserId
) -> dict[str, Any]:
    proposal = await db.get(SkillProposal, proposal_id)
    if proposal is None or proposal.status != "pending":
        raise HTTPException(404, "No such pending proposal")
    version = await knowledge.approve_proposal(
        db, proposal, status=body.status if body.status != "blocked" else "gating", user_id=user
    )
    await db.commit()
    await queue_embedding(str(bid))
    return {"skill_id": version.skill_id, "version": _version(version)}


@router.post("/skill-proposals/{proposal_id}/reject", summary="Reject a proposed change")
async def reject(proposal_id: uuid.UUID, db: Db, user: UserId) -> dict[str, str]:
    proposal = await db.get(SkillProposal, proposal_id)
    if proposal is None:
        raise HTTPException(404)
    proposal.status, proposal.decided_by, proposal.decided_at = "rejected", user, datetime.now(UTC)
    return {"status": "rejected"}


# ---------------------------------------------------------------- synonyms


@router.get("/synonyms", summary="The business's synonym list")
async def synonyms(db: Db) -> list[dict[str, Any]]:
    return [
        {"term": s.term, "synonyms": s.synonyms}
        for s in (await db.execute(select(Synonym).order_by(Synonym.term))).scalars()
    ]


@router.put("/synonyms", summary="Set the words that mean the same as a word")
async def put_synonyms(body: SynonymIn, bid: BusinessId, db: Db) -> dict[str, Any]:
    statement = insert(Synonym).values(
        business_id=bid, term=body.term.lower(), synonyms=body.synonyms
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[Synonym.business_id, Synonym.term], set_={"synonyms": body.synonyms}
        )
    )
    return body.model_dump()


# ---------------------------------------------------------------- past emails


@router.post("/gmail/sync", summary="Fetch past threads from the connected Gmail")
async def gmail_sync(bid: BusinessId) -> dict[str, str]:
    await sync_gmail.defer_async(business_id=str(bid))
    return {"status": "queued"}


@router.get("/email-threads", summary="Fetched threads, newest first")
async def threads(db: Db, limit: int = 200) -> list[dict[str, Any]]:
    notes = {
        row[0]: row[1]
        for row in (
            await db.execute(
                select(EmailNote.thread_id, func.count()).group_by(EmailNote.thread_id)
            )
        ).all()
    }
    rows = (
        await db.execute(
            select(EmailThread)
            .order_by(EmailThread.last_message_at.desc().nulls_last())
            .limit(min(limit, 500))
        )
    ).scalars()
    return [
        {
            "id": t.id,
            "subject": t.subject,
            "participants": t.participants,
            "message_count": t.message_count,
            "last_message_at": t.last_message_at,
            "snippet": t.snippet,
            "notes": notes.get(t.id, 0),
        }
        for t in rows
    ]


@router.get("/email-threads/{thread_id}", summary="A thread with every message and its notes")
async def thread(thread_id: uuid.UUID, db: Db) -> dict[str, Any]:
    item = await db.get(EmailThread, thread_id)
    if item is None:
        raise HTTPException(404)
    messages = (
        await db.execute(
            select(EmailMessage)
            .where(EmailMessage.thread_id == thread_id)
            .order_by(EmailMessage.position)
        )
    ).scalars()
    notes = (
        await db.execute(
            select(EmailNote).where(EmailNote.thread_id == thread_id).order_by(EmailNote.created_at)
        )
    ).scalars()
    return {
        "id": item.id,
        "subject": item.subject,
        "participants": item.participants,
        "messages": [
            {
                "id": m.id,
                "position": m.position,
                "from": m.from_addr,
                "to": m.to_addrs,
                "sent_at": m.sent_at,
                "body": m.body_text,
                "attachments": m.attachments,
            }
            for m in messages
        ],
        "notes": [
            {
                "id": n.id,
                "message_id": n.message_id,
                "body": n.body,
                "status": n.status,
                "proposal_id": n.proposal_id,
                "created_at": n.created_at,
            }
            for n in notes
        ],
    }


@router.post(
    "/email-threads/{thread_id}/notes", summary="Write a note on the thread or on one message"
)
async def add_note(
    thread_id: uuid.UUID, body: NoteIn, bid: BusinessId, db: Db, user: UserId
) -> dict[str, Any]:
    if await db.get(EmailThread, thread_id) is None:
        raise HTTPException(404)
    note = EmailNote(
        business_id=bid,
        thread_id=thread_id,
        message_id=body.message_id,
        body=body.body,
        author_id=user,
    )
    db.add(note)
    await db.flush()
    return {"id": note.id}


@router.post("/email-notes/improve", summary="Opus proposes skill changes from the new notes")
async def improve(bid: BusinessId, user: UserId) -> dict[str, str]:
    await improve_from_notes.defer_async(business_id=str(bid), user_id=str(user) if user else None)
    return {"status": "queued"}


# ---------------------------------------------------------------- lessons (copied in by the chat)


@router.put(
    "/lessons/{lesson_id}", summary="Index an approved lesson so answers can use and cite it"
)
async def put_lesson(
    lesson_id: uuid.UUID, body: LessonIn, bid: BusinessId, db: Db
) -> dict[str, str]:
    doc = await db.get(LessonDoc, lesson_id) or LessonDoc(
        id=lesson_id,
        business_id=bid,
        topic=body.topic,
        title=body.title,
        markdown=body.markdown,
        version=body.version,
    )
    doc.topic, doc.title, doc.markdown, doc.version, doc.updated_at = (
        body.topic,
        body.title,
        body.markdown,
        body.version,
        datetime.now(UTC),
    )
    db.add(doc)
    await db.flush()
    await index_lesson(db, doc)
    await db.commit()
    await queue_embedding(str(bid))
    return {"status": "indexed"}


@router.delete(
    "/lessons/{lesson_id}",
    status_code=204,
    summary="Switch a lesson off or delete it: it leaves answers",
)
async def delete_lesson(lesson_id: uuid.UUID, db: Db) -> None:
    await db.execute(delete(LessonDoc).where(LessonDoc.id == lesson_id))


@router.post("/lessons/similar", summary="Approved lessons that may disagree with a new one")
async def similar_lessons(body: SimilarIn, db: Db) -> list[dict[str, Any]]:
    found = await search(db, body.text, knowledge_types=["lesson"], limit=5, mode="keyword")
    return [
        {"lesson_id": h.lesson_id, "title": h.title, "score": h.score}
        for h in found.hits
        if h.score >= 0.3
    ]


# ---------------------------------------------------------------- expert tasks


def _task(t: ExpertTask) -> dict[str, Any]:
    return {
        "id": t.id,
        "question": t.question,
        "reason": t.reason,
        "status": t.status,
        "asked_count": t.asked_count,
        "context": t.context,
        "answer": t.answer,
        "answered_at": t.answered_at,
        "created_at": t.created_at,
        "last_asked_at": t.last_asked_at,
    }


@router.get("/expert-tasks", summary="Questions waiting for a human expert")
async def expert_tasks(db: Db, status: str = "open") -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(ExpertTask)
            .where(ExpertTask.status == status)
            .order_by(ExpertTask.asked_count.desc(), ExpertTask.last_asked_at.desc())
        )
    ).scalars()
    return [_task(t) for t in rows]


@router.post("/expert-tasks", summary="Ask a human: open a task (or count a repeat)")
async def new_expert_task(body: ExpertTaskIn, bid: BusinessId, db: Db) -> dict[str, Any]:
    return _task(await open_task(db, bid, body.question, body.reason, body.context))


@router.post("/expert-tasks/{task_id}/answer", summary="Answer it: the answer becomes knowledge")
async def answer_expert(
    task_id: uuid.UUID, body: AnswerIn, bid: BusinessId, db: Db, user: UserId
) -> dict[str, Any]:
    task = await db.get(ExpertTask, task_id)
    if task is None:
        raise HTTPException(404)
    page = await answer_task(db, task, body.answer, user)
    await db.commit()
    await queue_embedding(str(bid))
    return {"task": _task(task), "page_id": page.id}


@router.post("/expert-tasks/{task_id}/dismiss", summary="Dismiss a task")
async def dismiss(task_id: uuid.UUID, db: Db) -> dict[str, str]:
    task = await db.get(ExpertTask, task_id)
    if task is None:
        raise HTTPException(404)
    task.status = "dismissed"
    return {"status": "dismissed"}
