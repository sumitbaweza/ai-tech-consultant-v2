"""Scoring a change: the app, never Opus, answers the cases with the code in the work folder.

It runs as a separate process inside the work folder, so Python imports the edited code:

    python -m self_improve.evaluate --spec spec.json --out scores.json

The Knowledge Brain runs in the same process from the same folder (Opus may have changed it
too), and the prompts Opus edited in self_improve_input/prompts/ are passed as overrides. The
judge and the rules are protected paths, so they are always the reviewed ones.
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from ask_teach_chat import pipeline
from core.brain_client import BrainError, use_transport
from core.db import dispose_engines, tenant_session
from core.llm.types import LlmError
from core.models import ModelCall
from eval_lab import runner
from eval_lab.models import Benchmark, Case

JOB_FOR_KIND = {
    "brain": "brain_answer",
    "hidden": "brain_answer",
    "chat": "chat_answer",
    "feedback": "chat_answer",
    "starter": "chat_answer",
}


def edited_prompts(folder: Path) -> dict[str, str]:
    prompts = folder / "self_improve_input" / "prompts"
    return (
        {p.stem: p.read_text(encoding="utf-8") for p in sorted(prompts.glob("*.md"))}
        if prompts.exists()
        else {}
    )


async def score_group(
    business_id: uuid.UUID,
    case_ids: list[uuid.UUID],
    *,
    model_key: str | None,
    prompts: dict[str, str],
) -> dict[str, Any]:
    async with tenant_session(business_id) as session:
        rows = (
            await session.execute(
                select(Case, Benchmark.kind)
                .join(Benchmark, Benchmark.id == Case.benchmark_id)
                .where(Case.id.in_(case_ids))
                .order_by(Case.case_key)
            )
        ).all()
    brain_prompts = {k: v for k, v in prompts.items() if k.startswith("brain.")}
    chat_prompts = {k: v for k, v in prompts.items() if not k.startswith("brain.")}
    passed: dict[str, bool | None] = {}
    by_category: dict[str, list[bool]] = {}
    failures: list[dict[str, Any]] = []
    for case, kind in rows:
        job = JOB_FOR_KIND.get(kind, "chat_answer")
        try:
            done = await runner.answer_case(
                business_id,
                case,
                job=job,
                model_key=model_key,
                overrides=pipeline.Overrides(prompts=chat_prompts),
                ask_overrides=brain_prompts or None,
            )
            scored = await runner.score_case(business_id, case, done, second_opinion=False)
        except (LlmError, BrainError) as exc:
            passed[str(case.id)] = None
            failures.append(
                {
                    "case_key": case.case_key,
                    "question": case.question,
                    "answer": "",
                    "why": f"error: {exc}"[:300],
                }
            )
            continue
        result: bool | None = scored["passed"]
        passed[str(case.id)] = result
        if result is not None:
            by_category.setdefault(case.category, []).append(result)
        if result is False:
            judge = scored["first"]
            failures.append(
                {
                    "case_key": case.case_key,
                    "category": case.category,
                    "question": case.question,
                    "answer": done.seen.answer[:800],
                    "why": judge.reason
                    if judge
                    else ", ".join(k for k, ok in scored["verdict"].checks.items() if not ok),
                }
            )
    rated = [v for v in passed.values() if v is not None]
    return {
        "cases": len(rows),
        "pass_rate": round(sum(rated) / len(rated), 4) if rated else None,
        "by_category": {k: round(sum(v) / len(v), 4) for k, v in by_category.items()},
        "passed": passed,
        "failures": failures,
    }


async def evaluate(spec: dict[str, Any], folder: Path) -> dict[str, Any]:
    business_id = uuid.UUID(spec["business_id"])
    started = datetime.now(UTC)
    prompts = edited_prompts(folder)
    scores: dict[str, Any] = {}
    for group, ids in spec["groups"].items():
        scores[group] = await score_group(
            business_id,
            [uuid.UUID(i) for i in ids],
            model_key=spec.get("model_key"),
            prompts=prompts,
        )
    async with tenant_session(business_id) as session:
        cost = await session.scalar(
            select(func.coalesce(func.sum(ModelCall.cost_usd), 0)).where(
                ModelCall.created_at >= started
            )
        )
    scores["cost_usd"] = float(Decimal(str(cost or 0)))
    return scores


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m self_improve.evaluate")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    import httpx

    from knowledge_brain.api.main import app as brain_app  # the Brain's code from this folder
    from knowledge_brain.worker import app as brain_jobs

    use_transport(httpx.ASGITransport(app=brain_app))
    try:
        async with brain_jobs.open_async():  # the Brain may queue a job while answering
            scores = await evaluate(spec, Path(os.getcwd()))
        Path(args.out).write_text(json.dumps(scores), encoding="utf-8")
        return 0
    finally:
        await dispose_engines()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
