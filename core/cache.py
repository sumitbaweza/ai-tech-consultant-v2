"""A small key-value cache behind one interface (STACK.md S1).

Today it is a Postgres table (core.cache_entries). If measurements show the cache is hot
enough to matter, a Valkey version of the same three methods replaces it without touching
callers.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from core.config import DbRole
from core.db import tenant_session
from core.models import CacheEntry


class Cache(Protocol):
    async def get(self, business_id: uuid.UUID, key: str) -> dict[str, Any] | None: ...

    async def set(
        self, business_id: uuid.UUID, key: str, value: dict[str, Any], ttl_seconds: int | None
    ) -> None: ...

    async def delete(self, business_id: uuid.UUID, key: str) -> None: ...


class PostgresCache:
    def __init__(self, role: DbRole) -> None:
        self.role: DbRole = role

    async def get(self, business_id: uuid.UUID, key: str) -> dict[str, Any] | None:
        async with tenant_session(business_id, role=self.role) as session:
            entry = (
                await session.execute(select(CacheEntry).where(CacheEntry.key == key))
            ).scalar_one_or_none()
            if entry is None:
                return None
            if entry.expires_at is not None and entry.expires_at < datetime.now(UTC):
                await session.delete(entry)
                return None
            return entry.value

    async def set(
        self, business_id: uuid.UUID, key: str, value: dict[str, Any], ttl_seconds: int | None
    ) -> None:
        expires = datetime.now(UTC) + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        async with tenant_session(business_id, role=self.role) as session:
            statement = insert(CacheEntry).values(
                business_id=business_id, key=key, value=value, expires_at=expires
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[CacheEntry.business_id, CacheEntry.key],
                    set_={"value": value, "expires_at": expires},
                )
            )

    async def delete(self, business_id: uuid.UUID, key: str) -> None:
        async with tenant_session(business_id, role=self.role) as session:
            await session.execute(delete(CacheEntry).where(CacheEntry.key == key))
