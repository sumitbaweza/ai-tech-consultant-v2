"""The Eval Lab's background jobs (queue "web"). Runs save case by case, so a retry after an
outage carries on where the run stopped."""

import uuid

from procrastinate import Blueprint

from core.config import get_settings
from core.jobs import RETRY
from eval_lab import runner, special

bp = Blueprint()


def _uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


@bp.task(name="run_eval", queue="web", retry=RETRY)
async def run_eval(business_id: str, run_id: str) -> None:
    await runner.run_benchmark(uuid.UUID(business_id), uuid.UUID(run_id))


@bp.task(name="run_gate", queue="web", retry=RETRY)
async def run_gate(business_id: str, gate_id: str) -> None:
    await special.run_gate(uuid.UUID(business_id), uuid.UUID(gate_id))


@bp.task(name="caching_proof", queue="web", retry=RETRY)
async def caching_proof(business_id: str, tasks: int = 100, user_id: str | None = None) -> None:
    await special.caching_proof(uuid.UUID(business_id), tasks, _uuid(user_id))


@bp.task(name="router_proof", queue="web", retry=RETRY)
async def router_proof(business_id: str, cases: int = 30, user_id: str | None = None) -> None:
    await special.router_proof(uuid.UUID(business_id), cases, _uuid(user_id))


@bp.task(name="lesson_check", queue="web", retry=RETRY)
async def lesson_check(business_id: str, lesson_id: str, user_id: str | None = None) -> None:
    await special.lesson_check(uuid.UUID(business_id), uuid.UUID(lesson_id), _uuid(user_id))


@bp.task(name="url_cases", queue="web", retry=RETRY)
async def url_cases(business_id: str, run_id: str, user_id: str, site: str | None = None) -> None:
    await special.url_cases(
        uuid.UUID(business_id),
        uuid.UUID(run_id),
        user_id=uuid.UUID(user_id),
        site=site or get_settings().fixture_site_url,
    )


@bp.task(name="retrieval_check", queue="web", retry=RETRY)
async def retrieval_check(business_id: str, run_id: str) -> None:
    await special.retrieval_check(uuid.UUID(business_id), uuid.UUID(run_id))


@bp.task(name="teach_tests", queue="web", retry=RETRY)
async def teach_tests(business_id: str, run_id: str, user_id: str | None = None) -> None:
    await special.teach_tests(uuid.UUID(business_id), uuid.UUID(run_id), _uuid(user_id))
