"""The Self-Improve Loop's tables (schema `improve`): runs, their rounds, weekly emails."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base, Tenant, new_id, server_now

RUN_STATUSES = (
    "queued",
    "running",
    "fixed",  # the target was reached; waiting for a human
    "not_fixed",  # stopped at a limit; the report says what it tried
    "approved",  # merged
    "rejected",
    "undone",  # merged, then reverted
    "error",
)


class ImproveRun(Tenant, Base):
    """One attempt by Claude Opus 5.5 to fix failing cases, on its own git branch (Task 5)."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in RUN_STATUSES) + ")", name="status"
        ),
        Index("ix_improve_runs_business_created", "business_id", text("created_at DESC")),
        {"schema": "improve"},
    )

    id: Mapped[uuid.UUID] = new_id()
    title: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="queued")
    # What failed: the eval results and the person's (or the judge's) comment on why
    failing_result_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default=text("'{}'"))
    comment: Mapped[str]
    # What was sent to Opus: prompts, failing answers, related code paths (self_improve/packer)
    packet: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    branch: Mapped[str | None]
    base_sha: Mapped[str | None]
    head_sha: Mapped[str | None]
    pr_url: Mapped[str | None]
    pr_number: Mapped[int | None]
    # Hard limits (Task 5.2) and what was used
    max_rounds: Mapped[int]
    max_cost_usd: Mapped[Decimal]
    rounds_used: Mapped[int] = mapped_column(server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(server_default=text("0"))
    # Pass rates before and after: on the failing cases, on all the other cases, and on the
    # hidden check set that Opus never sees
    scores_before: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    scores_after: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    changed_files: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    # Edits to protected paths (the test set, the judge prompt) that were thrown away
    violations: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    report_md: Mapped[str | None]
    report_path: Mapped[str | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()
    finished_at: Mapped[datetime | None]
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None]
    merge_sha: Mapped[str | None]
    revert_sha: Mapped[str | None]
    error: Mapped[str | None]


class ImproveRound(Tenant, Base):
    """One round: Opus edits, then the app runs the tests and the evals itself."""

    __tablename__ = "rounds"
    __table_args__ = (UniqueConstraint("run_id", "n"), {"schema": "improve"})

    id: Mapped[uuid.UUID] = new_id()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("improve.runs.id", ondelete="CASCADE"))
    n: Mapped[int]
    agent_summary: Mapped[str | None]  # what Opus says it changed and why
    # Every tool call Opus made (name, a short input, allowed or denied), for the audit
    tool_calls: Mapped[list[dict[str, Any]]] = mapped_column(server_default=text("'[]'::jsonb"))
    changed_files: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    tests_passed: Mapped[bool | None]
    scores: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    cost_usd: Mapped[Decimal] = mapped_column(server_default=text("0"))
    started_at: Mapped[datetime] = server_now()
    finished_at: Mapped[datetime | None]


class WeeklyReport(Tenant, Base):
    """The weekly email to the owner (Task 5.4): what got better, what failed, the cost."""

    __tablename__ = "weekly_reports"
    __table_args__ = (UniqueConstraint("business_id", "week_start"), {"schema": "improve"})

    id: Mapped[uuid.UUID] = new_id()
    week_start: Mapped[date]
    subject: Mapped[str]
    body_md: Mapped[str]
    sent_to: Mapped[str | None]
    # sent (through the owner's Gmail) | stored (Gmail not connected) | error
    send_status: Mapped[str]
    error: Mapped[str | None]
    created_at: Mapped[datetime] = server_now()
