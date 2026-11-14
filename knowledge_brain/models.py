"""The Knowledge Brain's tables (schema `brain`). Only the Brain's service reads them.

The web app's database role has no access to this schema at all, so the Ask and Teach Chat
and the Eval Lab can reach this data only through the Brain's HTTP API (Task 2.8).

Statuses and kinds are plain text with a CHECK constraint listing the allowed values: SQL
stays readable (`WHERE level = 'active'`) and the database rejects a value the code doesn't
know. There are no relationship() attributes: queries join explicitly, which avoids hidden
lazy loads (async SQLAlchemy raises an error on those).
"""

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base, Tenant, new_id, server_now

# voyage-4-lite gives 1,024 numbers per text. Stored as halfvec (2 bytes each, D7).
EMBEDDING_DIMENSIONS = 1024

# The 5 knowledge types of Task 2.1, plus what the Brain also stores and cites: uploaded
# documents (Go Live), approved lessons (Task 3.4) and experts' answers (Task 3.2)
KNOWLEDGE_TYPES = (
    "sitemap",
    "page_url",
    "instruction",
    "skill",
    "past_email",
    "document",
    "lesson",
    "expert",
)
LEVELS = ("active", "watch", "quarantined", "archived")
# Answers may use these levels only; quarantined and archived pages never appear (Task 2.4)
SEARCHABLE_LEVELS = ("active", "watch")


def one_of(column: str, values: tuple[str, ...], name: str | None = None) -> CheckConstraint:
    """A CHECK constraint that allows only the listed values in a text column."""
    allowed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name or column)


# ---------------------------------------------------------------- sources and sitemaps


class Source(Tenant, Base):
    """Something the Brain reads: a sitemap, a single page URL, an uploaded document, Gmail."""

    __tablename__ = "sources"
    __table_args__ = (
        one_of("kind", ("sitemap", "page_url", "document", "gmail")),
        one_of("status", ("planned", "reading", "ready", "paused", "error")),
        one_of("action_rule", ("quarantine", "archive", "quarantine_then_archive")),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = new_id()
    kind: Mapped[str]
    label: Mapped[str]
    url: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="planned")
    # Recheck schedule (Task 2.4): every 1, 7, 15 or 30 days; None for documents
    recheck_days: Mapped[int | None]
    next_check_at: Mapped[datetime | None] = mapped_column(index=True)
    last_synced_at: Mapped[datetime | None]
    # URL health (Task 2.4): X, the refreshes in a row that miss a URL before an action is
    # proposed, and what that action is
    miss_threshold: Mapped[int] = mapped_column(server_default=text("2"))
    action_rule: Mapped[str] = mapped_column(server_default="quarantine_then_archive")
    # Crawl speed, the Gmail sync position (history id), anything kind-specific
    settings: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    error: Mapped[str | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


class Sitemap(Tenant, Base):
    """A sitemap file: one the user added, or one found inside it (nested, D2: parent_id)."""

    __tablename__ = "sitemaps"
    __table_args__ = (UniqueConstraint("business_id", "url"), {"schema": "brain"})

    id: Mapped[uuid.UUID] = new_id()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brain.sources.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.sitemaps.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str]
    is_index: Mapped[bool] = mapped_column(server_default=text("false"))
    depth: Mapped[int] = mapped_column(server_default=text("0"))
    # <url> entries in the file at the last read, duplicates inside it included
    url_count: Mapped[int] = mapped_column(server_default=text("0"))
    first_url: Mapped[str | None]
    last_url: Mapped[str | None]
    last_checked_at: Mapped[datetime | None]
    last_error: Mapped[str | None]
    created_at: Mapped[datetime] = server_now()


# ---------------------------------------------------------------- pages and their health


