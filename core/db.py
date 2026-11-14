"""Database engines and sessions.

Every session that works for a business sets `app.business_id` at the start of each
transaction, and row-level security (DATABASE.md D1) then shows that business's rows only.
The setting is transaction-local (`set_config(..., true)`), so a pooled connection never carries
one business's id into the next request.
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, SessionTransaction

from core.config import DbRole, get_settings

_engines: dict[DbRole, AsyncEngine] = {}
_factories: dict[DbRole, async_sessionmaker[AsyncSession]] = {}


class TenantSession(Session):
    """A Session that tells Postgres which business (and user) it works for."""


@event.listens_for(TenantSession, "after_begin")
def _set_tenant(session: Session, transaction: SessionTransaction, connection: Connection) -> None:
    # Runs at the start of every transaction, including ones after a commit inside a request
    business_id = session.info.get("business_id")
    user_id = session.info.get("user_id")
    connection.exec_driver_sql(
        "SELECT set_config('app.business_id', %(business)s, true),"
        " set_config('app.user_id', %(user)s, true)",
        {"business": str(business_id or ""), "user": str(user_id or "")},
    )


def engine(role: DbRole) -> AsyncEngine:
    """One engine (connection pool) per database role, made on first use."""
    if role not in _engines:
        _engines[role] = create_async_engine(
            get_settings().database_url(role),
            # Test a pooled connection before using it, so a database restart (or a laptop
            # waking from sleep) doesn't break the next request
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
        )
    return _engines[role]


def _factory(role: DbRole) -> async_sessionmaker[AsyncSession]:
    if role not in _factories:
        _factories[role] = async_sessionmaker(
            engine(role),
            sync_session_class=TenantSession,
            # Loaded rows stay readable after a commit (async SQLAlchemy can't lazy-reload)
            expire_on_commit=False,
        )
    return _factories[role]


@asynccontextmanager
async def tenant_session(
    business_id: uuid.UUID,
    *,
    role: DbRole = "web",
    user_id: uuid.UUID | None = None,
) -> AsyncGenerator[AsyncSession]:
    """A session that sees one business's rows. Commits at the end, rolls back on error."""
    async with _factory(role)() as session:
        session.info["business_id"] = business_id
        session.info["user_id"] = user_id
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def user_session(
    user_id: uuid.UUID | None, *, role: DbRole = "web"
) -> AsyncGenerator[AsyncSession]:
    """A session before a business is chosen: sign-up, login, the business list."""
    async with _factory(role)() as session:
        session.info["user_id"] = user_id
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def owner_session() -> AsyncGenerator[AsyncSession]:
    """The owner role skips row-level security: only for the seed script and tests."""
    async with _factory("owner")() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def dispose_engines() -> None:
    for db_engine in _engines.values():
        await db_engine.dispose()
    _engines.clear()
    _factories.clear()
