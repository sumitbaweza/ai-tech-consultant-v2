"""Task 1.1: no business can ever see another business's data. Proved here at the database
level, for every table with a business id; the API-level proof is in test_brain_api.py."""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from core.crypto import encrypt
from core.db import engine, owner_session, tenant_session, user_session
from core.models import Business, Connection, Prompt, Role
from tests.conftest import make_business

pytestmark = pytest.mark.db


async def test_a_business_sees_only_its_own_rows() -> None:
    a, b = await make_business("Alpha"), await make_business("Beta")
    async with tenant_session(a) as session:
        session.add(
            Connection(
                business_id=a, kind="claude", secret_enc=encrypt("sk-ant-x" * 4, business_id=a)
            )
        )
    async with tenant_session(b) as session:
        assert (await session.execute(select(Connection))).scalars().all() == []
        prompts = (await session.execute(select(Prompt.business_id))).scalars().all()
        assert prompts and set(prompts) == {b}
        roles = (await session.execute(select(Role.business_id))).scalars().all()
        assert set(roles) == {b}
        # Even asking for A's business row by its id finds nothing
        assert await session.get(Business, a) is None


async def test_a_forged_business_id_cannot_write_into_another_business() -> None:
    a, b = await make_business(), await make_business()
    with pytest.raises(DBAPIError):  # row-level security's WITH CHECK refuses the row
        async with tenant_session(b) as session:
            session.add(Connection(business_id=a, kind="gemini"))
            await session.flush()


async def test_updates_and_deletes_cannot_reach_another_business() -> None:
    a, b = await make_business(), await make_business()
    async with tenant_session(b) as session:
        updated = await session.execute(text("UPDATE core.prompts SET title = 'hacked'"))
        deleted = await session.execute(text("DELETE FROM core.roles"))
        assert getattr(updated, "rowcount", 0) > 0  # only B's own rows
        assert getattr(deleted, "rowcount", 0) > 0
    async with tenant_session(a) as session:
        titles = (await session.execute(select(Prompt.title))).scalars().all()
        assert titles and "hacked" not in titles
        assert (await session.execute(select(Role))).scalars().all()


async def test_no_business_set_means_no_rows() -> None:
    await make_business()
    async with user_session(None) as session:
        assert (await session.execute(select(Prompt))).scalars().all() == []


async def test_every_business_table_has_row_level_security() -> None:
    async with owner_session() as session:
        rows = await session.execute(
            text(
                """
                SELECT c.table_schema || '.' || c.table_name AS name, t.rowsecurity
                FROM information_schema.columns c
                JOIN pg_tables t ON t.schemaname = c.table_schema AND t.tablename = c.table_name
                WHERE c.column_name = 'business_id'
                  AND c.table_schema IN ('core','brain','chat','evals','improve','golive')
                """
            )
        )
        unprotected = [row.name for row in rows if not row.rowsecurity]
    assert unprotected == ["core.web_sessions"]  # looked up by its token hash, before a business


async def test_web_role_cannot_read_the_brain_schema() -> None:
    """The chat and the Eval Lab must use the Brain's API: the database refuses direct reads."""
    async with engine("web").connect() as connection:
        with pytest.raises(DBAPIError, match="permission denied"):
            await connection.execute(text("SELECT count(*) FROM brain.chunks"))


async def test_brain_role_cannot_read_chat_tables() -> None:
    async with engine("brain").connect() as connection:
        with pytest.raises(DBAPIError, match="permission denied"):
            await connection.execute(text("SELECT count(*) FROM chat.messages"))


async def test_api_key_lookup_returns_only_the_owning_business() -> None:
    from core.security import sha256_hex
    from workspace.service import create_api_key

    a = await make_business()
    async with tenant_session(a) as session:
        key = await create_api_key(session, a, name="t", user_id=None)
    async with engine("brain").connect() as connection:
        found = await connection.scalar(
            text("SELECT core.business_for_api_key(:h)"), {"h": sha256_hex(key.key)}
        )
        missing = await connection.scalar(
            text("SELECT core.business_for_api_key(:h)"), {"h": sha256_hex("btk_nope")}
        )
    assert found == a and missing is None
    assert isinstance(found, uuid.UUID)
