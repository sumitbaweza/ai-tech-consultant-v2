"""The release gate (Task 4.5): a new prompt, role, skill or model choice goes live only if no
score falls below the live version's, on the same gate cases.

Saving in Settings makes the new version `gating` and asks for a gate. The gate job
(eval_lab/jobs.py) answers the gate cases with the live version and with the candidate, then
promotes the candidate (`live`) or blocks it with the scores that dropped. When no test case
covers the change yet, there is no score that could drop: the version goes live and the gate
says so, so the owner knows to add cases.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import ModelChoice, PromptVersion, RoleVersion
from core.prompts import make_live
from eval_lab.models import Benchmark, Case, Gate, HumanGrade

TargetKind = Literal["prompt", "role", "skill", "model_choice"]
GATE_CASES = 20  # per gate: enough to catch a drop, small enough to run in a few minutes


def benchmark_kinds_for(target_kind: str, target_key: str) -> tuple[str, ...]:
    """Which benchmark sets show whether this change made answers worse."""
    if target_kind == "prompt" and target_key.startswith("brain."):
        return ("brain",)
    if target_kind == "prompt" and target_key.startswith("eval.judge"):
        return ()  # the judge is gated on hand grades instead (judge_agreement)
    if target_kind == "model_choice" and target_key in ("judge", "judge_second"):
        return ()
    return ("chat", "brain", "starter")


async def gate_case_ids(
    session: AsyncSession, target_kind: str, target_key: str
) -> list[uuid.UUID]:
    kinds = benchmark_kinds_for(target_kind, target_key)
    if not kinds:
        return []
    rows = await session.execute(
        select(Case.id)
        .join(Benchmark, Benchmark.id == Case.benchmark_id)
        .where(Benchmark.kind.in_(kinds), Benchmark.hidden.is_(False))
        .where(
            Case.category.in_(
                (
                    "regular",
                    "screenshot",
                    "instructions",
                    "synonym_typo",
                    "handoff",
                    "trick",
                    "not_in_kb",
                    "no_answer",
                )
            )
        )
        .order_by(Case.case_key)
        .limit(GATE_CASES)
    )
    return [row[0] for row in rows]


async def human_grade_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(HumanGrade)) or 0)


async def request_gate(
    session: AsyncSession,
    business_id: uuid.UUID,
    *,
    target_kind: TargetKind,
    target_key: str,
    candidate_ref: str,
    baseline_ref: str | None,
    user_id: uuid.UUID | None,
) -> tuple[Gate, bool]:
    """Record a gate. Returns (gate, needs_run): False when there was nothing to compare."""
    gate = Gate(
        business_id=business_id,
        target_kind=target_kind,
        target_key=target_key,
        candidate_ref=candidate_ref,
        baseline_ref=baseline_ref,
        created_by=user_id,
    )
    session.add(gate)
    await session.flush()
    judge_change = not benchmark_kinds_for(target_kind, target_key)
    has_cases = (
        (await human_grade_count(session) > 0)
        if judge_change
        else bool(await gate_case_ids(session, target_kind, target_key))
    )
    if baseline_ref is None or not has_cases:
        gate.status = "passed"
        gate.reason = (
            "Nothing to compare yet: no test case covers this change, so no score could drop. "
            "Load a benchmark (or grade results by hand, for the judge) to gate future changes."
            if baseline_ref is not None
            else "First version: nothing to compare with."
        )
        gate.finished_at = datetime.now(UTC)
        await activate(session, gate)
        return gate, False
    return gate, True


async def activate(session: AsyncSession, gate: Gate) -> None:
    """Make the candidate live. Skills live in the Brain: the gate job does that over the API."""
    candidate = gate.candidate_ref
    if gate.target_kind == "prompt":
        version = await session.get(PromptVersion, uuid.UUID(candidate))
        if version is not None:
            version.gate_id = gate.id
            await make_live(session, version)
    elif gate.target_kind == "role":
        version = await session.get(RoleVersion, uuid.UUID(candidate))
        if version is not None:
            await session.execute(
                update(RoleVersion)
                .where(RoleVersion.role_id == version.role_id, RoleVersion.status == "live")
                .values(status="retired")
            )
            version.status = "live"
            version.gate_id = gate.id
    elif gate.target_kind == "model_choice":
        choice = (
            await session.execute(select(ModelChoice).where(ModelChoice.step == gate.target_key))
        ).scalar_one_or_none()
        if choice is None:
            session.add(
                ModelChoice(business_id=gate.business_id, step=gate.target_key, model_key=candidate)
            )
        else:
            choice.model_key = candidate
            choice.updated_at = datetime.now(UTC)
    await session.flush()


async def block(session: AsyncSession, gate: Gate, reason: str) -> None:
    gate.status = "blocked"
    gate.reason = reason
    gate.finished_at = datetime.now(UTC)
    if gate.target_kind == "prompt":
        version = await session.get(PromptVersion, uuid.UUID(gate.candidate_ref))
        if version is not None:
            version.status, version.gate_id = "blocked", gate.id
    elif gate.target_kind == "role":
        version = await session.get(RoleVersion, uuid.UUID(gate.candidate_ref))
        if version is not None:
            version.status, version.gate_id = "blocked", gate.id
    await session.flush()
