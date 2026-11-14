"""The Eval Lab's tables (schema `evals`): benchmarks loaded from the versioned files in
benchmark/, runs, every answer with its judge score, hand grades, notes, and release gates."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base, Tenant, new_id, server_now


def one_of(column: str, values: tuple[str, ...]) -> CheckConstraint:
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=column)


class Benchmark(Tenant, Base):
    """A set of test cases at one version: loaded from benchmark/<version>/<set>.yaml, made
    from the business's own pages (Go Live), or grown from real feedback."""

    __tablename__ = "benchmarks"
    __table_args__ = (
        UniqueConstraint("business_id", "name", "version"),
        one_of("kind", ("brain", "url", "chat", "teach", "starter", "feedback", "hidden")),
        {"schema": "evals"},
    )

    id: Mapped[uuid.UUID] = new_id()
    name: Mapped[str]
    version: Mapped[str]
    kind: Mapped[str]
    file_path: Mapped[str | None]
    # Hidden sets are run only by the app, never shown to the Self-Improve agent (Task 5.2)
    hidden: Mapped[bool] = mapped_column(server_default=text("false"))
    case_count: Mapped[int] = mapped_column(server_default=text("0"))
    created_at: Mapped[datetime] = server_now()


class Case(Tenant, Base):
    """One test: a question, the expected answer and its source, and rule checks."""

    __tablename__ = "cases"
    __table_args__ = (UniqueConstraint("benchmark_id", "case_key"), {"schema": "evals"})

    id: Mapped[uuid.UUID] = new_id()
    benchmark_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evals.benchmarks.id", ondelete="CASCADE"), index=True
    )
    case_key: Mapped[str]  # e.g. KB-017, stable across versions
    # regular | screenshot | no_answer | instructions | synonym_typo | trick | handoff |
    # not_in_kb | teach | url_301 | url_404 | url_nested | url_duplicate | url_recovered
    category: Mapped[str]
    question: Mapped[str]
    expected: Mapped[str]
    expected_sources: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    # Rule checks scored without a judge: must_hand_off, must_not_answer, must_not_contain,
    # url_level ... (eval_lab/rules.py)
    checks: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    # A teach test's correction, given to the chat before the second ask (Task 3.5)
    teach: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime] = server_now()


class Run(Tenant, Base):
    """One run of a benchmark on one or more models, for one job, in one mode."""

    __tablename__ = "runs"
    __table_args__ = (
        one_of("status", ("queued", "running", "done", "failed", "cancelled")),
        Index("ix_runs_business_created", "business_id", text("created_at DESC")),
        {"schema": "evals"},
    )

    id: Mapped[uuid.UUID] = new_id()
    benchmark_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evals.benchmarks.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str]
    # tag_job | find_facts | write_answer | read_screenshot | write_lesson | judge |
    # brain_answer (the Brain's /v1/ask end to end) | chat_answer (the chat end to end)
    job: Mapped[str]
    model_keys: Mapped[list[str]]
    judge_model_key: Mapped[str | None]
    # normal | local_only | router | always_sonnet | cache_off | cache_on | gate |
    # before_lesson | after_lesson | self_improve | readiness
    mode: Mapped[str] = mapped_column(server_default="normal")
    status: Mapped[str] = mapped_column(server_default="queued")
    params: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    # Totals per model, filled in when the run ends: pass rate, cost, average and p95 time
    summary: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    total_cost_usd: Mapped[Decimal] = mapped_column(server_default=text("0"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    error: Mapped[str | None]


class Result(Tenant, Base):
    """One model's answer to one case, with the judge's score and the rule checks."""

    __tablename__ = "results"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", "model_key"),
        {"schema": "evals"},
    )

    id: Mapped[uuid.UUID] = new_id()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evals.runs.id", ondelete="CASCADE"))
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evals.cases.id", ondelete="CASCADE"), index=True
    )
    model_key: Mapped[str]
    answer: Mapped[str | None]
    answered: Mapped[bool | None]
    needs_human: Mapped[bool | None]
    cited_sources: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    # The judge (Task 4.1): score 0-1, pass or fail, one line of reason
    judge_model_key: Mapped[str | None]
    judge_score: Mapped[float | None]
    judge_pass: Mapped[bool | None]
    judge_reason: Mapped[str | None]
    # The free second opinion (Nemotron 3 Ultra by default)
    second_model_key: Mapped[str | None]
    second_score: Mapped[float | None]
    second_reason: Mapped[str | None]
    rule_checks: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    passed: Mapped[bool | None]  # the final verdict: rule checks and the judge together
    latency_ms: Mapped[int | None]
    cost_usd: Mapped[Decimal] = mapped_column(server_default=text("0"))
    trace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.traces.id", ondelete="SET NULL")
    )
    error: Mapped[str | None]
    created_at: Mapped[datetime] = server_now()


class HumanGrade(Tenant, Base):
    """The candidate's own blind grade of a result, to check the judge (Task 4.1)."""

    __tablename__ = "human_grades"
    __table_args__ = (UniqueConstraint("result_id", "user_id"), {"schema": "evals"})

    id: Mapped[uuid.UUID] = new_id()
    result_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evals.results.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    passed: Mapped[bool]
    note: Mapped[str | None]
    created_at: Mapped[datetime] = server_now()


class CaseNote(Tenant, Base):
    """A reviewer's note on one case for one model: what was good, what was wrong (4.2)."""

    __tablename__ = "case_notes"
    __table_args__ = (UniqueConstraint("case_id", "model_key", "user_id"), {"schema": "evals"})

    id: Mapped[uuid.UUID] = new_id()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evals.cases.id", ondelete="CASCADE"))
    model_key: Mapped[str]
    note: Mapped[str]
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = server_now()


class CandidateCase(Tenant, Base):
    """A test case proposed from a thumbs down in the chat (Task 4.4), waiting for review."""

    __tablename__ = "candidate_cases"
    __table_args__ = (one_of("status", ("new", "accepted", "rejected")), {"schema": "evals"})

    id: Mapped[uuid.UUID] = new_id()
    feedback_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat.feedback.id", ondelete="SET NULL")
    )
    question: Mapped[str]
    bad_answer: Mapped[str | None]
    expected: Mapped[str]
    reason: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="new")
    accepted_case_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evals.cases.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


class Gate(Tenant, Base):
    """The release gate (Task 4.5): a new prompt, role, skill or model version against the
    live one, on the same gate cases. It goes live only if no score drops."""

    __tablename__ = "gates"
    __table_args__ = (
        one_of("target_kind", ("prompt", "role", "skill", "model_choice")),
        one_of("status", ("running", "passed", "blocked", "error")),
        {"schema": "evals"},
    )

    id: Mapped[uuid.UUID] = new_id()
    target_kind: Mapped[str]
    target_key: Mapped[str]  # prompt key, role key, skill key or step name
    candidate_ref: Mapped[str]  # the version id (or model key) being checked
    baseline_ref: Mapped[str | None]
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evals.runs.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(server_default="running")
    # Pass rate per category for the live version and the candidate
    baseline_scores: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    candidate_scores: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    reason: Mapped[str | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()
    finished_at: Mapped[datetime | None]
