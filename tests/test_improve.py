"""Feature 5, the Self-Improve Loop: the fence around Opus, the protected paths, the anti-cheat
check, the git branch, approve and undo, the limits, the report and the weekly email.
Opus and the evals are stand-ins here (no model call, no cost); git is real, in a temp repo."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from core.config import get_settings
from core.db import owner_session, tenant_session
from core.models import Membership, PromptVersion
from eval_lab.models import Benchmark, Case, Result, Run
from self_improve import loop, repo, review, weekly
from self_improve.agent import FAKE_RULE, FakeAgentRunner, agent_env, check_tool
from self_improve.models import ImproveRun, WeeklyReport
from self_improve.packer import not_worth_sending
from tests.conftest import make_business
from tests.web_helpers import Browser

# ---------------------------------------------------------------- the fence around Opus


def test_file_tools_stay_inside_the_work_folder(tmp_path: Path) -> None:
    work = tmp_path / "work"
    (work / "benchmark" / "hidden").mkdir(parents=True)
    assert check_tool("Read", {"file_path": str(work / "app.py")}, work) is None
    assert check_tool("Edit", {"file_path": "knowledge_brain/answer.py"}, work) is None
    assert check_tool("Read", {"file_path": str(tmp_path / "secret.txt")}, work)
    assert check_tool("Read", {"file_path": "../repo/benchmark/hidden/v1.yaml"}, work)
    assert check_tool("Read", {"file_path": str(work / "benchmark" / "hidden" / "v1.yaml")}, work)
    assert check_tool("Grep", {"pattern": "x", "path": "/etc"}, work)
    assert check_tool("WebFetch", {"url": "https://example.com"}, work)


def test_the_shell_runs_only_plain_allowed_commands(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    assert check_tool("Bash", {"command": "pytest -q"}, work) is None
    assert check_tool("Bash", {"command": "ruff check ."}, work) is None
    for bad in (
        "cat /repo/benchmark/hidden/v1.yaml",
        "pytest -q | tail",
        "curl https://x.test",
        "echo $ANTHROPIC_API_KEY",
        "pytest; rm -rf .",
        "cat ../secret",
        "python -c 'import os'",
        "git push origin main",
    ):
        assert check_tool("Bash", {"command": bad}, work), bad


def test_opus_gets_no_secret_but_its_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WEB_DB_PASSWORD", "hunter2")
    monkeypatch.setenv("SECRET_KEY", "abc")
    env = agent_env("sk-test", tmp_path)
    assert (
        env["WEB_DB_PASSWORD"] == ""
        and env["SECRET_KEY"] == ""
        and env["ANTHROPIC_API_KEY"] == "sk-test"
    )


def test_protected_paths_and_copied_questions() -> None:
    assert repo.is_protected("benchmark/v1/brain.yaml") and repo.is_protected("tests/test_x.py")
    assert repo.is_protected("prompts/eval.judge/v2.md") and repo.is_protected("eval_lab/rules.py")
    assert not repo.is_protected("prompts/chat.answer_user/v2.md") and not repo.is_protected(
        "knowledge_brain/answer.py"
    )
    question = "How do I connect my custom domain to the website?"
    copied = f"+    if question == '{question}':\n+        return 'Open Settings'"
    assert repo.hardcoded_questions(copied, [question, "Short one?"]) == [question]
    assert repo.hardcoded_questions("+ a general rule about steps", [question]) == []


def test_safety_failures_stay_with_a_person() -> None:
    failed = Result(passed=False, error=None)
    assert not_worth_sending("trick", failed) and not_worth_sending("handoff", failed)
    assert not_worth_sending("url_301", failed)
    assert not_worth_sending("regular", Result(passed=False, error="timeout"))
    assert not_worth_sending("regular", failed) is None


# ---------------------------------------------------------------- git, for real, in a temp repo


async def make_repo(path: Path) -> Path:
    files = {
        "app.py": "def answer() -> str:\n    return 'old'\n",
        "tests/test_app.py": "def test_ok() -> None:\n    assert True\n",
        "benchmark/v1/brain.yaml": "cases: []\n",
        "benchmark/hidden/v1.yaml": "cases: [secret]\n",
        "prompts/eval.judge/v1.md": "Judge fairly.\n",
    }
    for name, body in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(body, encoding="utf-8")
    await repo.git(path, "init", "-q", "-b", "main")
    await repo.git(path, "add", "-A")
    await repo.git(path, *repo.IDENTITY, "commit", "-q", "-m", "start")
    return path


async def test_export_hides_the_hidden_set_and_protect_puts_files_back(tmp_path: Path) -> None:
    source = await make_repo(tmp_path / "repo")
    work = tmp_path / "improve" / "work"
    await repo.export(source, await repo.head(source), work)
    assert (work / "app.py").exists() and not (work / "benchmark" / "hidden").exists()
    assert "secret" not in (await repo.git(work, "log", "--all", "-p"))  # not in its history either
    (work / "tests" / "test_app.py").write_text(
        "def test_ok() -> None:\n    assert 1 == 1  # easier\n", encoding="utf-8"
    )
    (work / "benchmark" / "v1" / "new.yaml").write_text("cases: [easy]\n", encoding="utf-8")
    (work / "app.py").write_text("def answer() -> str:\n    return 'new'\n", encoding="utf-8")
    thrown = await repo.protect(work)
    assert sorted(thrown) == ["benchmark/v1/new.yaml", "tests/test_app.py"]
    assert await repo.changed_files(work) == ["app.py"]
    assert "assert True" in (work / "tests" / "test_app.py").read_text(encoding="utf-8")


async def test_a_branch_then_merge_and_undo(tmp_path: Path) -> None:
    source = await make_repo(tmp_path / "repo")
    base = await repo.head(source)
    work = tmp_path / "improve" / "work"
    await repo.export(source, base, work)
    (work / "app.py").write_text("def answer() -> str:\n    return 'FIXED'\n", encoding="utf-8")
    sha = await repo.to_branch(
        source, base, "improve/test-1", await repo.patch(work), "fix", tmp_path / "trees"
    )
    assert await repo.head(source) == base  # main untouched
    assert "FIXED" in await repo.git(source, "show", f"{sha}:app.py")
    merged = await repo.merge(source, "improve/test-1", into="main", message="merge", who="Tess")
    assert "FIXED" in (source / "app.py").read_text(encoding="utf-8")
    await repo.revert(source, merged, who="Tess")
    assert "old" in (source / "app.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- the loop, end to end


async def _failing_run(business_id: uuid.UUID) -> uuid.UUID:
    """An eval run where one case failed and one passed, plus a 2-case hidden set."""
    async with tenant_session(business_id) as session:
        brain = Benchmark(
            business_id=business_id, name="Mini", version="v1", kind="brain", case_count=2
        )
        hidden = Benchmark(
            business_id=business_id,
            name="Hidden",
            version="v1",
            kind="hidden",
            case_count=2,
            hidden=True,
        )
        session.add_all([brain, hidden])
        await session.flush()
        bad = Case(
            business_id=business_id,
            benchmark_id=brain.id,
            case_key="M-001",
            category="regular",
            question="How do I connect my custom domain to the website?",
            expected="Settings, then Domains.",
            checks={"must_answer": True},
        )
        good = Case(
            business_id=business_id,
            benchmark_id=brain.id,
            case_key="M-002",
            category="regular",
            question="Where is the logo setting?",
            expected="Settings, Branding.",
            checks={"must_answer": True},
        )
        session.add_all(
            [bad, good]
            + [
                Case(
                    business_id=business_id,
                    benchmark_id=hidden.id,
                    case_key=f"H-{i}",
                    category="regular",
                    question=f"Hidden question {i}?",
                    expected="x",
                )
                for i in (1, 2)
            ]
        )
        await session.flush()
        run = Run(
            business_id=business_id,
            benchmark_id=brain.id,
            name="Mini on Sonnet",
            job="brain_answer",
            model_keys=["claude-sonnet-5-5"],
            status="done",
            finished_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        session.add_all(
            [
                Result(
                    business_id=business_id,
                    run_id=run.id,
                    case_id=bad.id,
                    model_key="claude-sonnet-5-5",
                    passed=False,
                    answer="Open the Help page.",
                    judge_score=0.2,
                    judge_reason="Misses the Domains page",
                    cost_usd=Decimal("0.01"),
                ),
                Result(
                    business_id=business_id,
                    run_id=run.id,
                    case_id=good.id,
                    model_key="claude-sonnet-5-5",
                    passed=True,
                    answer="Settings, Branding.",
                    judge_score=0.9,
                    cost_usd=Decimal("0.01"),
                ),
            ]
        )
        return run.id


async def fake_evaluate(work: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Passes the failing case once a prompt has the fake rule or app.py says FIXED."""
    prompts = " ".join(
        p.read_text(encoding="utf-8")
        for p in (work / "self_improve_input" / "prompts").glob("*.md")
    )
    app = (work / "app.py").read_text(encoding="utf-8") if (work / "app.py").exists() else ""
    fixed = FAKE_RULE.strip() in prompts or "FIXED" in app
    scores: dict[str, Any] = {"cost_usd": 0.01}
    for group, ids in spec["groups"].items():
        rate = 1.0 if group != "failing" or fixed else 0.0
        scores[group] = {
            "cases": len(ids),
            "pass_rate": rate,
            "by_category": {"regular": rate},
            "passed": {},
            "failures": []
            if rate
            else [{"case_key": "M-001", "question": "q", "answer": "a", "why": "still wrong"}],
        }
    return scores


