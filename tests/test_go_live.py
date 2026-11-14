"""Feature 6, Go Live: the stopwatch, the starting test set made from the business's own pages,
the readiness score, and the chat that opens only at the owner's level.
The model is the fake provider and the Brain runs in-process: no network, no cost."""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import httpx
import pytest
from sqlalchemy import select

from core.brain_client import BrainClient, use_transport
from core.config import get_settings
from core.db import tenant_session
from core.llm.providers.fake import FakeProvider
from core.models import Business, Role
from eval_lab.models import Benchmark, Case
from go_live import service
from go_live.jobs import build_setup
from go_live.models import Setup
from go_live.starter import make_starter_tests, spread
from tests.conftest import make_business
from tests.test_evals import owned_business
from tests.web_helpers import Browser

DOC = (
    b"# Connecting a custom domain\n\nOpen Settings, then Domains. Enter your domain name and press Save. "
    b"Then create an A record at your registrar pointing to the address shown on the Domains page. "
    b"It can take up to 24 hours for the change to reach everyone.\n"
)


def test_the_stopwatch_times_each_step() -> None:
    setup = Setup(
        business_id=uuid.uuid4(), started_at=datetime.now(UTC) - timedelta(minutes=3), step_times={}
    )
    service.mark(setup, "business", "started")
    service.mark(setup, "business", "done")
    assert "business" in service.done_steps(setup) and service.step_seconds(setup)["business"] >= 0
    assert 175 <= service.seconds_taken(setup) <= 200
    assert service.next_step("roles") == "build" and service.next_step("readiness") == "readiness"


def test_tests_are_spread_over_the_whole_help_center() -> None:
    pages = [{"id": i} for i in range(100)]
    picked = spread(pages, 10)
    assert len(picked) == 10 and picked[0]["id"] == 0 and picked[-1]["id"] == 90


@pytest.fixture
async def brain(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[None]:
    from knowledge_brain.api.main import app as brain_app
    from knowledge_brain.worker import app as brain_jobs

    fake = FakeProvider()
    monkeypatch.setattr(
        "core.llm.client.default_providers",
        lambda: dict.fromkeys(("claude", "gemini", "openrouter", "ollama"), fake),
    )
    async with brain_jobs.open_async():
        use_transport(httpx.ASGITransport(app=brain_app))
        yield
        use_transport(None)


async def _with_document(business_id: uuid.UUID) -> None:
    async with await BrainClient.for_business(business_id) as client:
        await client.request(
            "POST", "/v1/documents", files={"file": ("domains.md", DOC, "text/markdown")}
        )


@pytest.mark.db
async def test_the_starting_test_set_comes_from_the_business_pages(brain: None) -> None:
    business_id = await make_business("Starter Co")
    await _with_document(business_id)
    benchmark_id, made = await make_starter_tests(business_id, [], pages=5)
    assert benchmark_id is not None and made == 2
    async with tenant_session(business_id) as session:
        cases = list(
            (await session.execute(select(Case).where(Case.benchmark_id == benchmark_id))).scalars()
        )
        benchmark = await session.get(Benchmark, benchmark_id)
    assert benchmark is not None and benchmark.kind == "starter"
    assert all("Open Settings, then Domains" in c.expected for c in cases)
    assert {c.case_key for c in cases} == {"ST-001", "ST-002"}


@pytest.mark.db
async def test_build_writes_the_tests_and_scores_them(brain: None) -> None:
    business_id = await make_business("Build Co")
    await _with_document(business_id)
    async with tenant_session(business_id) as session:
        setup = Setup(business_id=business_id, started_at=datetime.now(UTC), status="building")
        session.add(setup)
        await session.flush()
        setup_id = setup.id
    await build_setup(business_id, setup_id, poll=0.1)
    async with tenant_session(business_id) as session:
        done = await session.get(Setup, setup_id)
    assert done is not None and done.status == "waiting" and done.current_step == "readiness"
    assert (
        done.readiness_total == 2 and done.readiness_correct is not None and done.readiness_run_id
    )


@pytest.mark.db
async def test_the_wizard_walks_through_and_the_chat_opens_only_at_the_level(
    brain: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "llm_fake", True)  # the keys step accepts the fake models
    browser = Browser()
    try:
        email = await browser.signup("Wizard Co")
        business_id = await owned_business(email)
        assert (await browser.get("/setup")).headers["location"] == "/setup/business"
        for step in ("business", "keys", "knowledge", "gmail", "roles", "build", "readiness"):
            page = await browser.get(f"/setup/{step}")
            assert page.status_code == 200, (step, page.text[:300])
        assert (
            await browser.post("/setup/business", {"name": "Wizard Co", "tone": "warm and patient"})
        ).status_code == 303
        assert (await browser.post("/setup/keys")).headers["location"] == "/setup/knowledge"
        refused = await browser.post("/setup/knowledge")
        assert "Add a sitemap" in unquote(refused.headers["location"])
        await _with_document(business_id)
        assert (await browser.post("/setup/knowledge")).headers["location"] == "/setup/gmail"
        assert (await browser.post("/setup/gmail")).headers["location"] == "/setup/roles"
        async with tenant_session(business_id) as session:
            role_ids = [str(r.id) for r in (await session.execute(select(Role))).scalars()]
        built = await browser.post("/setup/roles", {"role_ids": role_ids[:2]})
        assert (
            built.headers["location"] == "/setup/build"
            and browser.queue.jobs[-1][0] == "golive:build"
        )
        async with tenant_session(business_id) as session:
            setup = (await session.execute(select(Setup))).scalar_one()
            archived = [r for r in (await session.execute(select(Role))).scalars() if r.archived_at]
            setup.readiness_correct, setup.readiness_total, setup.status = 17, 20, "waiting"  # 85%
        assert len(archived) == len(role_ids) - 2
        closed = await browser.post("/setup/readiness/switch-on")
        assert "Not yet" in unquote(closed.headers["location"])  # 85% is below the default 90%
        await browser.post("/setup/readiness/threshold", {"level": "85"})
        opened = await browser.post("/setup/readiness/switch-on")
        assert unquote(opened.headers["location"]).startswith("/?msg=The chat is on")
        async with tenant_session(business_id) as session:
            business = await session.get(Business, business_id)
            setup = (await session.execute(select(Setup))).scalar_one()
        assert business is not None and business.chat_enabled and setup.finished_at is not None
    finally:
        await browser.close()
