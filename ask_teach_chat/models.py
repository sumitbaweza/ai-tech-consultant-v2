"""The Ask and Teach Chat's tables (schema `chat`): chats, messages, answers with everything
they used, feedback, and lessons (D10, D11)."""

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


class Chat(Tenant, Base):
    """One conversation. Its mode is picked at "New chat"; its role can change mid-chat."""

    __tablename__ = "chats"
    __table_args__ = (
        one_of("mode", ("ask", "teach")),
        Index("ix_chats_business_updated", "business_id", text("updated_at DESC")),
        {"schema": "chat"},
    )

    id: Mapped[uuid.UUID] = new_id()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    title: Mapped[str]
    mode: Mapped[str]
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.roles.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()
    updated_at: Mapped[datetime] = server_now()


class Message(Tenant, Base):
    """Everything said in a chat, in order: questions, answers, teaching, handoff notes."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "seq"),
        one_of("sender", ("user", "agent")),
        one_of(
            "kind",
            (
                "question",
                "answer",
                "dont_know",
                "handoff",
                "teaching",
                "clarifying",
                "lesson_draft",
                "notice",
            ),
        ),
        {"schema": "chat"},
    )

    id: Mapped[uuid.UUID] = new_id()
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat.chats.id", ondelete="CASCADE"))
    seq: Mapped[int]
    sender: Mapped[str]
    kind: Mapped[str]
    body: Mapped[str]  # personal data masked (core/pii.py)
    # The role that was active for this message (the chat's role can change later)
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.roles.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()


class Answer(Tenant, Base):
    """What one agent answer used and cost (D11, D15). Its text is the message it belongs to.

    The trace panel (Task 3.3) reads this row and the model calls with the same trace_id.
    """

    __tablename__ = "answers"
    __table_args__ = {"schema": "chat"}

    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat.messages.id", ondelete="CASCADE"), primary_key=True
    )
    question_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat.messages.id", ondelete="SET NULL")
    )
    # The real question, found inside a long pasted email (Task 3.1)
    question: Mapped[str]
    answered: Mapped[bool]  # False: "I don't know" or "Send to a human"
    needs_human: Mapped[bool] = mapped_column(server_default=text("false"))
    handoff_reason: Mapped[str | None]  # angry | refund | legal | role rule
    expert_task_id: Mapped[uuid.UUID | None]  # brain.expert_tasks row (through the API)
    confidence: Mapped[float | None]
    top_score: Mapped[float | None]  # best search score, compared with the "I don't know" line
    # [{n, chunk_id, knowledge_type, title, url, anchor, quote}]: what the answer cites
    citations: Mapped[list[dict[str, Any]]] = mapped_column(server_default=text("'[]'::jsonb"))
    knowledge_types: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    chunk_ids: Mapped[list[str]] = mapped_column(server_default=text("'{}'"))
    skill_version_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default=text("'{}'"))
    lesson_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default=text("'{}'"))
    instruction_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default=text("'{}'"))
    # D15: which prompt version wrote it ({prompt key: version id}) and which role version
    prompt_versions: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    role_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.role_versions.id", ondelete="SET NULL")
    )
    model_key: Mapped[str | None]
    trace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.traces.id", ondelete="SET NULL")
    )
    cost_usd: Mapped[Decimal] = mapped_column(server_default=text("0"))
    latency_ms: Mapped[int] = mapped_column(server_default=text("0"))
    # What the injection shield and the risk check found (Task 3.6)
    safety: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    # For the trace panel: the skill's and lessons' names, each step's time
    summary: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = server_now()


class Feedback(Tenant, Base):
    """The comment box and thumbs on an answer (Tasks 3.4 and 4.4)."""

    __tablename__ = "feedback"
    __table_args__ = (
        one_of("verdict", ("right", "wrong")),
        {"schema": "chat"},
    )

    id: Mapped[uuid.UUID] = new_id()
    answer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat.answers.message_id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    verdict: Mapped[str]  # right (thumbs up) | wrong (thumbs down)
    why: Mapped[str | None]
    correct_answer: Mapped[str | None]
    # What it turned into: a lesson (Task 3.4) and a new test case (Task 4.4)
    lesson_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat.lessons.id", ondelete="SET NULL", use_alter=True)
    )
    candidate_case_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime] = server_now()


class Lesson(Tenant, Base):
    """A lesson learned from a correction (Task 3.4, D10): a Markdown file plus this record.

    Nothing is used before a human approves it. The approved text is copied into the Brain
    (through its API) so search can find and cite it.
    """

    __tablename__ = "lessons"
    __table_args__ = (
        one_of("status", ("pending", "approved", "off", "rejected")),
        {"schema": "chat"},
    )

    id: Mapped[uuid.UUID] = new_id()
    topic: Mapped[str]  # file name: lessons/<topic>.md
    title: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="pending")
    # D10: where it came from: the chat, the corrected answer, the feedback row
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat.chats.id", ondelete="SET NULL"), index=True
    )
    answer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat.answers.message_id", ondelete="SET NULL")
    )
    taught_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    confidence: Mapped[float]  # how sure the lesson writer is it understood (0 to 1)
    file_path: Mapped[str | None]
    # When the correction fits a whole kind of work, a skill change is suggested instead
    skill_proposal_id: Mapped[uuid.UUID | None]
    # Lessons that may disagree with this one, found at approval time
    conflicts_with: Mapped[list[uuid.UUID]] = mapped_column(server_default=text("'{}'"))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = server_now()


class LessonVersion(Tenant, Base):
    """Every edit of a lesson; the newest one is the lesson's text."""

    __tablename__ = "lesson_versions"
    __table_args__ = (UniqueConstraint("lesson_id", "version"), {"schema": "chat"})

    id: Mapped[uuid.UUID] = new_id()
    lesson_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat.lessons.id", ondelete="CASCADE"))
    version: Mapped[int]
    markdown: Mapped[str]
    rule: Mapped[str]  # the lesson in one or two sentences
    # {question, wrong_answer, right_answer}: the example it came from
    example: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    author: Mapped[str]  # agent | human
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = server_now()