async def unit_tests_pass(work: Path) -> tuple[bool, str]:
    return True, "1 passed"


@pytest.fixture
async def improve_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    settings = get_settings()
    monkeypatch.setattr(settings, "self_improve_repo", await make_repo(tmp_path / "repo"))
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "llm_fake", True)
    business_id = await make_business("Improve Co")
    return {
        "business_id": business_id,
        "source_run": await _failing_run(business_id),
        "repo": settings.self_improve_repo,
    }


@pytest.mark.db
async def test_a_prompt_fix_passes_then_becomes_a_gated_prompt_version_and_can_be_undone(
    improve_world: dict[str, Any],
) -> None:
    business_id = improve_world["business_id"]
    run_id = await loop.create_run(
        business_id,
        source_run_id=improve_world["source_run"],
        comment="",
        max_rounds=3,
        max_cost=Decimal("2"),
        user_id=None,
    )
    status = await loop.run_loop(
        business_id, run_id, loop.Tools(FakeAgentRunner(), fake_evaluate, unit_tests_pass)
    )
    assert status == "fixed"
    async with tenant_session(business_id) as session:
        run = await session.get(ImproveRun, run_id)
        assert run is not None and run.rounds_used == 1 and run.branch and run.report_md
    assert "Misses the Domains page" in run.comment  # the judge's reason, as the default comment
    report = improve_world["repo"] / run.report_path
    assert report.exists() and "Hidden check set" in report.read_text(encoding="utf-8")
    decision = await review.approve(
        business_id, run_id, user_id=await _owner(business_id), who="Tess"
    )
    assert decision.gates_to_run  # the new prompt version goes through the release gate
    async with tenant_session(business_id) as session:
        new = (
            await session.execute(
                select(PromptVersion).where(PromptVersion.author == "self_improve")
            )
        ).scalar_one()
        assert new.status == "gating" and FAKE_RULE.strip() in new.body
    await review.undo(business_id, run_id, user_id=await _owner(business_id), who="Tess")
    async with tenant_session(business_id) as session:
        new = (
            await session.execute(
                select(PromptVersion).where(PromptVersion.author == "self_improve")
            )
        ).scalar_one()
        run = await session.get(ImproveRun, run_id)
        assert new.status == "retired" and run is not None and run.status == "undone"


