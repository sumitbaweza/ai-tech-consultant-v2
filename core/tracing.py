"""Traces: every step of one run (a chat answer, an eval case, a job), with its time.

The trace row is written first, in its own transaction, because model calls written during
the run point at it. The steps (spans) are kept in memory and saved when the run ends.
"""

import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from core.config import DbRole
from core.db import tenant_session
from core.models import Span, Trace


@dataclass
class SpanRecord:
    name: str
    started_at: datetime
    duration_ms: int = 0
    attrs: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class Tracer:
    business_id: uuid.UUID
    trace_id: uuid.UUID
    role: DbRole
    spans: list[SpanRecord] = field(default_factory=list[SpanRecord])

    @classmethod
    async def start(
        cls, business_id: uuid.UUID, kind: str, title: str, *, role: DbRole = "web"
    ) -> Tracer:
        trace_id = uuid.uuid4()
        async with tenant_session(business_id, role=role) as session:
            session.add(Trace(id=trace_id, business_id=business_id, kind=kind, title=title[:200]))
        return cls(business_id=business_id, trace_id=trace_id, role=role)

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Generator[dict[str, Any]]:
        """Time a step. The yielded dict can be filled with what the step found."""
        record = SpanRecord(name=name, started_at=datetime.now(UTC), attrs=dict(attrs))
        started = time.monotonic()
        try:
            yield record.attrs
        except Exception as exc:
            record.attrs["error"] = type(exc).__name__
            raise
        finally:
            record.duration_ms = int((time.monotonic() - started) * 1000)
            self.spans.append(record)

    async def finish(self, status: str = "ok") -> None:
        async with tenant_session(self.business_id, role=self.role) as session:
            for record in self.spans:
                session.add(
                    Span(
                        business_id=self.business_id,
                        trace_id=self.trace_id,
                        name=record.name,
                        started_at=record.started_at,
                        duration_ms=record.duration_ms,
                        attrs=record.attrs,
                    )
                )
            await session.execute(
                update(Trace)
                .where(Trace.id == self.trace_id)
                .values(status=status, finished_at=datetime.now(UTC))
            )
