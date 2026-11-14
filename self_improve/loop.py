"""Task 5.2: fix, test and try again, within limits.

    export the base -> [Opus edits -> protected files put back -> anti-cheat check ->
    unit tests -> the failing cases] x rounds -> when they all pass: the other cases and the
    hidden check set -> a branch, a report, and a human decides

The app, not Opus, runs every test and eval, so Opus can't skip or change them. "Fixed" means:
the unit tests pass, every given failing case passes, no category of the other cases drops,
and the hidden check set (which Opus never sees) doesn't drop. Two hard limits stop the run:
rounds and dollars. When one is hit, the report says what was tried.
"""

import json
import os
import shutil
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from core.config import get_settings
from core.db import tenant_session
from core.llm.providers.base import jdict
from core.prompts import live_prompt, render
from eval_lab.models import Benchmark, Case, Result
from self_improve import github, repo
from self_improve.agent import AgentRound, AgentRunner, ClaudeAgentRunner, FakeAgentRunner
from self_improve.evaluate import edited_prompts
from self_improve.models import ImproveRound, ImproveRun
from self_improve.packer import build_packet, failing_cases_text, related_files_text
from workspace.connections import credentials_of, get_connection

Evaluator = Callable[[Path, dict[str, Any]], Awaitable[dict[str, Any]]]
TestRunner = Callable[[Path], Awaitable[tuple[bool, str]]]
AGENT_SHARE = Decimal(
    "0.6"
)  # of the money left, what a round of Opus may spend; the rest is for evals


class LoopError(RuntimeError):
    pass


async def subprocess_evaluator(work: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Score cases with the work folder's code, in a separate Python process."""
    spec_path, out_path = work.parent / "eval-spec.json", work.parent / "eval-scores.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    out_path.unlink(missing_ok=True)
    done = await repo.run(
        [
            sys.executable,
            "-m",
            "self_improve.evaluate",
            "--spec",
            str(spec_path),
            "--out",
            str(out_path),
        ],
        work,
        env={**os.environ, "PYTHONPATH": str(work)},
        limit_seconds=3600,
    )
    if done.code != 0 or not out_path.exists():
        raise LoopError(
            "The evaluation failed: " + (done.err or done.out).decode("utf-8", "replace")[-800:]
        )
    scores: dict[str, Any] = json.loads(out_path.read_text(encoding="utf-8"))
    return scores


async def run_unit_tests(work: Path) -> tuple[bool, str]:
    """The work folder's unit tests. The database is made unreachable, so tests that need it
    skip instead of touching real data."""
    env = {**os.environ, "PYTHONPATH": str(work), "DB_HOST": "unreachable.invalid"}
    done = await repo.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
        work,
        env=env,
        limit_seconds=900,
    )
    output = (done.out + done.err).decode("utf-8", "replace")
    return done.code in (0, 5), output[-1500:]  # 5 = no tests collected


@dataclass
class Tools:
    agent: AgentRunner
    evaluate: Evaluator
    unit_tests: TestRunner


def default_tools() -> Tools:
    agent: AgentRunner = FakeAgentRunner() if get_settings().llm_fake else ClaudeAgentRunner()
    return Tools(agent, subprocess_evaluator, run_unit_tests)


# ---------------------------------------------------------------- starting a run


async def create_run(
    business_id: uuid.UUID,
    *,
    source_run_id: uuid.UUID,
    comment: str,
    max_rounds: int,
    max_cost: Decimal,
    user_id: uuid.UUID | None,
    result_ids: list[uuid.UUID] | None = None,
) -> uuid.UUID:
    packet = await build_packet(
        business_id, source_run_id=source_run_id, comment=comment, result_ids=result_ids
    )
    if not packet["failing"]:
        raise LookupError(
            "No failure in that run is worth sending to Opus: "
            + (
                "; ".join(f"{e['case_key']}: {e['reason']}" for e in packet["excluded"][:5])
                or "nothing failed"
            )
        )
    keys = ", ".join(f["case_key"] for f in packet["failing"][:4]) + (
        "…" if len(packet["failing"]) > 4 else ""
    )
    async with tenant_session(business_id) as session:
        run = ImproveRun(
            business_id=business_id,
            title=f"Fix {len(packet['failing'])} failing case(s): {keys}",
            comment=packet["comment"],
            packet=packet,
            max_rounds=max_rounds,
            max_cost_usd=max_cost,
            failing_result_ids=[uuid.UUID(f["result_id"]) for f in packet["failing"]],
            created_by=user_id,
        )
        session.add(run)
        await session.flush()
        return run.id


