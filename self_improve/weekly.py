"""Task 5.4: one email a week to the owner: what got better, what failed, and the cost.

Made every Monday morning (a periodic job) or on demand from the Self-Improve screen. It goes
out through the owner's own Gmail (the gmail.send permission, D-7); without Gmail connected it
is kept in the app with the status "stored", so nothing is lost.
"""

import base64
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage
from typing import Any

import httpx
from sqlalchemy import func, select

from ask_teach_chat.models import Feedback, Message
from core.crypto import decrypt
from core.db import tenant_session
from core.google import access_token
from core.llm.providers.base import jdict
from core.models import Membership, ModelCall, User
from eval_lab.models import Benchmark, Run
from self_improve.models import ImproveRun, WeeklyReport
from web.markdown import render_markdown
from workspace.connections import get_connection


def week_of(moment: datetime) -> tuple[date, datetime, datetime]:
    """The Monday-to-Monday week that ended at or before `moment`'s Monday."""
    monday = (moment - timedelta(days=moment.weekday())).date()
    end = datetime(monday.year, monday.month, monday.day, tzinfo=UTC)
    start = end - timedelta(days=7)
    return start.date(), start, end


def _best_rate(run: Run) -> float | None:
    stats = [jdict(s) for s in (run.summary or {}).values()]
    rates = [float(s["pass_rate"]) for s in stats if s.get("pass_rate") is not None]
    return max(rates) if rates else None


async def build(
    business_id: uuid.UUID, now: datetime | None = None
) -> tuple[date, str, str, dict[str, Any]]:
    week_start, start, end = week_of(now or datetime.now(UTC))
    async with tenant_session(business_id) as session:
        runs = list(
            (
                await session.execute(
                    select(Run, Benchmark.name)
                    .join(Benchmark, Benchmark.id == Run.benchmark_id)
                    .where(Run.status == "done", Run.finished_at < end)
                    .order_by(Run.finished_at)
                )
            ).all()
        )
        improve = list(
            (
                await session.execute(
                    select(ImproveRun).where(
                        ImproveRun.created_at >= start, ImproveRun.created_at < end
                    )
                )
            ).scalars()
        )
        cost = Decimal(
            await session.scalar(
                select(func.coalesce(func.sum(ModelCall.cost_usd), 0)).where(
                    ModelCall.created_at >= start, ModelCall.created_at < end
                )
            )
            or 0
        )
        by_step = (
            await session.execute(
                select(ModelCall.step, func.sum(ModelCall.cost_usd))
                .where(ModelCall.created_at >= start, ModelCall.created_at < end)
                .group_by(ModelCall.step)
                .order_by(func.sum(ModelCall.cost_usd).desc())
                .limit(3)
            )
        ).all()
        kinds = dict(
            (
                await session.execute(
                    select(Message.kind, func.count())
                    .where(
                        Message.sender == "agent",
                        Message.created_at >= start,
                        Message.created_at < end,
                    )
                    .group_by(Message.kind)
                )
            ).all()
        )
        thumbs_down = int(
            await session.scalar(
                select(func.count())
                .select_from(Feedback)
                .where(
                    Feedback.verdict == "wrong",
                    Feedback.created_at >= start,
                    Feedback.created_at < end,
                )
            )
            or 0
        )
    better: list[str] = []
    worse: list[str] = []
    latest_scores: list[str] = []
    by_name: dict[str, list[Run]] = {}
    for run, name in runs:
        if (
            run.job in ("brain_answer", "chat_answer") and _best_rate(run) is not None
        ):  # scored runs only
            by_name.setdefault(name, []).append(run)
    for name, items in by_name.items():
        this_week = [r for r in items if r.finished_at and r.finished_at >= start]
        if not this_week:
            continue
        earlier = [r for r in items if r.finished_at and r.finished_at < start]
        now_rate = _best_rate(this_week[-1])
        before_rate = _best_rate(earlier[-1]) if earlier else None
        if now_rate is None:
            latest_scores.append(f"{name}: not scored")
        elif before_rate is None or now_rate == before_rate:
            latest_scores.append(f"{name}: {now_rate:.0%}")
        else:
            (better if now_rate > before_rate else worse).append(
                f"{name}: {now_rate:.0%} (was {before_rate:.0%})"
            )
    opus_cost = sum((Decimal(r.cost_usd) for r in improve), Decimal(0))
    facts = {
        "answers": kinds.get("answer", 0),
        "dont_know": kinds.get("dont_know", 0),
        "handoffs": kinds.get("handoff", 0),
        "thumbs_down": thumbs_down,
        "cost": float(cost),
        "opus_cost": float(opus_cost),
        "improve": {
            s: sum(1 for r in improve if r.status == s)
            for s in ("fixed", "approved", "not_fixed", "undone")
        },
    }
    lines = [
        f"# Your AI Tech Consultant, week of {week_start:%d %b %Y}",
        "",
        "## What got better",
        "",
    ]
    lines += [f"- {line}" for line in better] or ["- No benchmark score went up this week."]
    fixed = [r for r in improve if r.status in ("fixed", "approved")]
    lines += [f"- Self-Improve fixed: {r.title} ({r.status})" for r in fixed]
    lines += ["", "## What failed or needs you", ""]
    lines += [f"- Went down: {line}" for line in worse]
    lines += [
        f"- Self-Improve could not fix: {r.title}" for r in improve if r.status == "not_fixed"
    ]
    lines += [
        f"- {facts['dont_know']} questions the agent couldn't answer and {facts['handoffs']} handed to a person",
        f"- {thumbs_down} answers got a thumbs down",
    ]
    lines += [f"- Latest score: {line}" for line in latest_scores]
    lines += [
        "",
        "## The cost",
        "",
        f"- Model calls this week: ${cost:.2f} (Self-Improve's Opus runs: ${opus_cost:.2f})",
    ]
    lines += [
        f"- Costliest step: {step} (${Decimal(value or 0):.2f})" for step, value in by_step[:1]
    ]
    lines += ["", f"{facts['answers']} questions answered this week. Open the app for the details."]
    subject = f"AI Tech Consultant: your week of {week_start:%d %b} ({len(better)} better, {len(worse)} worse, ${cost:.2f})"
    return week_start, subject, "\n".join(lines) + "\n", facts


