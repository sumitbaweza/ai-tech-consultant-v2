"""The shared kernel's tables (schema `core`): accounts, businesses, keys, connections,
prompts, roles and the log of every model call.

Every feature folder adds its own tables in its own schema (brain, chat, evals, improve,
golive). Every table that holds a business's data has a `business_id` column, and row-level
security (RLS) in the database shows a connection only the rows of the business it set for
its transaction (core/db.py, DATABASE.md D1).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.types import DateTime

# Our schemas, one per main feature plus the shared core. `public` holds the extensions
# (vector, pg_trgm) and Procrastinate's job tables.
SCHEMAS = ("core", "brain", "chat", "evals", "improve", "golive")

# Fixed names for constraints and indexes, so migrations can refer to them
NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    # Python types -> Postgres types, so most columns need only `Mapped[...]`
    type_annotation_map = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[dict[str, Any]]: JSONB,
        list[str]: ARRAY(Text),
        list[uuid.UUID]: ARRAY(UUID(as_uuid=True)),
        str: Text,
        Decimal: Numeric(12, 6),
    }


def new_id() -> Mapped[uuid.UUID]:
    """A UUID primary key that Postgres fills in."""
    return mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))


def server_now() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


class Tenant:
    """Mixin: the business that owns the row. Deleting the business deletes the row."""

    @declared_attr
    def business_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(ForeignKey("core.businesses.id", ondelete="CASCADE"), index=True)


# ---------------------------------------------------------------- accounts and businesses


class User(Base):
    """A person who signs in. Not tied to one business: a user can belong to several."""

    __tablename__ = "users"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = new_id()
    email: Mapped[str] = mapped_column(unique=True)  # stored lowercased
    name: Mapped[str]
    password_hash: Mapped[str]  # scrypt, with its salt and cost (core/security.py)
    created_at: Mapped[datetime] = server_now()
    last_login_at: Mapped[datetime | None]


class Business(Base):
    """One client company. Every other business-owned row points here."""

    __tablename__ = "businesses"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = new_id()
    name: Mapped[str]
    slug: Mapped[str] = mapped_column(unique=True)
    # Tone of voice picked in Go Live; every role's prompt receives it as {tone}
    tone: Mapped[str] = mapped_column(server_default="friendly, clear and short")
    # The local-only switch (Task 4.2): only the 2 Ollama models, no paid API at all
    local_only: Mapped[bool] = mapped_column(server_default=text("false"))
    # The chat opens for the team only when the readiness score reaches the threshold (6.3)
    chat_enabled: Mapped[bool] = mapped_column(server_default=text("false"))
    readiness_threshold: Mapped[int] = mapped_column(server_default=text("90"))
    # The "I don't know" line (3.2): below this search score the chat says it doesn't know.
    # The Eval Lab's threshold sweep sets it.
    dont_know_threshold: Mapped[float] = mapped_column(server_default=text("0.3"))
    # The Brain API key the web app uses for this business, encrypted (core/crypto.py)
    internal_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    # Advanced settings (Settings > Advanced): limits, retries, crawl speed
    advanced: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


class Membership(Base):
    """Who belongs to which business, and as what (owner or member)."""

    __tablename__ = "memberships"
    __table_args__ = {"schema": "core"}

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.businesses.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(server_default="member")  # owner | member
    created_at: Mapped[datetime] = server_now()


class WebSession(Base):
    """A signed-in browser. The cookie holds a random token; only its SHA-256 is stored."""

    __tablename__ = "web_sessions"
    __table_args__ = {"schema": "core"}

    token_hash: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.users.id", ondelete="CASCADE"), index=True
    )
    # The business the user is working in now (the switcher in the top bar)
    business_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.businesses.id", ondelete="SET NULL")
    )
    csrf_token: Mapped[str]
    created_at: Mapped[datetime] = server_now()
    expires_at: Mapped[datetime]


class ApiKey(Tenant, Base):
    """A key for the Knowledge Brain's API. Only the SHA-256 is stored; the key is shown once."""

    __tablename__ = "api_keys"
    __table_args__ = {"schema": "core"}

    id: Mapped[uuid.UUID] = new_id()
    name: Mapped[str]
    prefix: Mapped[str]  # the first characters, to recognise a key in the list
    key_hash: Mapped[str] = mapped_column(unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class Connection(Tenant, Base):
    """One of the 5 connections (Task 1.2): claude, gemini, openrouter, ollama, google."""

    __tablename__ = "connections"
    __table_args__ = (UniqueConstraint("business_id", "kind"), {"schema": "core"})

    id: Mapped[uuid.UUID] = new_id()
    kind: Mapped[str]
    # The API key, or Google's refresh token, encrypted with AES-GCM. Never shown again.
    secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    masked: Mapped[str | None]  # e.g. "sk-ant-...a1b2", safe to show and log
    # Non-secret settings: the Ollama address, the Gmail address and granted scopes
    config: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(server_default="untested")  # untested | ok | error
    last_tested_at: Mapped[datetime | None]
    last_test_message: Mapped[str | None]
    updated_at: Mapped[datetime] = server_now()


class ModelChoice(Tenant, Base):
    """The model the business picked for one step (Task 1.2), from the 14 in the registry."""

    __tablename__ = "model_choices"
    __table_args__ = (UniqueConstraint("business_id", "step"), {"schema": "core"})

    id: Mapped[uuid.UUID] = new_id()
    step: Mapped[str]  # core/llm/registry.py STEPS
    model_key: Mapped[str]
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = server_now()


class RoutingRule(Tenant, Base):
    """The routing table (Task 4.3): for a job and a complexity, the model to use first."""

    __tablename__ = "routing_rules"
    __table_args__ = (
        UniqueConstraint("business_id", "job", "complexity"),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = new_id()
    job: Mapped[str]
    complexity: Mapped[str]  # low | medium | high
    model_key: Mapped[str]
    # Where the row came from: default, eval_lab (cheapest model that passed), or human
    source: Mapped[str] = mapped_column(server_default="default")
    updated_at: Mapped[datetime] = server_now()


# ---------------------------------------------------------------- prompts and roles


class Prompt(Tenant, Base):
    """One prompt the app sends to a model (Task 1.3). Its text lives in its versions."""

    __tablename__ = "prompts"
    __table_args__ = (UniqueConstraint("business_id", "key"), {"schema": "core"})

    id: Mapped[uuid.UUID] = new_id()
    key: Mapped[str]  # e.g. "chat.answer_system"; the default text is prompts/<key>/v1.md
    title: Mapped[str]
    description: Mapped[str]
    kind: Mapped[str]  # system | user_template
    # Variables the text must keep, e.g. {knowledge_results}; saving without one warns
    required_vars: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    advanced: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = server_now()


class PromptVersion(Tenant, Base):
    """Every save of a prompt is a new row. Exactly one version per prompt is live."""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("prompt_id", "version"),
        Index(
            "uq_prompt_versions_one_live",
            "prompt_id",
            unique=True,
            postgresql_where=text("status = 'live'"),
        ),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = new_id()
    prompt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.prompts.id", ondelete="CASCADE"))
    version: Mapped[int]
    body: Mapped[str]
    variables: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    # default (shipped with the app) | human (saved in Settings) | self_improve (Opus, approved)
    author: Mapped[str]
    # draft | gating (the release gate is running) | live | blocked | retired
    status: Mapped[str]
    note: Mapped[str | None]
    gate_id: Mapped[uuid.UUID | None]  # evals.gates row that decided it
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


class Role(Tenant, Base):
    """An agent role (Task 1.4): Support Engineer, Support Executive, AI Consultant, or new."""

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("business_id", "key"), {"schema": "core"})

    id: Mapped[uuid.UUID] = new_id()
    key: Mapped[str]
    name: Mapped[str]
    builtin: Mapped[bool] = mapped_column(server_default=text("false"))
    archived_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = server_now()


class RoleVersion(Tenant, Base):
    """Every save of a role is a new row; exactly one is live. Answers point at the version."""

    __tablename__ = "role_versions"
    __table_args__ = (
        UniqueConstraint("role_id", "version"),
        Index(
            "uq_role_versions_one_live",
            "role_id",
            unique=True,
            postgresql_where=text("status = 'live'"),
        ),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = new_id()
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.roles.id", ondelete="CASCADE"))
    version: Mapped[int]
    goal: Mapped[str]
    prompt: Mapped[str]
    tone: Mapped[str]
    # What it may do: answer_from_brain, draft_reply, create_expert_task, share_price
    capabilities: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    # Which knowledge types it may use: sitemap, page_url, instruction, skill, past_email, ...
    knowledge_types: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    # Skill keys it may use; empty means every skill
    skill_keys: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    model_key: Mapped[str | None]  # None: the router picks
    # When to hand over to a human, on top of the fixed angry / refund / legal rule
    handover: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    author: Mapped[str]
    status: Mapped[str]
    note: Mapped[str | None]
    gate_id: Mapped[uuid.UUID | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


# ---------------------------------------------------------------- logs, traces and cost


class Trace(Tenant, Base):
    """One run of something: a chat answer, an eval case, a job. Its steps are spans."""

    __tablename__ = "traces"
    __table_args__ = (
        Index("ix_traces_business_started", "business_id", text("started_at DESC")),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = new_id()
    kind: Mapped[str]  # chat_answer | eval_case | job | setup | role_test | skill_test
    title: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="running")  # running | ok | error
    started_at: Mapped[datetime] = server_now()
    finished_at: Mapped[datetime | None]


class Span(Tenant, Base):
    """One step inside a trace: shield, search, routing, a model call, the citation check."""

    __tablename__ = "spans"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.traces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str]
    started_at: Mapped[datetime] = server_now()
    duration_ms: Mapped[int] = mapped_column(server_default=text("0"))
    attrs: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


class ModelCall(Tenant, Base):
    """The ledger (Task 1.5, D13): one row per model call, ready for billing.

    Text previews are masked (core/pii.py) and cut short. Keys never reach this table.
    """

    __tablename__ = "model_calls"
    __table_args__ = (
        Index("ix_model_calls_business_created", "business_id", text("created_at DESC")),
        Index("ix_model_calls_business_job_model", "business_id", "job", "model_key"),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.traces.id", ondelete="SET NULL"), index=True
    )
    step: Mapped[str]  # what the app was doing, e.g. chat.write_answer
    job: Mapped[str]  # the router's job: tag_job, find_facts, write_answer, ...
    provider: Mapped[str]  # claude | gemini | openrouter | ollama
    model_key: Mapped[str]
    provider_model: Mapped[str]  # the exact model id sent to the provider
    prompt_key: Mapped[str | None]
    prompt_version_id: Mapped[uuid.UUID | None]
    role_version_id: Mapped[uuid.UUID | None]
    input_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    cache_write_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(server_default=text("0"))
    latency_ms: Mapped[int] = mapped_column(server_default=text("0"))
    status: Mapped[str]  # ok | error | refused
    error: Mapped[str | None]
    # The router's decision: token count and how it was counted, complexity, the model it
    # picked and why, whether it moved up a model or to a fallback
    route: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    request_preview: Mapped[str | None]
    response_preview: Mapped[str | None]
    created_at: Mapped[datetime] = server_now()


# ---------------------------------------------------------------- small shared services


class CacheEntry(Base):
    """The cache behind core/cache.py's interface. A Valkey server can replace it later."""

    __tablename__ = "cache_entries"
    __table_args__ = {"schema": "core"}

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.businesses.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[dict[str, Any]]
    expires_at: Mapped[datetime | None] = mapped_column(index=True)


class AuditEvent(Tenant, Base):
    """Who did what: approvals, deletions, key changes, publishing a version."""

    __tablename__ = "audit_log"
    __table_args__ = {"schema": "core"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    action: Mapped[str]
    target: Mapped[str]
    detail: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = server_now()


__all__ = [
    "ARRAY",
    "JSONB",
    "SCHEMAS",
    "ApiKey",
    "AuditEvent",
    "Base",
    "Business",
    "CacheEntry",
    "Connection",
    "Membership",
    "ModelCall",
    "ModelChoice",
    "Prompt",
    "PromptVersion",
    "Role",
    "RoleVersion",
    "RoutingRule",
    "Span",
    "Tenant",
    "Trace",
    "User",
    "WebSession",
    "new_id",
    "server_now",
]
