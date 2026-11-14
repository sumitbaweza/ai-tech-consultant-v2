"""Running the benchmark (Tasks 4.1 to 4.3, 3.5, 1.3): every case on every model, scored by rules
and the judge, saved case by case so a stopped run carries on where it was.

The system under test is reached the way users reach it: the Knowledge Brain through its API
(POST /v1/ask with a forced model) and the chat through its pipeline. Benchmark text is public
help-center content or made up, so free models and Gemini may see it (real_customer_data=False).
"""

import statistics
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from ask_teach_chat import pipeline
from ask_teach_chat.preview import default_role
from core.brain_api import AskRequest
from core.brain_client import BrainClient, BrainError
from core.db import tenant_session
from core.llm.providers.base import jdict
from core.llm.types import LlmError
from core.models import ModelCall
from eval_lab import judge as judging
from eval_lab.models import Benchmark, Case, Result, Run
from eval_lab.rules import Observed, check

JOBS = ("brain_answer", "chat_answer")


@dataclass
class Answered:
    seen: Observed
    model_key: str | None
    cost: Decimal
    latency_ms: int
    trace_id: uuid.UUID | None
    top_score: float | None


async def answer_case(
    business_id: uuid.UUID,
    case: Case,
    *,
    job: str,
    model_key: str | None,
    overrides: pipeline.Overrides | None = None,
    role: pipeline.RoleSettings | None = None,
    ask_overrides: dict[str, str] | None = None,
) -> Answered:
    if job == "brain_answer":
        async with await BrainClient.for_business(business_id) as brain:
            response = await brain.ask(
                AskRequest(
                    question=case.question,
                    model_key=model_key,
                    prompt_overrides=ask_overrides or None,
                )
            )
        seen = Observed(
            answer=response.answer,
            answered=response.answered,
            needs_human=False,
            cited_urls=[c.url for c in response.citations if c.url]
            + [f"{c.knowledge_type}:{c.title}" for c in response.citations],
            knowledge_types=list(response.knowledge_types),
            kind="answer" if response.answered else "dont_know",
        )
        return Answered(
            seen,
            response.model_key,
            Decimal(str(response.cost_usd)),
            response.latency_ms,
            response.trace_id,
            response.top_score,
        )
    chosen = overrides or pipeline.Overrides()
    if model_key:
        chosen.model_key = model_key
    outcome = await pipeline.answer(
        business_id,
        text=case.question,
        role=role or await default_role(business_id),
        overrides=chosen,
        trace_kind="eval_case",
        real_customer_data=False,
    )
    seen = Observed(
        answer=outcome.text,
        answered=outcome.answered,
        needs_human=outcome.needs_human,
        cited_urls=[str(c.get("url")) for c in outcome.citations if c.get("url")]
        + [f"{c.get('knowledge_type')}:{c.get('title')}" for c in outcome.citations],
        knowledge_types=outcome.knowledge_types,
        kind=outcome.kind,
    )
    return Answered(
        seen,
        outcome.model_key,
        outcome.cost_usd,
        outcome.latency_ms,
        outcome.trace_id,
        outcome.top_score,
    )


async def score_case(
    business_id: uuid.UUID, case: Case, done: Answered, *, second_opinion: bool
) -> dict[str, Any]:
    verdict = check(case.checks, done.seen)
    first = second = None
    if verdict.needs_judge:
        first = await judging.judge(
            business_id,
            question=case.question,
            expected=case.expected,
            answer=done.seen.answer,
            answered_by=done.model_key,
            trace_id=done.trace_id,
        )
        if second_opinion:
            second = await judging.judge(
                business_id,
                question=case.question,
                expected=case.expected,
                answer=done.seen.answer,
                answered_by=done.model_key,
                trace_id=done.trace_id,
                second=True,
            )
    judged_ok = first.passed if first else None
    passed = verdict.passed and (judged_ok is not False) if verdict.needs_judge else verdict.passed
    if verdict.needs_judge and first is None:
        passed = None if verdict.passed else False  # rules passed but nobody could judge the facts
    return {"verdict": verdict, "first": first, "second": second, "passed": passed}