# ---------------------------------------------------------------- the loop


def _no_drop(before: dict[str, float], after: dict[str, float]) -> list[str]:
    return [
        f"{cat}: {rate:.0%} -> {after.get(cat, 0.0):.0%}"
        for cat, rate in before.items()
        if after.get(cat, 0.0) < rate
    ]


async def _baseline_others(
    business_id: uuid.UUID, packet: dict[str, Any]
) -> tuple[list[str], dict[str, float]]:
    """The other cases of the source eval run, with their pass rates there: free, already measured."""
    failing = {f["case_id"] for f in packet["failing"]}
    async with tenant_session(business_id) as session:
        rows = (
            await session.execute(
                select(Result, Case.category)
                .join(Case, Case.id == Result.case_id)
                .where(
                    Result.run_id == uuid.UUID(packet["source_run_id"]),
                    Result.model_key == packet["model_key"],
                    Result.passed.is_not(None),
                )
            )
        ).all()
    ids: list[str] = []
    by_category: dict[str, list[bool]] = {}
    for result, category in rows:
        if str(result.case_id) in failing:
            continue
        ids.append(str(result.case_id))
        by_category.setdefault(category, []).append(bool(result.passed))
    return ids, {k: round(sum(v) / len(v), 4) for k, v in by_category.items()}


