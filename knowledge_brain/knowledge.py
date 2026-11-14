"""Instructions and skills (Task 2.5), and skill changes proposed from notes (Task 2.6).

Instructions are rules in plain words, for all roles or chosen ones. Skills are Markdown guides
(name, when to use, steps, examples), versioned: every change is a new version, exactly one is
live, and an older version comes back in one click (copied into a new version). The release
gate (Eval Lab, Task 4.5) decides when a new skill version goes live; the Brain only stores the
status it is told.
"""

import re
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.brain_api import SkillText
from knowledge_brain.models import (
    Chunk,
    EmailNote,
    Instruction,
    Skill,
    SkillProposal,
    SkillVersion,
)
from knowledge_brain.search.index import index_instruction, index_skill_version
from knowledge_brain.search.search import search

SKILL_MATCH_MIN = 0.25  # below this, no skill is a good fit for the question


class SkillDraft(BaseModel):
    name: str
    when_to_use: str
    markdown: str


# ---------------------------------------------------------------- instructions


async def save_instruction(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    instruction_id: uuid.UUID | None,
    title: str,
    body: str,
    enabled: bool,
    all_roles: bool,
    role_ids: list[uuid.UUID],
    user_id: uuid.UUID | None,
) -> Instruction:
    instruction = await session.get(Instruction, instruction_id) if instruction_id else None
    if instruction is None:
        instruction = Instruction(
            business_id=business_id, created_by=user_id, title=title, body=body
        )
        session.add(instruction)
    instruction.title, instruction.body, instruction.enabled = title.strip(), body.strip(), enabled
    instruction.all_roles = all_roles
    instruction.role_ids = [] if all_roles else role_ids
    instruction.updated_at = datetime.now(UTC)
    await session.flush()
    await index_instruction(session, instruction)
    return instruction


async def instructions_for_role(
    session: AsyncSession, role_id: uuid.UUID | None
) -> list[Instruction]:
    rows = (
        (
            await session.execute(
                select(Instruction)
                .where(Instruction.enabled.is_(True))
                .order_by(Instruction.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [i for i in rows if i.all_roles or (role_id is not None and role_id in i.role_ids)]


# ---------------------------------------------------------------- skills


def parse_skill(markdown: str) -> tuple[str, str]:
    """(name, when to use) from the skill's Markdown."""
    name_match = re.search(r"^#\s+(.+)$", markdown, re.MULTILINE)
    when_match = re.search(
        r"^##\s*When to use\s*\n+(.+?)(?:\n##|\Z)",
        markdown,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    name = name_match.group(1).strip() if name_match else "Untitled skill"
    when = " ".join(when_match.group(1).split()) if when_match else ""
    return name[:200], when[:1000]


def skill_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "skill"


async def create_skill(
    session: AsyncSession,
    business_id: uuid.UUID,
    markdown: str,
    *,
    author: str,
    status: str,
    user_id: uuid.UUID | None,
    note: str | None = None,
) -> SkillVersion:
    name, _ = parse_skill(markdown)
    key = skill_key(name)
    if await session.scalar(select(func.count()).select_from(Skill).where(Skill.key == key)):
        key = f"{key}-{uuid.uuid4().hex[:4]}"
    skill = Skill(business_id=business_id, key=key, name=name)
    session.add(skill)
    await session.flush()
    return await add_skill_version(
        session, skill, markdown, author=author, status=status, user_id=user_id, note=note
    )


async def add_skill_version(
    session: AsyncSession,
    skill: Skill,
    markdown: str,
    *,
    author: str,
    status: str,
    user_id: uuid.UUID | None,
    note: str | None = None,
    proposal_id: uuid.UUID | None = None,
) -> SkillVersion:
    name, when = parse_skill(markdown)
    number = (
        int(
            await session.scalar(
                select(func.max(SkillVersion.version)).where(SkillVersion.skill_id == skill.id)
            )
            or 0
        )
        + 1
    )
    version = SkillVersion(
        business_id=skill.business_id,
        skill_id=skill.id,
        version=number,
        markdown=markdown,
        when_to_use=when,
        status="draft",
        author=author,
        note=note,
        created_by=user_id,
        proposal_id=proposal_id,
    )
    session.add(version)
    await session.flush()
    skill.name = name
    if status == "live":
        await make_live(session, version, approved_by=user_id)
    else:
        version.status = status
    return version


async def make_live(
    session: AsyncSession, version: SkillVersion, *, approved_by: uuid.UUID | None
) -> None:
    await session.execute(
        update(SkillVersion)
        .where(SkillVersion.skill_id == version.skill_id, SkillVersion.status == "live")
        .values(status="retired")
    )
    version.status = "live"
    version.approved_by = approved_by
    await session.flush()
    skill = await session.get(Skill, version.skill_id)
    await index_skill_version(session, version, skill.name if skill else "Skill")


async def skill_text(
    session: AsyncSession, version_id: uuid.UUID, score: float = 0.0
) -> SkillText | None:
    row = (
        await session.execute(
            select(SkillVersion, Skill)
            .join(Skill, Skill.id == SkillVersion.skill_id)
            .where(SkillVersion.id == version_id)
        )
    ).first()
    if row is None:
        return None
    version, skill = row
    return SkillText(
        skill_id=skill.id,
        version_id=version.id,
        key=skill.key,
        name=skill.name,
        version=version.version,
        when_to_use=version.when_to_use,
        markdown=version.markdown,
        match_score=score,
    )


async def select_skill(
    session: AsyncSession, question: str, allowed_keys: list[str] | None
) -> SkillText | None:
    """The agent picks the right skill itself (Task 2.5): the live skill whose text best matches
    the question, among the skills the role may use."""
    found = await search(session, question, knowledge_types=["skill"], limit=5, mode="keyword")
    for hit in found.hits:
        if hit.skill_version_id is None or hit.score < SKILL_MATCH_MIN:
            continue
        text = await skill_text(session, hit.skill_version_id, hit.score)
        if text is not None and (not allowed_keys or text.key in allowed_keys):
            return text
    return None


# ---------------------------------------------------------------- proposals (Task 2.6)


async def approve_proposal(
    session: AsyncSession, proposal: SkillProposal, *, status: str, user_id: uuid.UUID | None
) -> SkillVersion:
    """A human approved Opus's change: it becomes a new version of the skill (or a new skill)."""
    if proposal.skill_id is not None:
        skill = await session.get(Skill, proposal.skill_id)
        assert skill is not None
        version = await add_skill_version(
            session,
            skill,
            proposal.proposed_markdown,
            author="from_notes" if proposal.origin == "email_notes" else "from_lesson",
            status=status,
            user_id=user_id,
            note=proposal.rationale,
            proposal_id=proposal.id,
        )
    else:
        version = await create_skill(
            session,
            proposal.business_id,
            proposal.proposed_markdown,
            author="from_notes" if proposal.origin == "email_notes" else "from_lesson",
            status=status,
            user_id=user_id,
            note=proposal.rationale,
        )
        version.proposal_id = proposal.id
        proposal.skill_id = version.skill_id
    proposal.status, proposal.decided_by, proposal.decided_at = (
        "approved",
        user_id,
        datetime.now(UTC),
    )
    await session.execute(
        update(EmailNote).where(EmailNote.id.in_(proposal.note_ids)).values(status="used")
    )
    return version


async def live_chunk_count(session: AsyncSession) -> int:
    return int(
        await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.knowledge_type == "skill")
        )
        or 0
    )
