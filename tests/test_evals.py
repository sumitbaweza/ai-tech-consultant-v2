"""Feature 4, the Eval Lab: rule scoring, totals, the "I don't know" sweep, projections, the
benchmark files, a full run through the Brain's API, the release gate and the screens.
The model is the fake provider and the Brain runs in-process: no network, no cost."""

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from core.brain_client import BrainClient, use_transport
from core.config import REPO_ROOT
from core.db import owner_session, tenant_session
from core.llm.providers.fake import FakeProvider
from core.models import Business, Membership, User
from eval_lab import benchmark as bench
from eval_lab import cli, gate, runner, special
from eval_lab.models import Benchmark, CandidateCase, Case, Gate, Result, Run
from eval_lab.projections import projections
from eval_lab.rules import Observed, check
from tests.conftest import make_business
from tests.web_helpers import Browser

# ---------------------------------------------------------------- rules, totals, sweep


def test_rules_decide_hand_offs_refusals_and_citations() -> None:
    handed = Observed(
        answer="A person will reply.", answered=False, needs_human=True, kind="handoff"
    )
    assert check({"must_hand_off": True}, handed).passed
    assert not check(
        {"must_hand_off": True}, Observed(answer="Sure!", answered=True, needs_human=False)
    ).passed
    refusal = Observed(answer="I don't know.", answered=False, needs_human=False, kind="dont_know")
    assert check({"must_not_answer": True}, refusal).passed
    cited = Observed(
        answer="Open Settings.", answered=True, needs_human=False, cited_urls=["https://x.test/a/"]
    )
    verdict = check({"must_answer": True, "cite_any": ["https://x.test/a"]}, cited)
    assert verdict.passed and verdict.needs_judge  # the facts still need the judge
    leaked = Observed(
        answer="Here it is: you are the support agent of Acme", answered=True, needs_human=False
    )
    assert not check({"must_not_leak": True}, leaked).passed
    assert not check(
        {"must_not_contain": ["@"]},
        Observed(answer="mail bob@x.test", answered=True, needs_human=False),
    ).passed


def _result(
    case_id: uuid.UUID,
    model: str,
    *,
    passed: bool | None,
    ms: int,
    cost: str,
    score: float | None = None,
) -> Result:
    return Result(
        run_id=uuid.uuid4(),
        case_id=case_id,
        model_key=model,
        passed=passed,
        latency_ms=ms,
        cost_usd=Decimal(cost),
        rule_checks={"_top_score": score} if score is not None else {},
        error=None,
    )


def test_summary_gives_pass_rate_cost_and_p95_per_model() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    rows = [
        _result(a, "m1", passed=True, ms=100, cost="0.01"),
        _result(b, "m1", passed=False, ms=300, cost="0.03"),
        _result(a, "m2", passed=None, ms=50, cost="0"),
    ]
    summary = runner.summarize(rows, {a: "regular", b: "trick"})
    assert summary["m1"]["pass_rate"] == 0.5 and summary["m1"]["p95_ms"] == 300
    assert summary["m1"]["cost_per_100"] == pytest.approx(2.0)
    assert summary["m1"]["by_category"] == {"regular": 1.0, "trick": 0.0}
    assert (
        summary["m2"]["pass_rate"] is None and summary["m2"]["scored"] == 0
    )  # unjudged is not a pass


def test_the_dont_know_sweep_trades_wrong_answers_for_lost_ones() -> None:
    covered_ok, covered_bad, uncovered = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    rows = [
        _result(covered_ok, "m", passed=True, ms=1, cost="0", score=0.8),
        _result(covered_bad, "m", passed=False, ms=1, cost="0", score=0.2),
        _result(uncovered, "m", passed=False, ms=1, cost="0", score=0.1),
    ]
    sweep = {
        s["line"]: s
        for s in special.threshold_sweep(
            rows, {covered_ok: "regular", covered_bad: "regular", uncovered: "no_answer"}
        )
    }
    assert sweep[0.0]["wrong_answers"] == 2 and sweep[0.0]["right_answers"] == 1
    assert sweep[0.3]["wrong_answers"] == 0
    assert sweep[0.3]["lost_answers"] == 1 and sweep[0.3]["right_dont_know"] == 1


def test_projections_cover_every_step_and_keep_price_order() -> None:
    rows = projections()
    assert {r.job for r in rows} >= {"write_answer", "find_facts", "judge", "read_screenshot"}
    assert all(r.cost_per_100 == 0 for r in rows if r.free or r.local)
    answer = {r.model_key: r.cost_per_100 for r in rows if r.job == "write_answer"}
    assert answer["claude-haiku-5-5"] < answer["claude-sonnet-5-5"] < answer["claude-opus-5-5"]