class Page(Tenant, Base):
    """One URL (from a sitemap or added alone), an uploaded document, or an expert's answer.

    One row per URL per business, however many sitemaps list it (D3).
    """

    __tablename__ = "pages"
    __table_args__ = (
        UniqueConstraint("business_id", "url"),
        one_of("origin", ("sitemap", "page_url", "document", "expert")),
        one_of("kind", ("article", "listing")),
        one_of("level", LEVELS),
        one_of("fetch_state", ("discovered", "pending", "fetched", "failed")),
        Index("ix_pages_business_level", "business_id", "level"),
        Index("ix_pages_fetch_queue", "business_id", "run_id", "fetch_state"),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = new_id()
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.sources.id", ondelete="SET NULL"), index=True
    )
    url: Mapped[str]
    # Which knowledge type its chunks carry: sitemap, page_url, document or expert
    origin: Mapped[str]
    # listing: a page that is mostly links (a category or folder page). Tracked, not embedded.
    kind: Mapped[str] = mapped_column(server_default="article")
    title: Mapped[str | None]
    # Where the page sits on its site, e.g. "Emails > Email Accounts" (from breadcrumbs)
    breadcrumb: Mapped[str | None]
    content_md: Mapped[str | None]
    # SHA-256 of the cleaned title and text: the page changed only if this changed
    content_hash: Mapped[str | None]
    # content_hash when the chunks were last built; a different value means "re-embed"
    embedded_hash: Mapped[str | None]
    # URL health (Task 2.4, D4): the level now, and refreshes in a row that missed it
    level: Mapped[str] = mapped_column(server_default="active")
    miss_count: Mapped[int] = mapped_column(server_default=text("0"))
    level_changed_at: Mapped[datetime | None]
    # A 301 points here: the new page. The old page is quarantined.
    moved_to_page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.pages.id", ondelete="SET NULL")
    )
    http_status: Mapped[int | None]
    error: Mapped[str | None]
    # Sent back on the next fetch, so an unchanged page can answer 304 Not Modified
    etag: Mapped[str | None]
    last_modified: Mapped[str | None]
    sitemap_lastmod: Mapped[datetime | None]
    # The read queue (Task 2.3): discovered (listed, not confirmed) -> pending -> fetched or
    # failed. claimed_at: when a batch job took the page. An old claim means that job died,
    # so the next batch takes the page again: that is how a stopped read resumes.
    fetch_state: Mapped[str] = mapped_column(server_default="discovered")
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.refresh_runs.id", ondelete="SET NULL")
    )
    claimed_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    fetched_at: Mapped[datetime | None]
    changed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = server_now()


class PageSitemap(Tenant, Base):
    """Which sitemaps list a URL (D3). Two rows still listed for one page = a duplicate."""

    __tablename__ = "page_sitemaps"
    __table_args__ = {"schema": "brain"}

    page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brain.pages.id", ondelete="CASCADE"), primary_key=True
    )
    sitemap_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brain.sitemaps.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    position: Mapped[int]  # order in the file: gives the first and last URL
    lastmod: Mapped[datetime | None]
    first_seen_at: Mapped[datetime] = server_now()
    last_seen_at: Mapped[datetime] = server_now()
    # Set when a read of the sitemap no longer lists the URL; cleared if it comes back
    removed_at: Mapped[datetime | None]


