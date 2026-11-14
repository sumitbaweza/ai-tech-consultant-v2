"""Go Live's table (schema `golive`): one guided setup, timed step by step (Task 6)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, text
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base, Tenant, new_id, server_now

# The wizard's steps, in order (Task 6.1)
STEPS = ("business", "keys", "knowledge", "gmail", "roles", "build", "readiness")


class Setup(Tenant, Base):
    """A business's setup run: when each step started and ended, and the readiness score."""

    __tablename__ = "setups"
    __table_args__ = {"schema": "golive"}

    id: Mapped[uuid.UUID] = new_id()
    current_step: Mapped[str] = mapped_column(server_default="business")
    # {step: {"started_at": ..., "done_at": ...}}: the stopwatch, step by step (Task 6.4)
    step_times: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    # The starter slice read first, so the chat can open while the full read carries on
    starter_pages: Mapped[int] = mapped_column(server_default=text("150"))
    source_ids: Mapped[list[uuid.UUID]] = mapped_column(server_default=text("'{}'"))
    # The starting test set made from the business's own pages (Task 6.2) and its run (6.3)
    starter_benchmark_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evals.benchmarks.id", ondelete="SET NULL")
    )
    readiness_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evals.runs.id", ondelete="SET NULL")
    )
    readiness_correct: Mapped[int | None]
    readiness_total: Mapped[int | None]
    status: Mapped[str] = mapped_column(server_default="running")  # running | ready | waiting
    started_at: Mapped[datetime] = server_now()
    finished_at: Mapped[datetime | None]
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
