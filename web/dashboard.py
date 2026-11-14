"""The dashboard (screen 5): one look at readiness, knowledge, the chat, cost and failures."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ask_teach_chat.models import Answer, Lesson
from core.brain_client import BrainClient, BrainError
from core.jobs import job_counts
from core.models import Business
from go_live.models import Setup
from self_improve.models import ImproveRun
from web.deps import Ctx, bid, business_ctx, db
from web.templating import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    ctx: Annotated[Ctx, Depends(business_ctx)],
    session: Annotated[AsyncSession, Depends(db)],
) -> HTMLResponse:
    business = await session.get(Business, bid(ctx))
    week = text("now() - interval '7 days'")
    answers = await session.scalar(
        select(func.count()).select_from(Answer).where(Answer.created_at >= week)
    )
    handoffs = await session.scalar(
        select(func.count())
        .select_from(Answer)
        .where(Answer.created_at >= week, Answer.needs_human.is_(True))
    )
    lessons_waiting = await session.scalar(
        select(func.count()).select_from(Lesson).where(Lesson.status == "pending")
    )
    cost_week = await session.scalar(
        text(
            "SELECT coalesce(sum(cost_usd), 0) FROM core.model_calls WHERE created_at >= now() - interval '7 days'"
        )
    )
    setup = (
        await session.execute(select(Setup).order_by(Setup.started_at.desc()).limit(1))
    ).scalar_one_or_none()
    latest_run = (
        await session.execute(select(ImproveRun).order_by(ImproveRun.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    jobs = await job_counts(session, bid(ctx))
    stats: dict[str, Any] = {}
    brain_error = None
    try:
        async with await BrainClient.for_business(bid(ctx)) as brain:
            stats = await brain.get("/v1/stats")
    except BrainError as exc:
        brain_error = exc.detail
    return render(
        request,
        "dashboard.html",
        active="dashboard",
        title="Dashboard",
        business=business,
        answers=answers or 0,
        handoffs=handoffs or 0,
        lessons_waiting=lessons_waiting or 0,
        cost_week=cost_week or 0,
        setup=setup,
        latest_run=latest_run,
        jobs=jobs,
        stats=stats,
        brain_error=brain_error,
    )