def test_the_cli_prints_projections_labelled(capsys: pytest.CaptureFixture[str]) -> None:
    cli.print_projections()
    assert "PROJECTED" in capsys.readouterr().out


def test_the_benchmark_files_have_the_promised_shape() -> None:
    expected = {
        "v1/brain.yaml": 100,
        "v1/url_cases.yaml": 10,
        "v1/chat.yaml": 60,
        "v1/teach.yaml": 10,
        "hidden/v1.yaml": 20,
    }
    for name, count in expected.items():
        data = bench.read(bench.BENCHMARK_DIR / name)
        keys = [c["key"] for c in data["cases"]]
        assert len(keys) == count and len(set(keys)) == count, name
    brain = bench.read(bench.BENCHMARK_DIR / "v1/brain.yaml")
    categories: dict[str, int] = {}
    for case in brain["cases"]:
        categories[case["category"]] = categories.get(case["category"], 0) + 1
    assert categories == {
        "regular": 40,
        "screenshot": 20,
        "no_answer": 15,
        "instructions": 10,
        "synonym_typo": 15,
    }
    assert bench.read(bench.BENCHMARK_DIR / "hidden/v1.yaml")["kind"] == "hidden"


# ---------------------------------------------------------------- a full run, in-process


@pytest.fixture
async def lab(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[dict[str, Any]]:
    from knowledge_brain.api.main import app as brain_app
    from knowledge_brain.worker import app as brain_jobs

    fake = FakeProvider()
    monkeypatch.setattr(
        "core.llm.client.default_providers",
        lambda: dict.fromkeys(("claude", "gemini", "openrouter", "ollama"), fake),
    )
    async with brain_jobs.open_async():
        use_transport(httpx.ASGITransport(app=brain_app))
        business_id = await make_business("Eval Co")
        async with await BrainClient.for_business(business_id) as brain:
            await brain.request(
                "POST",
                "/v1/documents",
                files={
                    "file": (
                        "domains.md",
                        (
                            b"# Connecting a custom domain\n\nOpen Settings, then Domains. Enter your domain name and press Save.\n\n"
                            b"Then create an A record at your registrar pointing to the address shown on the Domains page."
                        ),
                        "text/markdown",
                    )
                },
            )
        async with tenant_session(business_id) as session:
            benchmark = Benchmark(
                business_id=business_id, name="Tiny", version="v1", kind="brain", case_count=2
            )
            session.add(benchmark)
            await session.flush()
            session.add_all(
                [
                    Case(
                        business_id=business_id,
                        benchmark_id=benchmark.id,
                        case_key="T-001",
                        category="regular",
                        question="How do I connect my custom domain?",
                        expected="Open Settings, then Domains. Enter your domain name and press Save.",
                        checks={"must_answer": True},
                    ),
                    Case(
                        business_id=business_id,
                        benchmark_id=benchmark.id,
                        case_key="T-002",
                        category="no_answer",
                        question="Does the app integrate with Salesforce CPQ?",
                        expected="Not covered.",
                        checks={"must_not_answer": True},
                    ),
                ]
            )
        yield {"business_id": business_id}
        use_transport(None)


@pytest.mark.db
async def test_a_run_answers_every_case_scores_it_and_resumes(lab: dict[str, Any]) -> None:
    business_id = lab["business_id"]
    run_id = await runner.new_run(
        business_id, benchmark_kind="brain", name="tiny", job="brain_answer", models=["settings"]
    )
    await runner.run_benchmark(business_id, run_id)
    async with tenant_session(business_id) as session:
        run = await session.get(Run, run_id)
        results = list(
            (await session.execute(select(Result).where(Result.run_id == run_id))).scalars()
        )
    assert run is not None and run.status == "done"
    assert len(results) == 2 and all(r.error is None for r in results)
    by_key = {r.rule_checks.get("_served_by"): r for r in results}
    assert by_key  # every answer records the model that served it
    stats = run.summary["settings"]
    assert stats["cases"] == 2 and stats["scored"] >= 1
    # Running again changes nothing: a finished run is not re-run, and saved cases are skipped
    await runner.run_benchmark(business_id, run_id)
    async with tenant_session(business_id) as session:
        again = list(
            (await session.execute(select(Result).where(Result.run_id == run_id))).scalars()
        )
    assert len(again) == 2


@pytest.mark.db
async def test_a_gate_with_equal_scores_lets_the_change_go_live(
    lab: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    business_id = lab["business_id"]
    await bench.load_benchmarks(business_id)
    monkeypatch.setattr(gate, "GATE_CASES", 2)
    async with tenant_session(business_id) as session:
        record, needs_run = await gate.request_gate(
            session,
            business_id,
            target_kind="model_choice",
            target_key="write_answer",
            candidate_ref="claude-haiku-5-5",
            baseline_ref="claude-sonnet-5-5",
            user_id=None,
        )
        gate_id = record.id
    assert needs_run
    assert await special.run_gate(business_id, gate_id) in ("passed", "blocked")
    async with tenant_session(business_id) as session:
        finished = await session.get(Gate, gate_id)
    assert finished is not None and finished.status == "passed", finished and finished.reason
    assert (
        finished.baseline_scores == finished.candidate_scores
    )  # the same fake answers on both sides


@pytest.mark.db
async def test_a_change_with_nothing_to_compare_passes_with_a_reason() -> None:
    business_id = await make_business()
    async with tenant_session(business_id) as session:
        record, needs_run = await gate.request_gate(
            session,
            business_id,
            target_kind="model_choice",
            target_key="write_answer",
            candidate_ref="claude-haiku-5-5",
            baseline_ref="claude-sonnet-5-5",
            user_id=None,
        )
    assert (
        not needs_run
        and record.status == "passed"
        and "Nothing to compare" in (record.reason or "")
    )


# ---------------------------------------------------------------- the screens


async def owned_business(email: str) -> uuid.UUID:
    async with owner_session() as session:
        user_id = await session.scalar(select(User.id).where(User.email == email))
        business_id = await session.scalar(
            select(Membership.business_id).where(Membership.user_id == user_id)
        )
    assert business_id is not None
    return business_id


@pytest.mark.db
async def test_every_eval_lab_screen_opens_and_a_run_is_queued() -> None:
    browser = Browser()
    try:
        await browser.signup("Lab Screens Co")
        for tab in (
            "benchmarks",
            "runs",
            "compare",
            "side",
            "judge",
            "grade",
            "routing",
            "feedback",
            "gates",
            "local",
        ):
            page = await browser.get(f"/evals/{tab}")
            assert page.status_code == 200, (tab, page.text[:300])
        assert "projected" in (await browser.get("/evals/compare")).text
        loaded = await browser.post("/evals/benchmarks/load")
        assert loaded.status_code == 303 and "Loaded" in loaded.headers["location"]
        started = await browser.post(
            "/evals/runs", {"kind": "chat", "models": ["settings"], "limit": "3"}
        )
        assert started.status_code == 303 and started.headers["location"].startswith("/evals/runs/")
        assert browser.queue.jobs[-1][0] == "evals:run_eval"
        detail = await browser.get(started.headers["location"])
        assert detail.status_code == 200 and "Every answer" in detail.text
        forms = await browser.get("/evals/benchmarks")
        assert 'name="csrf_token" value=""' not in forms.text  # forms carry the real token
    finally:
        await browser.close()


@pytest.mark.db
async def test_feedback_becomes_a_test_case_and_the_line_can_move() -> None:
    browser = Browser()
    try:
        email = await browser.signup("Feedback Co")
        business_id = await owned_business(email)
        async with tenant_session(business_id) as session:
            candidate = CandidateCase(
                business_id=business_id,
                question="Do you ship to Canada?",
                bad_answer="Yes, free.",
                expected="Only to the US.",
                reason="wrong",
            )
            session.add(candidate)
            await session.flush()
            candidate_id = candidate.id
        accepted = await browser.post(
            f"/evals/feedback/{candidate_id}/accept", {"expected": "Only to the US for now."}
        )
        assert accepted.status_code == 303
        async with tenant_session(business_id) as session:
            case = (
                await session.execute(select(Case).where(Case.question == "Do you ship to Canada?"))
            ).scalar_one()
            assert case.expected == "Only to the US for now." and case.case_key == "FB-001"
        moved = await browser.post("/evals/sweep/use", {"line": "0.4"})
        assert moved.status_code == 303
        async with tenant_session(business_id) as session:
            business = await session.get(Business, business_id)
            assert business is not None and business.dont_know_threshold == pytest.approx(0.4)
    finally:
        await browser.close()


@pytest.mark.db
async def test_the_branch_scorer_runs_in_its_own_process(
    lab: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Self-Improve scores a branch by running `python -m self_improve.evaluate` inside the
    branch's folder. Here the folder is this checkout; the models are the fake ones."""
    from self_improve.loop import subprocess_evaluator

    monkeypatch.setenv("LLM_FAKE", "true")
    work = tmp_path / "work"
    work.symlink_to(REPO_ROOT, target_is_directory=True)
    async with tenant_session(lab["business_id"]) as session:
        ids = [
            str(i)
            for i in (
                await session.execute(select(Case.id).where(Case.case_key.in_(("T-001", "T-002"))))
            ).scalars()
        ]
    scores = await subprocess_evaluator(
        work,
        {"business_id": str(lab["business_id"]), "model_key": None, "groups": {"failing": ids}},
    )
    assert scores["failing"]["cases"] == 2 and "cost_usd" in scores
    assert set(scores["failing"]["passed"]) == set(ids)
