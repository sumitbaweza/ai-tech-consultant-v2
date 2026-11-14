"""Background jobs (Procrastinate, jobs stored in Postgres) behind one small interface.

Every job retries with growing waits: 2 s, 4 s, 8 s ... about 34 minutes in all over 10 tries.
So when a model API is switched off mid-run, the job fails, waits and carries on when the API
is back; long jobs save their progress item by item, so a retry repeats nothing that finished
(Task 1.5). A job that still fails after 10 tries lands in the "failed jobs" list (Logs and
costs > Failed jobs), where a person can retry it.

Job arguments carry ids only (business id, run id, ...), never a business's content.
"""

import uuid
from typing import Any, Protocol

from procrastinate import App, Blueprint, PsycopgConnector, RetryStrategy
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import DbRole, get_settings

RETRY = RetryStrategy(max_attempts=10, exponential_wait=2)


def make_app(role: DbRole, blueprints: dict[str, Blueprint]) -> App:
    """A Procrastinate app connected as one database role, with the given task groups."""
    app = App(connector=PsycopgConnector(conninfo=get_settings().conninfo(role)))
    for namespace, blueprint in blueprints.items():
        app.add_tasks_from(blueprint, namespace=namespace)
    return app


class JobQueue(Protocol):
    """What callers use to start a job: the Procrastinate details stay behind it."""

    async def enqueue(
        self, task: str, *, queue: str, lock: str | None = None, **kwargs: Any
    ) -> int: ...


class ProcrastinateQueue:
    def __init__(self, app: App) -> None:
        self.app = app

    async def enqueue(
        self, task: str, *, queue: str, lock: str | None = None, **kwargs: Any
    ) -> int:
        # By name, so one service can start the other's job without importing its code.
        # A lock runs jobs with the same lock one at a time (e.g. one crawl per source).
        deferrer = self.app.configure_task(task, queue=queue, lock=lock)
        return await deferrer.defer_async(**kwargs)


# ---------------------------------------------------------------- the failed-jobs list


async def failed_jobs(session: AsyncSession, business_id: uuid.UUID) -> list[dict[str, Any]]:
    """Jobs of this business that failed every retry. Job tables have no row-level security
    (both services use them), so the filter on the business id is explicit here."""
    rows = await session.execute(
        text(
            """
            SELECT id, task_name, queue_name, attempts, scheduled_at, args
            FROM procrastinate_jobs
            WHERE status = 'failed' AND args->>'business_id' = :business
            ORDER BY id DESC
            LIMIT 200
            """
        ),
        {"business": str(business_id)},
    )
    return [dict(row) for row in rows.mappings()]


async def job_counts(session: AsyncSession, business_id: uuid.UUID) -> dict[str, int]:
    rows = await session.execute(
        text(
            """
            SELECT status::text AS status, count(*) AS n
            FROM procrastinate_jobs
            WHERE args->>'business_id' = :business
            GROUP BY status
            """
        ),
        {"business": str(business_id)},
    )
    return {row.status: int(row.n) for row in rows}


async def delete_business_jobs(session: AsyncSession, business_id: uuid.UUID) -> int:
    """Part of deleting a business (D14): queued and finished jobs that name it."""
    result = await session.execute(
        text(
            "DELETE FROM procrastinate_jobs WHERE args->>'business_id' = :business "
            "AND status <> 'doing'"
        ),
        {"business": str(business_id)},
    )
    return int(getattr(result, "rowcount", 0) or 0)