async def run_benchmark(business_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """The job body: every case x every model, resuming after the last saved result."""
    async with tenant_session(business_id) as session:
        run = await session.get(Run, run_id)
        if run is None or run.status in ("done", "cancelled"):
            return
        run.status, run.started_at = "running", run.started_at or datetime.now(UTC)
        cases = list(
            (
                await session.execute(
                    select(Case)
                    .where(Case.benchmark_id == run.benchmark_id)
                    .order_by(Case.case_key)
                )
            ).scalars()
        )
        categories = run.params.get("categories")
        if categories:
            cases = [c for c in cases if c.category in categories]
        if run.params.get("limit"):
            cases = cases[: int(run.params["limit"])]
        done = {
            (r.case_id, r.model_key)
            for r in (
                await session.execute(select(Result).where(Result.run_id == run_id))
            ).scalars()
        }
        models, job, params = list(run.model_keys), run.job, dict(run.params)
    overrides_base: dict[str, Any] = jdict(params.get("overrides") or {})
    for model_key in models:
        for case in cases:
            if (case.id, model_key) in done:
                continue
            overrides = pipeline.Overrides(
                caching=overrides_base.get("caching", True),
                local_only=overrides_base.get("local_only", False),
                include_lessons=overrides_base.get("include_lessons", True),
            )
            forced = None if model_key in ("router", "settings") else model_key
            try:
                answered = await answer_case(
                    business_id,
                    case,
                    job=job,
                    model_key=forced,
                    overrides=overrides,
                    ask_overrides=overrides_base.get("prompts"),
                )
                scored = await score_case(
                    business_id, case, answered, second_opinion=bool(params.get("second_opinion"))
                )
                error = None
            except (LlmError, BrainError) as exc:
                answered, scored, error = None, None, str(exc)[:500]
            await _save(business_id, run_id, case, model_key, answered, scored, error)
    await finish(business_id, run_id)


async def _save(
    business_id: uuid.UUID,
    run_id: uuid.UUID,
    case: Case,
    model_key: str,
    answered: Answered | None,
    scored: dict[str, Any] | None,
    error: str | None,
) -> None:
    async with tenant_session(business_id) as session:
        row = Result(
            business_id=business_id,
            run_id=run_id,
            case_id=case.id,
            model_key=model_key,
            error=error,
        )
        if answered is not None and scored is not None:
            first, second = scored["first"], scored["second"]
            row.answer, row.answered, row.needs_human = (
                answered.seen.answer,
                answered.seen.answered,
                answered.seen.needs_human,
            )
            row.cited_sources = answered.seen.cited_urls[:20]
            row.judge_model_key = first.model_key if first else None
            row.judge_score = first.score if first else None
            row.judge_pass = first.passed if first else None
            row.judge_reason = first.reason if first else None
            row.second_model_key = second.model_key if second else None
            row.second_score = second.score if second else None
            row.second_reason = second.reason if second else None
            row.rule_checks = {
                **scored["verdict"].checks,
                "_top_score": answered.top_score,
                "_served_by": answered.model_key,
            }
            row.passed = scored["passed"]
            row.latency_ms, row.cost_usd, row.trace_id = (
                answered.latency_ms,
                answered.cost,
                answered.trace_id,
            )
        session.add(row)


def summarize(results: list[Result], categories: dict[uuid.UUID, str]) -> dict[str, Any]:
    by_model: dict[str, list[Result]] = {}
    for result in results:
        by_model.setdefault(result.model_key, []).append(result)
    summary: dict[str, Any] = {}
    for model, rows in by_model.items():
        scored = [r for r in rows if r.passed is not None]
        times = sorted(r.latency_ms for r in rows if r.latency_ms)
        cost = sum((r.cost_usd for r in rows), Decimal(0))
        per_category: dict[str, list[bool]] = {}
        for r in scored:
            per_category.setdefault(categories.get(r.case_id, "?"), []).append(bool(r.passed))
        summary[model] = {
            "cases": len(rows),
            "scored": len(scored),
            "errors": sum(1 for r in rows if r.error),
            "passed": sum(1 for r in scored if r.passed),
            "pass_rate": round(sum(1 for r in scored if r.passed) / len(scored), 4)
            if scored
            else None,
            "cost": float(cost),
            "cost_per_100": float(cost / len(rows) * 100) if rows else None,
            "avg_ms": int(statistics.mean(times)) if times else None,
            "p95_ms": times[min(len(times) - 1, round(0.95 * (len(times) - 1)))] if times else None,
            "by_category": {k: round(sum(v) / len(v), 4) for k, v in per_category.items()},
        }
    return summary


async def finish(business_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    async with tenant_session(business_id) as session:
        run = await session.get(Run, run_id)
        assert run is not None
        results = list(
            (await session.execute(select(Result).where(Result.run_id == run_id))).scalars()
        )
        categories = {
            c.id: c.category
            for c in (
                await session.execute(select(Case).where(Case.benchmark_id == run.benchmark_id))
            ).scalars()
        }
        run.summary = summarize(results, categories)
        run.total_cost_usd = sum((r.cost_usd for r in results), Decimal(0))
        run.status, run.finished_at = "done", datetime.now(UTC)
        return run.summary


async def new_run(
    business_id: uuid.UUID,
    *,
    benchmark_kind: str,
    name: str,
    job: str,
    models: list[str],
    mode: str = "normal",
    params: dict[str, Any] | None = None,
    user_id: uuid.UUID | None = None,
    judge_model: str | None = None,
) -> uuid.UUID:
    async with tenant_session(business_id) as session:
        benchmark = (
            await session.execute(
                select(Benchmark)
                .where(Benchmark.kind == benchmark_kind)
                .order_by(Benchmark.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if benchmark is None:
            raise LookupError(f"No {benchmark_kind} benchmark is loaded for this business")
        run = Run(
            business_id=business_id,
            benchmark_id=benchmark.id,
            name=name,
            job=job,
            model_keys=models,
            mode=mode,
            params=params or {},
            created_by=user_id,
            judge_model_key=judge_model,
        )
        session.add(run)
        await session.flush()
        return run.id


async def judge_agreement(business_id: uuid.UUID) -> dict[str, Any]:
    """Task 4.1: how often the judge agrees with the candidate's own blind grades."""
    from eval_lab.models import HumanGrade

    async with tenant_session(business_id) as session:
        rows = (
            await session.execute(
                select(HumanGrade, Result).join(Result, Result.id == HumanGrade.result_id)
            )
        ).all()
    pairs = [
        (grade.passed, result.judge_pass) for grade, result in rows if result.judge_pass is not None
    ]
    agreed = sum(1 for human, machine in pairs if human == machine)
    return {
        "graded": len(rows),
        "compared": len(pairs),
        "agreed": agreed,
        "rate": round(agreed / len(pairs), 4) if pairs else None,
    }


async def trace_cost(business_id: uuid.UUID, trace_id: uuid.UUID) -> Decimal:
    async with tenant_session(business_id) as session:
        values = await session.execute(
            select(ModelCall.cost_usd).where(ModelCall.trace_id == trace_id)
        )
        return sum((Decimal(v) for v in values.scalars()), Decimal(0))