async def _hidden_ids(business_id: uuid.UUID) -> list[str]:
    async with tenant_session(business_id) as session:
        benchmark = (
            await session.execute(
                select(Benchmark)
                .where(Benchmark.kind == "hidden")
                .order_by(Benchmark.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if benchmark is None:
            return []
        return [
            str(i)
            for i in (
                await session.execute(select(Case.id).where(Case.benchmark_id == benchmark.id))
            ).scalars()
        ]


async def _all_questions(business_id: uuid.UUID) -> list[str]:
    async with tenant_session(business_id) as session:
        return list((await session.execute(select(Case.question))).scalars())


def _feedback(
    n: int,
    tests_ok: bool,
    tests_out: str,
    failing: dict[str, Any],
    others_drop: list[str],
    others_failures: list[dict[str, Any]],
    hidden_dropped: bool,
    protected: list[str],
    cheats: list[str],
) -> str:
    lines = [f"Round {n} results:", f"- Unit tests: {'passed' if tests_ok else 'FAILED'}"]
    if not tests_ok:
        lines.append("```\n" + tests_out[-1200:] + "\n```")
    if protected:
        lines.append("- These edits were thrown away (protected paths): " + ", ".join(protected))
    if cheats:
        lines.append(
            "- Your change copied test questions into the app. That was rejected: fix the cause instead."
        )
    rate = failing.get("pass_rate")
    lines.append(
        f"- The given failing cases now pass: {rate:.0%}"
        if rate is not None
        else "- The failing cases couldn't be judged"
    )
    for f in failing.get("failures", [])[:8]:
        lines.append(
            f"  - {f['case_key']} still fails. Answer: {f['answer'][:300]} Why: {f['why']}"
        )
    if others_drop:
        lines.append("- Other cases got worse: " + "; ".join(others_drop))
        for f in others_failures[:5]:
            lines.append(f"  - {f['case_key']}: {f['question']} Why: {f['why']}")
    if hidden_dropped:
        lines.append(
            "- A check you can't see got worse: the change is probably too specific to these cases."
        )
    return "\n".join(lines)


async def run_loop(business_id: uuid.UUID, run_id: uuid.UUID, tools: Tools | None = None) -> str:
    tools = tools or default_tools()
    settings = get_settings()
    async with tenant_session(business_id) as session:
        run = await session.get(ImproveRun, run_id)
        if run is None or run.status != "queued":
            return "skipped"
        run.status = "running"
        packet, max_rounds, max_cost = dict(run.packet), run.max_rounds, Decimal(run.max_cost_usd)
        connection = await get_connection(session, "claude")
        api_key = credentials_of(connection, business_id).api_key if connection else None
        brief_prompt = await live_prompt(session, "improve.agent_brief")
    folder = settings.storage_dir / "improve" / str(run_id)
    work = folder / "work"
    try:
        if not api_key and not settings.llm_fake:
            raise LoopError("No Claude key in Settings, Keys: Opus can't run.")
        source = settings.self_improve_repo
        base_sha, base_branch = await repo.head(source), await repo.current_branch(source)
        shutil.rmtree(folder, ignore_errors=True)
        await repo.export(source, base_sha, work)
        prompts_dir = work / repo.INPUT_DIR / "prompts"
        prompts_dir.mkdir(parents=True)
        for key, body in packet["prompts"].items():
            (prompts_dir / f"{key}.md").write_text(body, encoding="utf-8")
        failing_ids = [f["case_id"] for f in packet["failing"]]
        other_ids, others_before = await _baseline_others(business_id, packet)
        hidden_ids = await _hidden_ids(business_id)
        questions = await _all_questions(business_id)
        cost = Decimal(0)
        hidden_before: float | None = None
        if hidden_ids:
            base_scores = await tools.evaluate(
                work,
                {
                    "business_id": str(business_id),
                    "model_key": packet["model_key"],
                    "groups": {"hidden": hidden_ids},
                },
            )
            cost += Decimal(str(base_scores.get("cost_usd", 0)))
            hidden_before = base_scores["hidden"]["pass_rate"]
        before = {
            "failing": 0.0,
            "failing_cases": len(failing_ids),
            "others": others_before,
            "others_cases": len(other_ids),
            "hidden": hidden_before,
            "hidden_cases": len(hidden_ids),
        }
        async with tenant_session(business_id) as session:
            saved = await session.get(ImproveRun, run_id)
            assert saved is not None
            saved.base_sha, saved.scores_before = base_sha, before
        status, stop_reason, last = "not_fixed", "rounds", ""
        after: dict[str, Any] = dict(before)
        violations: list[str] = []
        for n in range(1, max_rounds + 1):
            left = max_cost - cost
            if left <= Decimal("0.05"):
                stop_reason = "cost"
                break
            brief = render(
                brief_prompt.body,
                {
                    "failing_cases": failing_cases_text(packet),
                    "comment": packet["comment"],
                    "related_files": related_files_text(packet),
                    "round": str(n),
                    "max_rounds": str(max_rounds),
                    "last_results": last or "This is the first round.",
                },
            )
            done: AgentRound = await tools.agent.run_round(
                work,
                brief,
                budget_usd=(left * AGENT_SHARE).quantize(Decimal("0.01")),
                api_key=api_key or "",
            )
            cost += done.cost_usd
            protected = await repo.protect(work)
            violations += [p for p in protected if p not in violations]
            diff_text = await repo.patch(work)
            prompt_changes = {
                k: v for k, v in edited_prompts(work).items() if packet["prompts"].get(k) != v
            }
            cheats = repo.hardcoded_questions(
                diff_text
                + "\n".join(
                    "+" + line for body in prompt_changes.values() for line in body.splitlines()
                ),
                questions,
            )
            if cheats:
                violations.append(f"copied {len(cheats)} test question(s) into the change")
            changed = await repo.changed_files(work) + [
                f"{repo.INPUT_DIR}/prompts/{k}.md" for k in prompt_changes
            ]
            tests_ok, tests_out = await tools.unit_tests(work)
            spec = {"business_id": str(business_id), "model_key": packet["model_key"]}
            failing_scores: dict[str, Any] = (
                (await tools.evaluate(work, spec | {"groups": {"failing": failing_ids}}))
                if not cheats
                else {"failing": {"pass_rate": None, "failures": []}, "cost_usd": 0}
            )
            cost += Decimal(str(failing_scores.get("cost_usd", 0)))
            failing_now = jdict(failing_scores.get("failing"))
            round_scores: dict[str, Any] = {
                "failing": failing_now.get("pass_rate"),
                "tests_ok": tests_ok,
            }
            others_drop: list[str] = []
            others_failures: list[dict[str, Any]] = []
            hidden_dropped = False
            fixed = (
                tests_ok and not cheats and failing_now.get("pass_rate") == 1.0 and not done.error
            )
            if fixed and cost < max_cost:
                final = await tools.evaluate(
                    work, spec | {"groups": {"others": other_ids, "hidden": hidden_ids}}
                )
                cost += Decimal(str(final.get("cost_usd", 0)))
                others_after = final["others"].get("by_category", {})
                hidden_after = final["hidden"].get("pass_rate") if hidden_ids else None
                others_drop = _no_drop(others_before, others_after)
                others_failures = final["others"].get("failures", [])
                hidden_dropped = hidden_before is not None and (hidden_after or 0.0) < hidden_before
                round_scores |= {"others": others_after, "hidden": hidden_after}
                after = {
                    "failing": 1.0,
                    "failing_cases": len(failing_ids),
                    "others": others_after,
                    "others_cases": len(other_ids),
                    "hidden": hidden_after,
                    "hidden_cases": len(hidden_ids),
                }
                fixed = not others_drop and not hidden_dropped
            else:
                after = after | {"failing": failing_now.get("pass_rate")}
            async with tenant_session(business_id) as session:
                session.add(
                    ImproveRound(
                        business_id=business_id,
                        run_id=run_id,
                        n=n,
                        agent_summary=done.summary,
                        tool_calls=done.tool_calls,
                        changed_files=changed,
                        tests_passed=tests_ok,
                        scores=round_scores,
                        cost_usd=done.cost_usd,
                        finished_at=datetime.now(UTC),
                    )
                )
                saved = await session.get(ImproveRun, run_id)
                assert saved is not None
                saved.rounds_used, saved.cost_usd, saved.violations = n, cost, violations
            if fixed:
                status, stop_reason = "fixed", "target"
                break
            last = _feedback(
                n,
                tests_ok,
                tests_out,
                failing_now,
                others_drop,
                others_failures,
                hidden_dropped,
                protected,
                cheats,
            )
            if done.error:
                last += f"\n- Your session ended with an error: {done.error}"
            if cost >= max_cost:
                stop_reason = "cost"
                break
        await _finish(
            business_id,
            run_id,
            work,
            source,
            base_sha,
            base_branch,
            status,
            stop_reason,
            after,
            cost,
            violations,
        )
        return status
    except (LoopError, repo.GitError, OSError) as exc:
        async with tenant_session(business_id) as session:
            saved = await session.get(ImproveRun, run_id)
            assert saved is not None
            saved.status, saved.error, saved.finished_at = (
                "error",
                str(exc)[:1000],
                datetime.now(UTC),
            )
        return "error"
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def _finish(
    business_id: uuid.UUID,
    run_id: uuid.UUID,
    work: Path,
    source: Path,
    base_sha: str,
    base_branch: str,
    status: str,
    stop_reason: str,
    after: dict[str, Any],
    cost: Decimal,
    violations: list[str],
) -> None:
    async with tenant_session(business_id) as session:
        run = await session.get(ImproveRun, run_id)
        assert run is not None
        rounds = list(
            (
                await session.execute(
                    select(ImproveRound)
                    .where(ImproveRound.run_id == run_id)
                    .order_by(ImproveRound.n)
                )
            ).scalars()
        )
        packet = dict(run.packet)
    diff_text = await repo.patch(work)
    prompt_changes = {
        k: v for k, v in edited_prompts(work).items() if packet["prompts"].get(k) != v
    }
    changed = await repo.changed_files(work)
    branch = head_sha = pr_url = None
    pr_number: int | None = None
    if changed or prompt_changes:
        branch = f"improve/{datetime.now(UTC):%Y%m%d}-{str(run_id)[:8]}"
        message = f"Self-Improve: {packet['failing'][0]['case_key']} and others ({status.replace('_', ' ')})"
        head_sha = await repo.to_branch(
            source,
            base_sha,
            branch,
            diff_text,
            message,
            get_settings().storage_dir / "improve" / "worktrees",
        )
    packet["prompt_changes"] = prompt_changes
    report = report_markdown(
        run_id,
        packet,
        status,
        stop_reason,
        after,
        cost,
        violations,
        rounds,
        branch,
        changed,
        run.max_rounds,
        Decimal(run.max_cost_usd),
        run.scores_before,
    )
    if branch and status == "fixed" and github.configured():
        await github.push(source, branch)
        pr_number, pr_url = await github.open_pr(
            branch, base_branch, f"Self-Improve: {run.title}", report
        )
    reports = source / "reports" / "self-improve"
    reports.mkdir(parents=True, exist_ok=True)
    report_path = reports / f"{datetime.now(UTC):%Y-%m-%d}-{str(run_id)[:8]}.md"
    report_path.write_text(report, encoding="utf-8")
    async with tenant_session(business_id) as session:
        run = await session.get(ImproveRun, run_id)
        assert run is not None
        run.status, run.finished_at = status, datetime.now(UTC)
        run.scores_after, run.cost_usd, run.violations = after, cost, violations
        run.branch, run.head_sha, run.pr_url, run.pr_number = branch, head_sha, pr_url, pr_number
        run.changed_files = changed + [f"{repo.INPUT_DIR}/prompts/{k}.md" for k in prompt_changes]
        # The diff is kept with the run: the web service has no repo to read the branch from
        run.packet = packet | {
            "base_branch": base_branch,
            "stop_reason": stop_reason,
            "diff": diff_text[:200_000],
        }
        run.report_md, run.report_path = report, report_path.relative_to(source).as_posix()


def _rate(value: Any) -> str:
    return "-" if value is None else f"{float(value):.0%}"


def report_markdown(
    run_id: uuid.UUID,
    packet: dict[str, Any],
    status: str,
    stop_reason: str,
    after: dict[str, Any],
    cost: Decimal,
    violations: list[str],
    rounds: list[ImproveRound],
    branch: str | None,
    changed: list[str],
    max_rounds: int,
    max_cost: Decimal,
    before: dict[str, Any],
) -> str:
    verdict = (
        {
            "fixed": "Fixed: every given case passes, nothing else dropped. Waiting for a human to approve.",
            "not_fixed": {
                "cost": "Not fixed: the $ limit was reached.",
                "rounds": "Not fixed: the round limit was reached.",
            }.get(stop_reason, "Not fixed."),
        }[status]
        if status in ("fixed", "not_fixed")
        else status
    )
    others_before, others_after = jdict(before.get("others")), jdict(after.get("others"))
    lines = [
        f"# Self-Improve run {str(run_id)[:8]}",
        "",
        f"**{verdict}**",
        "",
        f"Model under test: {packet.get('model_key')} · job: {packet.get('job')} · source eval run: {packet.get('source_run_id')}",
        "",
        "## What failed",
        "",
    ]
    lines += [
        f"- **{f['case_key']}** ({f['category'].replace('_', ' ')}): {f['question']}  \n  Why: {f['why'] or 'not given'}"
        for f in packet["failing"]
    ]
    if packet.get("excluded"):
        lines += ["", "Left for a person (not sent to Opus):"]
        lines += [f"- {e['case_key']}: {e['reason']}" for e in packet["excluded"]]
    lines += [
        "",
        "## The comment sent with them",
        "",
        packet.get("comment") or "(none)",
        "",
        "## What Opus changed",
        "",
    ]
    lines += [f"- `{path}`" for path in changed] or ["- Nothing in the code."]
    lines += [
        f"- Prompt `{key}`: a new version (becomes a version in Settings when approved)"
        for key in packet.get("prompt_changes", {})
    ]
    if branch:
        lines.append(f"\nBranch: `{branch}`")
    lines += [
        "",
        "## Scores before and after",
        "",
        "| Cases | Before | After |",
        "| --- | --- | --- |",
        f"| The {before.get('failing_cases', 0)} given failing cases | {_rate(before.get('failing'))} | {_rate(after.get('failing'))} |",
    ]
    for category in sorted(set(others_before) | set(others_after)):
        lines.append(
            f"| Other cases: {category.replace('_', ' ')} | {_rate(others_before.get(category))} | {_rate(others_after.get(category)) if status == 'fixed' else 'not run'} |"
        )
    lines.append(
        f"| Hidden check set ({before.get('hidden_cases', 0)} cases, never shown to Opus) | {_rate(before.get('hidden'))} | {_rate(after.get('hidden')) if status == 'fixed' else 'not run'} |"
    )
    lines += [
        "",
        "## Cost and rounds",
        "",
        f"- Cost: ${cost:.2f} of the ${max_cost:.2f} limit (Opus and the evals together)",
        f"- Rounds: {len(rounds)} of {max_rounds}",
        "",
    ]
    lines += ["## Thrown away", ""] + (
        [f"- {v}" for v in violations] or ["- Nothing: no protected path was touched."]
    )
    lines += ["", "## Round by round", ""]
    for r in rounds:
        denied = sum(1 for call in r.tool_calls if call.get("denied"))
        lines += [
            f"### Round {r.n}",
            "",
            f"Tests {'passed' if r.tests_passed else 'failed'} · failing cases passing: {_rate(r.scores.get('failing'))} · "
            f"${Decimal(r.cost_usd):.2f} · {len(r.tool_calls)} tool calls ({denied} denied)",
            "",
            (r.agent_summary or "(no summary)").strip(),
            "",
        ]
    return "\n".join(lines) + "\n"
