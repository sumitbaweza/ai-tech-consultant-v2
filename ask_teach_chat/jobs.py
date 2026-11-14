"""The chat's background jobs: lessons written from the comment box (Opus can take a minute)."""

import uuid

from procrastinate import Blueprint

from ask_teach_chat import teach
from core.jobs import RETRY

bp = Blueprint()


@bp.task(name="lesson_from_feedback", queue="web", retry=RETRY)
async def lesson_from_feedback(business_id: str, feedback_id: str) -> None:
    await teach.lesson_from_feedback(uuid.UUID(business_id), uuid.UUID(feedback_id))