async def _owner_email(business_id: uuid.UUID) -> str | None:
    async with tenant_session(business_id) as session:
        return await session.scalar(
            select(User.email)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.business_id == business_id, Membership.role == "owner")
            .limit(1)
        )


async def send_gmail(business_id: uuid.UUID, to: str, subject: str, body_md: str) -> None:
    async with tenant_session(business_id) as session:
        connection = await get_connection(session, "google")
        if connection is None or connection.secret_enc is None:
            raise LookupError("Gmail is not connected")
        refresh = decrypt(connection.secret_enc, business_id=business_id)
    message = EmailMessage()
    message["To"], message["Subject"] = to, subject
    message.set_content(body_md)
    message.add_alternative(str(render_markdown(body_md)), subtype="html")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    token = await access_token(refresh)
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {token}"},
            json={"raw": raw},
        )
    response.raise_for_status()


async def make_weekly(
    business_id: uuid.UUID, now: datetime | None = None, *, send: bool = True
) -> uuid.UUID:
    """Build this week's email, send it if Gmail is connected, and keep it (one per week)."""
    week_start, subject, body, _ = await build(business_id, now)
    to = await _owner_email(business_id)
    status, error = "stored", None
    if send and to:
        try:
            await send_gmail(business_id, to, subject, body)
            status = "sent"
        except LookupError:
            status = "stored"
        except httpx.HTTPError as exc:
            status, error = "error", f"Gmail refused: {exc}"[:500]
    async with tenant_session(business_id) as session:
        report = (
            await session.execute(select(WeeklyReport).where(WeeklyReport.week_start == week_start))
        ).scalar_one_or_none()
        if report is None:
            report = WeeklyReport(
                business_id=business_id,
                week_start=week_start,
                subject=subject,
                body_md=body,
                send_status=status,
            )
            session.add(report)
        report.subject, report.body_md, report.send_status, report.error = (
            subject,
            body,
            status,
            error,
        )
        report.sent_to = to if status == "sent" else None
        await session.flush()
        return report.id