async def _owner(business_id: uuid.UUID) -> uuid.UUID:
    async with owner_session() as session:
        owner = await session.scalar(
            select(Membership.user_id).where(Membership.business_id == business_id)
        )
    assert owner is not None
    return owner


@pytest.mark.db
async def test_a_code_fix_is_merged_and_reverted_and_cheats_are_thrown_away(
    improve_world: dict[str, Any],
) -> None:
    business_id, source = improve_world["business_id"], improve_world["repo"]

    def fix_code(work: Path) -> str:
        (work / "app.py").write_text("def answer() -> str:\n    return 'FIXED'\n", encoding="utf-8")
        (work / "tests" / "test_app.py").write_text(
            "# made easier\n", encoding="utf-8"
        )  # must be thrown away
        return "Fixed answer()"

    run_id = await loop.create_run(
        business_id,
        source_run_id=improve_world["source_run"],
        comment="the steps are wrong",
        max_rounds=2,
        max_cost=Decimal("2"),
        user_id=None,
    )
    assert (
        await loop.run_loop(
            business_id,
            run_id,
            loop.Tools(FakeAgentRunner(fix_code), fake_evaluate, unit_tests_pass),
        )
        == "fixed"
    )
    async with tenant_session(business_id) as session:
        run = await session.get(ImproveRun, run_id)
        assert (
            run is not None
            and run.violations == ["tests/test_app.py"]
            and run.changed_files == ["app.py"]
        )
    await review.approve(business_id, run_id, user_id=await _owner(business_id), who="Tess")
    assert "FIXED" in (source / "app.py").read_text(encoding="utf-8")
    await review.undo(business_id, run_id, user_id=await _owner(business_id), who="Tess")
    assert "old" in (source / "app.py").read_text(encoding="utf-8")


