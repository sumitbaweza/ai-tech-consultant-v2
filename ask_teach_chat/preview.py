"""The test chats beside the role editor (Task 1.4) and the skill editor (Task 2.5).

They answer with the editor's unsaved values, through the same pipeline as the chat, and save
nothing but a trace (kind role_test or skill_test) and its model calls.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from ask_teach_chat.pipeline import Outcome, Overrides, RoleSettings, answer
from core.db import tenant_session
from core.llm.types import LlmError
from core.models import Role, RoleVersion


@dataclass
class Preview:
    outcome: Outcome | None
    error: str | None


async def default_role(business_id: uuid.UUID) -> RoleSettings:
    async with tenant_session(business_id) as session:
        row = (
            await session.execute(
                select(Role, RoleVersion)
                .join(RoleVersion, RoleVersion.role_id == Role.id)
                .where(RoleVersion.status == "live", Role.archived_at.is_(None))
                # The Support Executive sorts last, so it is picked when it exists
                .order_by(Role.key == "support-executive", Role.created_at)
            )
        ).all()
    if not row:
        raise LookupError("This business has no role yet")
    role, version = row[-1]
    return RoleSettings.from_version(role.name, version)


async def preview_answer(
    business_id: uuid.UUID,
    *,
    question: str,
    user_id: uuid.UUID | None,
    role_settings: dict[str, Any] | None = None,
    skill_markdown: str | None = None,
) -> Preview:
    if role_settings is not None:
        role = RoleSettings(
            role_id=None,
            version_id=None,
            name=role_settings["name"] or "New role",
            goal=role_settings["goal"],
            prompt=role_settings["prompt"],
            tone=role_settings["tone"],
            capabilities=role_settings["capabilities"],
            knowledge_types=role_settings["knowledge_types"],
            skill_keys=role_settings["skill_keys"],
            model_key=role_settings["model_key"],
            handover_note=str(role_settings["handover"].get("note", "")),
        )
        kind = "role_test"
    else:
        role = await default_role(business_id)
        kind = "skill_test"
    try:
        outcome = await answer(
            business_id,
            text=question,
            role=role,
            overrides=Overrides(skill_markdown=skill_markdown),
            trace_kind=kind,
        )
    except LlmError as exc:
        return Preview(None, f"The model isn't available: {exc}")
    return Preview(outcome, None)
