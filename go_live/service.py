"""The setup run: which step the owner is on, the stopwatch, and the readiness score."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from go_live.models import STEPS, Setup

LABELS = {
    "business": "Business and tone",
    "keys": "Keys",
    "knowledge": "Knowledge",
    "gmail": "Gmail",
    "roles": "Roles",
    "build": "Build",
    "readiness": "Readiness",
}
TONES = (
    "friendly, clear and short",
    "formal and precise",
    "warm and patient",
    "technical and direct",
)


async def current(
    session: AsyncSession, business_id: uuid.UUID, user_id: uuid.UUID | None
) -> Setup:
    """The business's latest setup, or a new one: the stopwatch starts here."""
    setup = (
        await session.execute(select(Setup).order_by(Setup.started_at.desc()).limit(1))
    ).scalar_one_or_none()
    if setup is None:
        setup = Setup(business_id=business_id, created_by=user_id, started_at=datetime.now(UTC))
        session.add(setup)
        await session.flush()
    return setup


def mark(setup: Setup, step: str, event: str) -> None:
    """Record when a step started or was done (a new dict, so the JSON column is saved)."""
    times: dict[str, Any] = {k: dict(v) for k, v in (setup.step_times or {}).items()}
    entry = times.setdefault(step, {})
    if event == "started" and "started_at" in entry:
        return
    entry[f"{event}_at"] = datetime.now(UTC).isoformat()
    setup.step_times = times


def done_steps(setup: Setup) -> set[str]:
    return {step for step, entry in (setup.step_times or {}).items() if "done_at" in entry}


def next_step(step: str) -> str:
    index = STEPS.index(step)
    return STEPS[min(index + 1, len(STEPS) - 1)]


def seconds_taken(setup: Setup) -> int:
    end = setup.finished_at or datetime.now(UTC)
    return int((end - setup.started_at).total_seconds())


def step_seconds(setup: Setup) -> dict[str, int]:
    out: dict[str, int] = {}
    for step, entry in (setup.step_times or {}).items():
        if "started_at" in entry and "done_at" in entry:
            started = datetime.fromisoformat(str(entry["started_at"]))
            out[step] = int(
                (datetime.fromisoformat(str(entry["done_at"])) - started).total_seconds()
            )
    return out


def score(setup: Setup) -> float | None:
    if not setup.readiness_total:
        return None
    return (setup.readiness_correct or 0) / setup.readiness_total