@pytest.mark.db
async def test_the_round_limit_stops_a_run_that_cannot_fix_it(
    improve_world: dict[str, Any],
) -> None:
    business_id = improve_world["business_id"]
    run_id = await loop.create_run(
        business_id,
        source_run_id=improve_world["source_run"],
        comment="",
        max_rounds=2,
        max_cost=Decimal("2"),
        user_id=None,
    )
    stuck = FakeAgentRunner(lambda work: "Tried rewording the answer prompt; no luck.")
    assert (
        await loop.run_loop(business_id, run_id, loop.Tools(stuck, fake_evaluate, unit_tests_pass))
        == "not_fixed"
    )
    async with tenant_session(business_id) as session:
        run = await session.get(ImproveRun, run_id)
    assert (
        run is not None
        and run.rounds_used == 2
        and run.report_md
        and "round limit" in run.report_md
    )
    assert stuck.rounds == 2


# ---------------------------------------------------------------- the weekly email and the screens


@pytest.mark.db
async def test_the_weekly_email_says_what_got_better_and_is_kept_without_gmail(
    improve_world: dict[str, Any],
) -> None:
    business_id = improve_world["business_id"]
    now = datetime.now(UTC)
    async with tenant_session(business_id) as session:
        brain = (
            await session.execute(select(Benchmark).where(Benchmark.kind == "brain"))
        ).scalar_one()
        for days, rate in ((10, 0.8), (1, 0.9)):
            session.add(
                Run(
                    business_id=business_id,
                    benchmark_id=brain.id,
                    name="weekly",
                    job="brain_answer",
                    model_keys=["settings"],
                    status="done",
                    finished_at=now - timedelta(days=days),
                    summary={"settings": {"pass_rate": rate}},
                )
            )
    week_start, subject, body, _ = await weekly.build(business_id, now + timedelta(days=7))
    assert "Mini: 90% (was 80%)" in body and "better" in subject
    report_id = await weekly.make_weekly(business_id, now + timedelta(days=7))
    async with tenant_session(business_id) as session:
        report = await session.get(WeeklyReport, report_id)
    assert report is not None and report.send_status == "stored" and report.week_start == week_start


@pytest.mark.db
async def test_the_self_improve_screens_open_and_a_run_is_queued() -> None:
    browser = Browser()
    try:
        email = await browser.signup("Improve Screens Co")
        from tests.test_evals import owned_business

        business_id = await owned_business(email)
        source = await _failing_run(business_id)
        for path in ("/improve", "/improve/weekly", f"/improve/new?run_id={source}"):
            page = await browser.get(path)
            assert page.status_code == 200, (path, page.text[:300])
        assert "M-001" in (await browser.get(f"/improve/new?run_id={source}")).text
        started = await browser.post(
            "/improve/runs", {"run_id": str(source), "max_rounds": "3", "max_cost": "2"}
        )
        assert started.status_code == 303 and browser.queue.jobs[-1][0] == "improve:run_loop"
        assert (await browser.get(started.headers["location"])).status_code == 200
    finally:
        await browser.close()