class PageVersion(Tenant, Base):
    """The change history (Task 2.4, D5): a full copy each time the content hash changed."""

    __tablename__ = "page_versions"
    __table_args__ = (UniqueConstraint("page_id", "version"), {"schema": "brain"})

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    page_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brain.pages.id", ondelete="CASCADE"))
    version: Mapped[int]
    change: Mapped[str]  # new | changed | restored
    content_hash: Mapped[str]
    title: Mapped[str | None]
    content_md: Mapped[str]
    # Lines added and removed against the version before, for the history list
    lines_added: Mapped[int] = mapped_column(server_default=text("0"))
    lines_removed: Mapped[int] = mapped_column(server_default=text("0"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.refresh_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


class HealthEvent(Tenant, Base):
    """The health history of a URL (D4): every miss, move, recovery and level change."""

    __tablename__ = "health_events"
    __table_args__ = (
        one_of(
            "event",
            (
                "missing",
                "not_found",
                "moved",
                "recovered",
                "quarantined",
                "archived",
                "restored",
                "error",
            ),
        ),
        {"schema": "brain"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brain.pages.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.refresh_runs.id", ondelete="SET NULL")
    )
    event: Mapped[str]
    from_level: Mapped[str]
    to_level: Mapped[str]
    miss_count: Mapped[int]
    detail: Mapped[str | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


class RefreshRun(Tenant, Base):
    """One read of a source: the first full read, a scheduled recheck, or a manual one."""

    __tablename__ = "refresh_runs"
    __table_args__ = (
        one_of("kind", ("first_read", "recheck", "manual", "starter")),
        one_of("status", ("running", "done", "failed", "cancelled")),
        Index("ix_refresh_runs_business_started", "business_id", text("started_at DESC")),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = new_id()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brain.sources.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="running")
    urls_total: Mapped[int] = mapped_column(server_default=text("0"))
    fetched: Mapped[int] = mapped_column(server_default=text("0"))
    new_pages: Mapped[int] = mapped_column(server_default=text("0"))
    changed: Mapped[int] = mapped_column(server_default=text("0"))
    unchanged: Mapped[int] = mapped_column(server_default=text("0"))
    failed: Mapped[int] = mapped_column(server_default=text("0"))
    missing: Mapped[int] = mapped_column(server_default=text("0"))
    moved: Mapped[int] = mapped_column(server_default=text("0"))
    recovered: Mapped[int] = mapped_column(server_default=text("0"))
    duplicates_skipped: Mapped[int] = mapped_column(server_default=text("0"))
    # The Refresh Run view (Task 2.4): first and last fetch time
    first_fetch_at: Mapped[datetime | None]
    last_fetch_at: Mapped[datetime | None]
    started_at: Mapped[datetime] = server_now()
    finished_at: Mapped[datetime | None]
    error: Mapped[str | None]


class RefreshRunUrl(Tenant, Base):
    """Every URL a run fetched and what happened: the run's full, scrollable URL list."""

    __tablename__ = "refresh_run_urls"
    __table_args__ = {"schema": "brain"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brain.refresh_runs.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.pages.id", ondelete="SET NULL")
    )
    url: Mapped[str]
    http_status: Mapped[int | None]
    # new | changed | unchanged | missing | not_found | moved | recovered | failed
    outcome: Mapped[str]
    fetched_at: Mapped[datetime] = server_now()


# ---------------------------------------------------------------- images and documents


class Image(Tenant, Base):
    """A screenshot on a page, and what a model says it shows (Task 2.3)."""

    __tablename__ = "images"
    __table_args__ = (
        UniqueConstraint("page_id", "position"),
        one_of("state", ("pending", "described", "failed", "skipped")),
        Index("ix_images_business_sha256", "business_id", "sha256"),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = new_id()
    page_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brain.pages.id", ondelete="CASCADE"))
    position: Mapped[int]  # order on the page
    src_url: Mapped[str]
    alt: Mapped[str | None]
    # The paragraph around the image, given to the model as context
    context: Mapped[str | None]
    # SHA-256 of the bytes: the same screenshot is described (and paid for) once per business
    sha256: Mapped[str | None]
    storage_path: Mapped[str | None]  # a local copy, so the source viewer can show it (S4)
    description: Mapped[str | None]
    described_by: Mapped[str | None]  # model key
    state: Mapped[str] = mapped_column(server_default="pending")
    error: Mapped[str | None]
    created_at: Mapped[datetime] = server_now()


class Document(Tenant, Base):
    """An uploaded file (PDF, DOCX, Markdown, text). Its text is stored as a page."""

    __tablename__ = "documents"
    __table_args__ = {"schema": "brain"}

    id: Mapped[uuid.UUID] = new_id()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brain.sources.id", ondelete="CASCADE"), index=True
    )
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.pages.id", ondelete="SET NULL")
    )
    filename: Mapped[str]
    mime: Mapped[str]
    size_bytes: Mapped[int]
    sha256: Mapped[str]
    storage_path: Mapped[str]
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


# ---------------------------------------------------------------- instructions and skills


class Instruction(Tenant, Base):
    """A rule a person wrote in plain words (Task 2.5), for all roles or for chosen roles."""

    __tablename__ = "instructions"
    __table_args__ = {"schema": "brain"}

    id: Mapped[uuid.UUID] = new_id()
    title: Mapped[str]
    body: Mapped[str]
    enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    # D12: True applies it to every role; otherwise only to the roles in role_ids
    all_roles: Mapped[bool] = mapped_column(server_default=text("true"))
    role_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default=text("'{}'"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()
    updated_at: Mapped[datetime] = server_now()


class Skill(Tenant, Base):
    """A step-by-step guide for one kind of work (Task 2.5). Its Markdown lives in versions."""

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("business_id", "key"), {"schema": "brain"})

    id: Mapped[uuid.UUID] = new_id()
    key: Mapped[str]
    name: Mapped[str]
    archived_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = server_now()


class SkillVersion(Tenant, Base):
    """Every change to a skill is a new row (D12); exactly one is live."""

    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint("skill_id", "version"),
        Index(
            "uq_skill_versions_one_live",
            "skill_id",
            unique=True,
            postgresql_where=text("status = 'live'"),
        ),
        one_of("status", ("draft", "gating", "live", "blocked", "retired")),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = new_id()
    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brain.skills.id", ondelete="CASCADE"))
    version: Mapped[int]
    # The whole skill: name, when to use it, the steps, 1 or 2 examples
    markdown: Mapped[str]
    when_to_use: Mapped[str]  # copied out of the Markdown, for picking the right skill
    status: Mapped[str]
    # human | agent_draft | from_notes (Opus, Task 2.6) | from_lesson | self_improve | default
    author: Mapped[str]
    note: Mapped[str | None]
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.skill_proposals.id", ondelete="SET NULL", use_alter=True)
    )
    gate_id: Mapped[uuid.UUID | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


class SkillProposal(Tenant, Base):
    """A skill change a model suggested from people's notes or a lesson, waiting for a human.

    D12: note_ids are the notes it used. Nothing changes until a human approves (Task 2.6).
    """

    __tablename__ = "skill_proposals"
    __table_args__ = (
        one_of("status", ("pending", "approved", "rejected")),
        one_of("origin", ("email_notes", "lesson", "self_improve")),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = new_id()
    skill_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.skills.id", ondelete="CASCADE"), index=True
    )
    origin: Mapped[str]
    base_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.skill_versions.id", ondelete="SET NULL")
    )
    proposed_name: Mapped[str]
    proposed_markdown: Mapped[str]
    rationale: Mapped[str]
    # What changed, as the model explained it, one line per change
    changes: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    note_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default=text("'{}'"))
    lesson_id: Mapped[uuid.UUID | None]  # chat.lessons row, when it came from a lesson
    model_key: Mapped[str | None]
    trace_id: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(server_default="pending")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = server_now()


# ---------------------------------------------------------------- past emails (Task 2.6)


class EmailThread(Tenant, Base):
    """A Gmail thread (D9: the text is stored, masked; Gmail is asked only once per thread)."""

    __tablename__ = "email_threads"
    __table_args__ = (UniqueConstraint("business_id", "gmail_thread_id"), {"schema": "brain"})

    id: Mapped[uuid.UUID] = new_id()
    gmail_thread_id: Mapped[str]
    subject: Mapped[str]
    participants: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    snippet: Mapped[str | None]
    message_count: Mapped[int] = mapped_column(server_default=text("0"))
    last_message_at: Mapped[datetime | None]
    # Gmail's history id when we stored it: a newer one means the thread has new messages
    history_id: Mapped[str | None]
    fetched_at: Mapped[datetime] = server_now()


class EmailMessage(Tenant, Base):
    """One message in a thread. Attachments: names and sizes only, never the files (D9)."""

    __tablename__ = "email_messages"
    __table_args__ = (UniqueConstraint("business_id", "gmail_message_id"), {"schema": "brain"})

    id: Mapped[uuid.UUID] = new_id()
    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brain.email_threads.id", ondelete="CASCADE"), index=True
    )
    gmail_message_id: Mapped[str]
    position: Mapped[int]
    from_addr: Mapped[str]
    to_addrs: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    sent_at: Mapped[datetime | None]
    subject: Mapped[str]
    body_text: Mapped[str]  # personal data masked before it is stored
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(server_default=text("'[]'::jsonb"))


