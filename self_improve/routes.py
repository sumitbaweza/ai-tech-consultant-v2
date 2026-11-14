"""Screen 12, Self-Improve: send failures to Opus, follow the rounds, read the report, approve
or undo with one click, and the weekly emails."""

import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eval_lab.models import Run
from self_improve import github
from self_improve.loop import create_run
from self_improve.models import ImproveRound, ImproveRun, WeeklyReport
from self_improve.packer import build_packet
from web.deps import Ctx, bid, business_ctx, db, owner_ctx
from web.state import queue
from web.templating import render

router = APIRouter(prefix="/improve")
CtxDep = Annotated[Ctx, Depends(business_ctx)]
OwnerDep = Annotated[Ctx, Depends(owner_ctx)]
DbDep = Annotated[AsyncSession, Depends(db)]

TABS = [("Runs", "/improve", "runs"), ("Weekly emails", "/improve/weekly", "weekly")]
DEFAULT_ROUNDS, DEFAULT_COST = 5, Decimal("5.00")


def page(request: Request, template: str, tab: str, **context: Any) -> HTMLResponse:
    return render(request, template, active="improve", tabs=TABS, tab=tab, **context)


@router.get("", response_class=HTMLResponse)
async def runs(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    rows = (
        (
            await session.execute(
                select(ImproveRun).order_by(ImproveRun.created_at.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    sources = (
        (
            await session.execute(
                select(Run)
                .where(Run.status == "done", Run.job.in_(("brain_answer", "chat_answer")))
                .order_by(Run.finished_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    busy = any(r.status in ("queued", "running") for r in rows)
    return page(
        request,
        "improve/runs.html",
        "runs",
        runs=rows,
        sources=sources,
        busy=busy,
        github=github.configured(),
        notice=request.query_params.get("msg"),
    )


@router.post("/from-run/{run_id}")
async def from_run(run_id: uuid.UUID, ctx: OwnerDep) -> Response:
    return RedirectResponse(f"/improve/new?run_id={run_id}", status_code=303)


@router.get("/new", response_class=HTMLResponse)
async def new(request: Request, ctx: OwnerDep, run_id: uuid.UUID) -> HTMLResponse:
    """What will be sent to Opus, before anything is spent."""
    try:
        packet = await build_packet(bid(ctx), source_run_id=run_id, comment="")
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return page(
        request,
        "improve/new.html",
        "runs",
        packet=packet,
        run_id=run_id,
        rounds=DEFAULT_ROUNDS,
        cost=DEFAULT_COST,
    )


@router.post("/runs")
async def start(request: Request, ctx: OwnerDep) -> Response:
    form = await request.form()
    try:
        source = uuid.UUID(str(form.get("run_id")))
        rounds = max(1, min(10, int(str(form.get("max_rounds") or DEFAULT_ROUNDS))))
        cost = max(
            Decimal("0.50"), min(Decimal("50"), Decimal(str(form.get("max_cost") or DEFAULT_COST)))
        )
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(400, "Check the limits: rounds 1 to 10, $0.50 to $50.") from exc
    picked = [uuid.UUID(str(v)) for v in form.getlist("result_ids")]
    try:
        run_id = await create_run(
            bid(ctx),
            source_run_id=source,
            comment=str(form.get("comment") or ""),
            max_rounds=rounds,
            max_cost=cost,
            user_id=ctx.user_id,
            result_ids=picked or None,
        )
    except LookupError as exc:
        return RedirectResponse(f"/improve?msg={exc}", status_code=303)
    await queue().enqueue(
        "improve:run_loop", queue="improve", business_id=str(bid(ctx)), run_id=str(run_id)
    )
    return RedirectResponse(f"/improve/runs/{run_id}", status_code=303)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def detail(request: Request, run_id: uuid.UUID, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    run = await session.get(ImproveRun, run_id)
    if run is None:
        raise HTTPException(404)
    rounds = (
        (
            await session.execute(
                select(ImproveRound).where(ImproveRound.run_id == run_id).order_by(ImproveRound.n)
            )
        )
        .scalars()
        .all()
    )
    return page(
        request,
        "improve/run.html",
        "runs",
        run=run,
        rounds=rounds,
        busy=run.status in ("queued", "running"),
        notice=request.query_params.get("msg"),
    )


@router.post("/runs/{run_id}/{decision}")
async def decide(run_id: uuid.UUID, decision: str, ctx: OwnerDep, session: DbDep) -> Response:
    """One click: approve (merge), reject, or undo. Git work happens in the improve worker."""
    run = await session.get(ImproveRun, run_id)
    allowed = {"approve": ("fixed",), "reject": ("fixed", "not_fixed"), "undo": ("approved",)}
    if run is None or decision not in allowed:
        raise HTTPException(404)
    if run.status not in allowed[decision]:
        return RedirectResponse(
            f"/improve/runs/{run_id}?msg=Not possible while the run is {run.status}.",
            status_code=303,
        )
    args: dict[str, str] = {
        "business_id": str(bid(ctx)),
        "run_id": str(run_id),
        "user_id": str(ctx.user_id),
    }
    if decision != "reject":
        args["who"] = ctx.user_name
    await queue().enqueue(
        f"improve:{decision}", queue="improve", lock=f"improve-review:{run_id}", **args
    )
    words = {"approve": "Merging", "reject": "Rejecting", "undo": "Undoing"}[decision]
    return RedirectResponse(
        f"/improve/runs/{run_id}?msg={words}: refresh in a few seconds.", status_code=303
    )


@router.get("/weekly", response_class=HTMLResponse)
async def weekly_list(
    request: Request, ctx: CtxDep, session: DbDep, report_id: uuid.UUID | None = None
) -> HTMLResponse:
    rows = (
        (
            await session.execute(
                select(WeeklyReport).order_by(WeeklyReport.week_start.desc()).limit(52)
            )
        )
        .scalars()
        .all()
    )
    chosen = next((r for r in rows if r.id == report_id), rows[0] if rows else None)
    return page(
        request,
        "improve/weekly.html",
        "weekly",
        reports=rows,
        report=chosen,
        notice=request.query_params.get("msg"),
    )


@router.post("/weekly/now")
async def weekly_now(ctx: OwnerDep) -> Response:
    await queue().enqueue("improve:weekly_now", queue="web", business_id=str(bid(ctx)))
    return RedirectResponse(
        "/improve/weekly?msg=Making this week's email: refresh in a few seconds.", status_code=303
    )
