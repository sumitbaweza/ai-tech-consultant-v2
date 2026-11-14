"""Logs and costs (Task 1.5, screen 13): the cost dashboard, traces, the ledger, failed jobs.

The aggregates are raw SQL (STACK.md S2): sums by day, step, model and role, and
percentile_cont(0.95) for the slowest 5% of calls. Row-level security keeps them to the
current business.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.jobs import failed_jobs
from core.models import ModelCall, Span, Trace
from web.deps import Ctx, bid, business_ctx, db
from web.templating import render

router = APIRouter(prefix="/logs")
CtxDep = Annotated[Ctx, Depends(business_ctx)]
DbDep = Annotated[AsyncSession, Depends(db)]

TABS = [
    ("Cost dashboard", "/logs", "costs"),
    ("Traces", "/logs/traces", "traces"),
    ("Model calls", "/logs/calls", "calls"),
    ("Failed jobs", "/logs/failed", "failed"),
]


def page(request: Request, template: str, tab: str, **context: Any) -> HTMLResponse:
    return render(request, template, active="logs", tabs=TABS, tab=tab, **context)


async def rows(session: AsyncSession, sql: str, **params: Any) -> list[dict[str, Any]]:
    result = await session.execute(text(sql), params)
    return [dict(row) for row in result.mappings()]


@router.get("", response_class=HTMLResponse)
async def cost_dashboard(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    totals = (
        await rows(
            session,
            """
        SELECT
          coalesce(sum(cost_usd) FILTER (WHERE created_at >= now() - interval '1 day'), 0) AS day,
          coalesce(sum(cost_usd) FILTER (WHERE created_at >= now() - interval '7 days'), 0) AS week,
          coalesce(sum(cost_usd) FILTER (WHERE created_at >= now() - interval '30 days'), 0) AS month,
          count(*) FILTER (WHERE created_at >= now() - interval '7 days') AS calls_week,
          coalesce(sum(cache_read_tokens) FILTER (WHERE created_at >= now() - interval '7 days'), 0) AS cached_week
        FROM core.model_calls""",
        )
    )[0]
    by_day = await rows(
        session,
        """
        SELECT date_trunc('day', created_at)::date AS day, sum(cost_usd) AS cost, count(*) AS calls
        FROM core.model_calls WHERE created_at >= now() - interval '14 days'
        GROUP BY 1 ORDER BY 1""",
    )
    by_step = await rows(
        session,
        """
        SELECT step, job, count(*) AS calls, sum(cost_usd) AS cost,
               avg(cost_usd) * 100 AS per_100, avg(latency_ms)::int AS avg_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::int AS p95_ms
        FROM core.model_calls WHERE status = 'ok'
        GROUP BY step, job ORDER BY per_100 DESC NULLS LAST LIMIT 30""",
    )
    by_model = await rows(
        session,
        """
        SELECT model_key, count(*) AS calls, sum(input_tokens) AS input_tokens,
               sum(output_tokens) AS output_tokens, sum(cache_read_tokens) AS cache_read,
               sum(cost_usd) AS cost, avg(latency_ms)::int AS avg_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::int AS p95_ms,
               count(*) FILTER (WHERE status <> 'ok') AS errors
        FROM core.model_calls GROUP BY model_key ORDER BY cost DESC""",
    )
    by_role = await rows(
        session,
        """
        SELECT coalesce(r.name, '(no role)') AS role, count(*) AS calls, sum(m.cost_usd) AS cost
        FROM core.model_calls m
        LEFT JOIN core.role_versions rv ON rv.id = m.role_version_id
        LEFT JOIN core.roles r ON r.id = rv.role_id
        GROUP BY 1 ORDER BY cost DESC""",
    )
    max_day = max((float(r["cost"]) for r in by_day), default=0.0) or 1.0
    return page(
        request,
        "workspace/logs_costs.html",
        "costs",
        totals=totals,
        by_day=by_day,
        max_day=max_day,
        by_step=by_step,
        by_model=by_model,
        by_role=by_role,
        costliest=by_step[0] if by_step else None,
    )


@router.get("/traces", response_class=HTMLResponse)
async def traces(
    request: Request, ctx: CtxDep, session: DbDep, kind: str | None = None
) -> HTMLResponse:
    items = await rows(
        session,
        """
        SELECT t.id, t.kind, t.title, t.status, t.started_at,
               extract(epoch FROM (t.finished_at - t.started_at)) * 1000 AS ms,
               coalesce(sum(m.cost_usd), 0) AS cost, count(m.id) AS calls
        FROM core.traces t LEFT JOIN core.model_calls m ON m.trace_id = t.id
        WHERE (CAST(:kind AS text) IS NULL OR t.kind = :kind)
        GROUP BY t.id ORDER BY t.started_at DESC LIMIT 200""",
        kind=kind,
    )
    return page(request, "workspace/logs_traces.html", "traces", items=items, kind=kind)


@router.get("/traces/{trace_id}", response_class=HTMLResponse)
async def trace_detail(
    request: Request, trace_id: uuid.UUID, ctx: CtxDep, session: DbDep
) -> HTMLResponse:
    trace = await session.get(Trace, trace_id)
    if trace is None:
        raise HTTPException(404)
    spans = (
        (
            await session.execute(
                select(Span).where(Span.trace_id == trace_id).order_by(Span.started_at)
            )
        )
        .scalars()
        .all()
    )
    calls = (
        (
            await session.execute(
                select(ModelCall)
                .where(ModelCall.trace_id == trace_id)
                .order_by(ModelCall.created_at)
            )
        )
        .scalars()
        .all()
    )
    return page(
        request,
        "workspace/logs_trace.html",
        "traces",
        trace=trace,
        spans=spans,
        calls=calls,
        total=sum(float(c.cost_usd) for c in calls),
    )


@router.get("/calls", response_class=HTMLResponse)
async def calls(request: Request, ctx: CtxDep, session: DbDep, page_no: int = 1) -> HTMLResponse:
    size = 100
    items = (
        (
            await session.execute(
                select(ModelCall)
                .order_by(ModelCall.created_at.desc())
                .offset((page_no - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    return page(
        request, "workspace/logs_calls.html", "calls", items=items, page_no=page_no, size=size
    )


@router.get("/failed", response_class=HTMLResponse)
async def failed(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    return page(
        request,
        "workspace/logs_failed.html",
        "failed",
        jobs=await failed_jobs(session, bid(ctx)),
        notice=request.query_params.get("msg"),
    )


@router.post("/failed/{job_id}/retry")
async def retry(job_id: int, ctx: CtxDep, session: DbDep) -> Response:
    from web.worker import app

    jobs = {job["id"] for job in await failed_jobs(session, bid(ctx))}
    if job_id not in jobs:  # only this business's failed jobs
        raise HTTPException(404)
    await app.job_manager.retry_job_by_id_async(job_id, retry_at=None)  # type: ignore[arg-type]
    return RedirectResponse("/logs/failed?msg=Job queued again.", status_code=303)