class EmailNote(Tenant, Base):
    """A person's note on a whole thread (message_id empty) or on one message (D9)."""

    __tablename__ = "email_notes"
    __table_args__ = (one_of("status", ("new", "used")), {"schema": "brain"})

    id: Mapped[uuid.UUID] = new_id()
    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brain.email_threads.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.email_messages.id", ondelete="CASCADE")
    )
    body: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="new")
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.skill_proposals.id", ondelete="SET NULL")
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


# ---------------------------------------------------------------- lessons and experts


class LessonDoc(Tenant, Base):
    """An approved lesson, copied in by the chat (Task 3.4) so search can find and cite it.

    The chat's own tables keep the lesson's record and versions; this copy is only for
    search. Switching a lesson off or deleting it removes it from here.
    """

    __tablename__ = "lesson_docs"
    __table_args__ = {"schema": "brain"}

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # the chat's lesson id
    topic: Mapped[str]
    title: Mapped[str]
    markdown: Mapped[str]
    version: Mapped[int]
    updated_at: Mapped[datetime] = server_now()


class ExpertTask(Tenant, Base):
    """A question the agent couldn't answer, waiting for a human expert (Task 3.2).

    The expert's answer is saved as a page (origin expert), so the next search finds it.
    """

    __tablename__ = "expert_tasks"
    __table_args__ = (
        Index(
            "uq_expert_tasks_open_question",
            "business_id",
            "question_key",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        one_of("status", ("open", "answered", "dismissed")),
        {"schema": "brain"},
    )

    id: Mapped[uuid.UUID] = new_id()
    question: Mapped[str]
    # The question's words in lower case without punctuation, so a repeat adds to the count
    question_key: Mapped[str]
    # Why it came here: dont_know | handoff_angry | handoff_refund | handoff_legal | thumbs_down
    reason: Mapped[str]
    # The chat, role and answer it came from, as ids the chat understands
    context: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column(server_default="open")
    asked_count: Mapped[int] = mapped_column(server_default=text("1"))
    last_asked_at: Mapped[datetime] = server_now()
    answer: Mapped[str | None]
    answered_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    answered_at: Mapped[datetime | None]
    answer_page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.pages.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


# ---------------------------------------------------------------- the search index


class Chunk(Tenant, Base):
    """A searchable piece of knowledge, with its words (tsv) and its meaning (embedding).

    Exactly one of the source columns is set, matching knowledge_type. `searchable` is kept
    in step with the source: a quarantined page, a switched-off instruction or lesson, a
    retired skill version all turn it false, so search never shows them (D6-D8).
    """

    __tablename__ = "chunks"
    __table_args__ = (
        one_of("knowledge_type", KNOWLEDGE_TYPES),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
        Index("ix_chunks_business_type", "business_id", "knowledge_type"),
        {"schema": "brain"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    knowledge_type: Mapped[str]
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.pages.id", ondelete="CASCADE"), index=True
    )
    image_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.images.id", ondelete="CASCADE"), index=True
    )
    instruction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.instructions.id", ondelete="CASCADE"), index=True
    )
    skill_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.skill_versions.id", ondelete="CASCADE"), index=True
    )
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.email_messages.id", ondelete="CASCADE"), index=True
    )
    lesson_doc_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brain.lesson_docs.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int]
    # Where the chunk sits: "Emails > Email Accounts > Add an email account > Step 2"
    heading: Mapped[str]
    # The paragraph's anchor, so one click opens the exact paragraph (Task 3.2)
    anchor: Mapped[str | None]
    content: Mapped[str]
    token_count: Mapped[int]
    searchable: Mapped[bool] = mapped_column(server_default=text("true"))
    embedding: Mapped[Any] = mapped_column(HALFVEC(EMBEDDING_DIMENSIONS), nullable=True)
    embedding_model: Mapped[str | None]
    # Full-text search terms, weighted: heading words count more than body words. Postgres
    # computes and stores them whenever heading or content change.
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(heading, '')), 'A') || "
            "setweight(to_tsvector('english', content), 'B')",
            persisted=True,
        ),
    )
    created_at: Mapped[datetime] = server_now()


class Term(Base):
    """Every word in the business's knowledge, for fixing typos in questions (D8, pg_trgm)."""

    __tablename__ = "terms"
    __table_args__ = (
        Index(
            "ix_terms_term_trgm",
            "term",
            postgresql_using="gin",
            postgresql_ops={"term": "gin_trgm_ops"},
        ),
        {"schema": "brain"},
    )

    business_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.businesses.id", ondelete="CASCADE"), primary_key=True
    )
    term: Mapped[str] = mapped_column(primary_key=True)
    doc_count: Mapped[int] = mapped_column(server_default=text("1"))


class Synonym(Tenant, Base):
    """Words that mean the same in this business ("plan" = "membership"), set in Settings."""

    __tablename__ = "synonyms"
    __table_args__ = (UniqueConstraint("business_id", "term"), {"schema": "brain"})

    id: Mapped[uuid.UUID] = new_id()
    term: Mapped[str]
    synonyms: Mapped[list[str]]
    created_at: Mapped[datetime] = server_now()
