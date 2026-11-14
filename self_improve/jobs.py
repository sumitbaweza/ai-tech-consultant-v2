"""The Self-Improve Loop's jobs. Everything that touches git runs on the "improve" queue, in
the worker-improve service: the only one with the repo mounted (at /repo).

run_loop has no retry: a run that crashed half-way is marked "error" rather than started
again, because a second start would pay for Opus twice.
"""

import uuid

from procrastinate import Blueprint
from sqlalchemy import text

from core.db import engine
from core.jobs import RETRY
from self_improve import loop, review, weekly
from web.state import queue

bp = Blueprint()


@bp.task(name="run_loop", queue="improve")
async def run_loop(business_id: str, run_id: str) -> None:
    await loop.run_loop(uuid.UUID(business_id), uuid.UUID(run_id))


@bp.task(name="approve", queue="improve")
async def approve(business_id: str, run_id: str, user_id: str, who: str) -> None:
    decision = await review.approve(
        uuid.UUID(business_id), uuid.UUID(run_id), user_id=uuid.UUID(user_id), who=who
    )
    for gate_id in decision.gates_to_run:
        await queue().enqueue(
            "evals:run_gate", queue="web", business_id=business_id, gate_id=str(gate_id)
        )


@bp.task(name="undo", queue="improve")
async def undo(business_id: str, run_id: str, user_id: str, who: str) -> None:
    await review.undo(
        uuid.UUID(business_id), uuid.UUID(run_id), user_id=uuid.UUID(user_id), who=who
    )


@bp.task(name="reject", queue="improve")
async def reject(business_id: str, run_id: str, user_id: str) -> None:
    await review.reject(uuid.UUID(business_id), uuid.UUID(run_id), user_id=uuid.UUID(user_id))


@bp.periodic(cron="0 7 * * 1")
@bp.task(name="weekly_emails", queue="web")
async def weekly_emails(timestamp: int) -> None:
    """Monday 07:00 UTC: last week's email for every business."""
    async with engine("web").connect() as connection:
        business_ids = (
            (await connection.execute(text("SELECT core.list_business_ids()"))).scalars().all()
        )
    for business_id in business_ids:
        await weekly.make_weekly(business_id)


@bp.task(name="weekly_now", queue="web", retry=RETRY)
async def weekly_now(business_id: str) -> None:
    await weekly.make_weekly(uuid.UUID(business_id))
