"""Go Live's build step, as one background job (queue "web"):

1. wait until the starter slice of every source is read (the full read carries on after),
2. write the starting test set from those pages (Task 6.2),
3. run it through the chat, the way the team will use it, for the readiness score (Task 6.3).
"""

import asyncio
import uuid
from datetime import UTC, datetime

from procrastinate import Blueprint

from core.brain_client import BrainClient, BrainError
from core.db import tenant_session
from eval_lab import runner
from eval_lab.models import Run
from go_live import service
from go_live.models import Setup
from go_live.starter import make_starter_tests

bp = Blueprint()
WAIT_SECONDS = 40 * 60  # the starter slice takes a few minutes; give up waiting after 40
POLL_SECONDS = 5.0
FINISHED = ("done", "failed", "stopped", "cancelled")


async def wait_for_reads(
    business_id: uuid.UUID,
    source_ids: list[uuid.UUID],
    *,
    poll: float = POLL_SECONDS,
    limit_seconds: float = WAIT_SECONDS,
) -> None:
    deadline = asyncio.get_running_loop().time() + limit_seconds
    async with await BrainClient.for_business(business_id) as brain:
        while True:
            states = [
                await brain.get(f"/v1/sources/{source_id}/progress") for source_id in source_ids
            ]
            if all(
                s.get("status") in FINISHED
                or (s.get("kind") == "starter" and s.get("percent", 0) >= 100)
                for s in states
            ):
                return
            if asyncio.get_running_loop().time() > deadline:
                return  # build with what has been read so far
            await asyncio.sleep(poll)


async def build_setup(
    business_id: uuid.UUID, setup_id: uuid.UUID, *, poll: float = POLL_SECONDS
) -> None:
    async with tenant_session(business_id) as session:
        setup = await session.get(Setup, setup_id)
        if setup is None:
            return
        setup.status = "building"
        sources, pages = list(setup.source_ids), setup.starter_pages
    try:
        if sources:
            await wait_for_reads(business_id, sources, poll=poll)
        benchmark_id, made = await make_starter_tests(
            business_id, sources, pages=min(15, max(pages // 10, 5))
        )
    except BrainError as exc:
        await _fail(business_id, setup_id, f"The Knowledge Brain didn't answer: {exc}")
        return
    if benchmark_id is None:
        await _fail(
            business_id,
            setup_id,
            "No test could be written: no page had enough text, or no model answered.",
        )
        return
    run_id = await runner.new_run(
        business_id,
        benchmark_kind="starter",
        name="Readiness: the starting test set",
        job="chat_answer",
        models=["settings"],
        mode="readiness",
    )
    async with tenant_session(business_id) as session:
        setup = await session.get(Setup, setup_id)
        assert setup is not None
        setup.starter_benchmark_id, setup.readiness_run_id = benchmark_id, run_id
    await runner.run_benchmark(business_id, run_id)
    async with tenant_session(business_id) as session:
        setup = await session.get(Setup, setup_id)
        run = await session.get(Run, run_id)
        assert setup is not None and run is not None
        stats = dict((run.summary or {}).get("settings") or {})
        setup.readiness_correct, setup.readiness_total = int(stats.get("passed", 0)), made
        setup.status, setup.current_step = "waiting", "readiness"
        service.mark(setup, "build", "done")
        service.mark(setup, "readiness", "started")


async def _fail(business_id: uuid.UUID, setup_id: uuid.UUID, reason: str) -> None:
    async with tenant_session(business_id) as session:
        setup = await session.get(Setup, setup_id)
        if setup is not None:
            setup.status = "failed"
            times = dict(setup.step_times or {})
            times["build"] = {
                **dict(times.get("build") or {}),
                "error": reason,
                "failed_at": datetime.now(UTC).isoformat(),
            }
            setup.step_times = times


@bp.task(name="build", queue="web")
async def build(business_id: str, setup_id: str) -> None:
    await build_setup(uuid.UUID(business_id), uuid.UUID(setup_id))
