"""improve: keep every tool call Opus made in a round, so a reviewer can audit what it tried

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-14 10:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rounds",
        sa.Column(
            "tool_calls",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        schema="improve",
    )


def downgrade() -> None:
    op.drop_column("rounds", "tool_calls", schema="improve")
