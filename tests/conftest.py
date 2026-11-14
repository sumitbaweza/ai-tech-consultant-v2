"""Test setup. Tests run in Docker (`make test`) against the consultant_test database.

Tests marked `db` need that database (migrated to the latest version) and are skipped when it
isn't reachable. No test calls a real model: the fake provider stands in.
"""

import base64
import os
import socket
import uuid
from collections.abc import AsyncIterator

import pytest

os.environ.setdefault("SECRET_KEY", base64.b64encode(b"t" * 32).decode())
os.environ.setdefault("DB_NAME", "consultant_test")
os.environ["STORAGE_DIR"] = os.environ.get("TEST_STORAGE_DIR", "/tmp/consultant-test-storage")

from core.config import get_settings
from core.db import dispose_engines, owner_session

get_settings.cache_clear()


def _db_reachable() -> bool:
    settings = get_settings()
    try:
        with socket.create_connection((settings.db_host, settings.db_port), timeout=1):
            return True
    except OSError:
        return False


DB_UP = _db_reachable()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if DB_UP:
        return
    skip = pytest.mark.skip(reason="Postgres from docker compose isn't reachable")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
async def _fresh_engines() -> AsyncIterator[None]:
    # Each test gets its own event loop; pooled connections belong to the old loop
    yield
    await dispose_engines()


async def make_business(name: str | None = None) -> uuid.UUID:
    """A business with its owner, made directly as the owner role (skips the screens)."""
    from workspace.service import create_business_for_owner

    async with owner_session() as session:
        business_id = await create_business_for_owner(
            session,
            owner_email=f"{uuid.uuid4().hex[:8]}@test.local",
            owner_name="Test Owner",
            password="test-password",
            business_name=name or f"Biz {uuid.uuid4().hex[:6]}",
        )
    return business_id
