"""Feature 1 through the screens: sign-up, Settings, keys, prompts, roles, CSRF, deletion."""

import re
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text

from core.db import owner_session
from core.models import AuditEvent, Connection, ModelCall, PromptVersion
from tests.web_helpers import Browser

pytestmark = pytest.mark.db


@pytest.fixture
async def browser() -> AsyncIterator[Browser]:
    b = Browser()
    await b.signup()
    yield b
    await b.close()


async def test_signup_lands_in_setup_and_every_settings_tab_renders(browser: Browser) -> None:
    for path in (
        "/settings/business",
        "/settings/keys",
        "/settings/models",
        "/settings/prompts",
        "/settings/roles",
        "/settings/routing",
        "/settings/members",
        "/settings/advanced",
        "/settings/danger",
        "/logs",
        "/logs/traces",
        "/logs/calls",
        "/logs/failed",
        "/businesses",
    ):
        response = await browser.get(path)
        assert response.status_code == 200, (path, response.text[:300])


async def test_pages_need_a_login() -> None:
    b = Browser()
    response = await b.get("/settings/keys")
    assert response.status_code == 303 and response.headers["location"] == "/login"
    await b.close()


async def test_post_without_csrf_token_is_refused(browser: Browser) -> None:
    response = await browser.http.post(
        "/settings/business",
        data={"name": "x", "tone": "y", "readiness_threshold": "80", "dont_know_threshold": "0.3"},
    )
    assert response.status_code == 403


async def test_a_saved_key_is_masked_and_never_shown_or_logged(browser: Browser) -> None:
    secret = "sk-ant-api03-" + uuid.uuid4().hex
    response = await browser.post("/settings/keys/claude", {"value": secret}, htmx=True)
    assert response.status_code == 200
    assert secret not in response.text and secret[-4:] in response.text
    page = await browser.get("/settings/keys")
    assert secret not in page.text
    async with owner_session() as session:
        stored = (
            (
                await session.execute(
                    select(Connection.secret_enc).where(Connection.kind == "claude")
                )
            )
            .scalars()
            .all()
        )
        assert stored and all(secret.encode() not in blob for blob in stored if blob)
        audit = (await session.execute(select(AuditEvent.detail))).scalars().all()
        calls = (await session.execute(select(ModelCall.request_preview))).scalars().all()
    assert all(secret not in str(row) for row in [*audit, *calls])


async def test_prompt_save_warns_about_a_missing_variable_then_versions(browser: Browser) -> None:
    key = "chat.answer_user"
    editor = await browser.get(f"/settings/prompts/{key}")
    assert editor.status_code == 200 and "{question}" in editor.text
    warned = await browser.post(
        f"/settings/prompts/{key}", {"body": "Only {knowledge_results} now"}
    )
    assert (
        warned.status_code == 200
        and "Save anyway" in warned.text
        and "{customer_message}" in warned.text
    )
    saved = await browser.post(
        f"/settings/prompts/{key}", {"body": "Only {knowledge_results} now", "confirm": "true"}
    )
    assert saved.status_code == 303
    page = await browser.get(f"/settings/prompts/{key}")
    assert "v2" in page.text
    # No benchmark is loaded for a new business, so nothing could drop: the gate passes it
    restored = await browser.post(f"/settings/prompts/{key}/restore", {"source": "default"})
    assert restored.status_code == 303
    async with owner_session() as session:
        versions = (
            await session.execute(
                text(
                    "SELECT v.version, v.status FROM core.prompt_versions v JOIN core.prompts p ON p.id = v.prompt_id "
                    "WHERE p.key = :k AND p.business_id IN (SELECT business_id FROM core.memberships m JOIN core.users u ON u.id = m.user_id "
                    "WHERE u.name = 'Tess') ORDER BY v.created_at DESC LIMIT 3"
                ),
                {"k": key},
            )
        ).all()
    assert versions[0].status == "live" and versions[0].version == 3


async def test_a_new_role_appears_and_has_a_live_version(browser: Browser) -> None:
    created = await browser.post(
        "/settings/roles",
        {
            "name": "Billing Helper",
            "goal": "Explain invoices",
            "prompt": "You explain invoices simply.",
            "tone": "kind",
            "cap_answer_from_brain": "1",
            "kt_sitemap": "1",
        },
    )
    assert created.status_code == 303
    roles = await browser.get("/settings/roles")
    assert "Billing Helper" in roles.text and "Support Engineer" in roles.text


async def test_deleting_a_business_leaves_no_row_behind(browser: Browser) -> None:
    async with owner_session() as session:
        business_id = (
            await session.execute(
                text(
                    "SELECT m.business_id FROM core.memberships m JOIN core.users u ON u.id = m.user_id "
                    "WHERE u.name = 'Tess' ORDER BY m.created_at DESC LIMIT 1"
                )
            )
        ).scalar_one()
    page = await browser.get("/settings/danger")
    name = re.search(r"This deletes <strong>([^<]+)</strong>", page.text)
    assert name
    response = await browser.post("/settings/danger/delete", {"confirm_name": name.group(1)})
    assert response.status_code == 303
    async with owner_session() as session:
        tables = (
            (
                await session.execute(
                    text(
                        "SELECT table_schema || '.' || table_name FROM information_schema.columns WHERE column_name = 'business_id' "
                        "AND table_schema IN ('core','brain','chat','evals','improve','golive')"
                    )
                )
            )
            .scalars()
            .all()
        )
        leftovers = {}
        for table in tables:
            count = await session.scalar(
                text(f"SELECT count(*) FROM {table} WHERE business_id = :b"), {"b": business_id}
            )
            if count:
                leftovers[table] = count
        assert leftovers == {}
        assert (
            await session.scalar(
                text("SELECT count(*) FROM core.businesses WHERE id = :b"), {"b": business_id}
            )
            == 0
        )
    _ = PromptVersion
