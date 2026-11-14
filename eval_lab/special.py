"""The Eval Lab's special runs: each answers one requirement with numbers.

- retrieval_check  Task 2.7: keywords + meaning + re-rank vs meaning only (free with keywords)
- url_cases        Step 4: the 10 URL cases on the fixture site (free: no model)
- teach_tests      Task 3.5: the same question before and after an approved lesson
- lesson_check     Task 3.5 for one lesson, from the Lessons screen
- caching_proof    Task 1.3: the same answers with caching off, then on
- router_proof     Task 4.3: the router against always-Sonnet, same cases
- threshold_sweep  Task 3.2: the "I don't know" line, from one run's scores, no new calls
- run_gate         Task 4.5: the candidate against the live version on the gate cases
"""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import select

from ask_teach_chat import pipeline, teach
from ask_teach_chat.models import Lesson
from ask_teach_chat.preview import default_role
from core.brain_api import SearchMode, SearchRequest
from core.brain_client import BrainClient, BrainError
from core.db import tenant_session, user_session
from core.llm.providers.base import jdict, jlist
from core.llm.types import ChatTurn, LlmError
from core.models import PromptVersion, RoleVersion, User
from eval_lab import gate as gating
from eval_lab import runner
from eval_lab.models import Benchmark, Case, Gate, Result, Run
from workspace.service import create_business

# ---------------------------------------------------------------- Task 2.7: retrieval


def _hit_rank(case: Case, urls: list[str | None]) -> int | None:
    wanted = {u.rstrip("/").lower() for u in case.expected_sources}
    for rank, url in enumerate(urls, start=1):
        if url and url.rstrip("/").lower() in wanted:
            return rank
    return None


async def retrieval_check(business_id: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
    """Does the right page come back? hit@1 and hit@5 per search mode. No model is called
    (the meaning-only and hybrid modes embed the question with Voyage when its key is set)."""
    async with tenant_session(business_id) as session:
        run = await session.get(Run, run_id)
        assert run is not None
        cases = [
            c
            for c in (
                await session.execute(select(Case).where(Case.benchmark_id == run.benchmark_id))
            ).scalars()
            if c.category in ("regular", "screenshot", "synonym_typo") and c.expected_sources
        ]
    modes: list[tuple[SearchMode, bool]] = [("keyword", False), ("hybrid", True), ("vector", False)]
    async with await BrainClient.for_business(business_id) as brain:
        for case in cases:
            for mode, rerank in modes:
                label = f"{mode}+rerank" if rerank else mode
                found = await brain.search(
                    SearchRequest(query=case.question, limit=10, mode=mode, rerank=rerank)
                )
                rank = _hit_rank(case, [hit.url for hit in found.hits])
                async with tenant_session(business_id) as session:
                    session.add(
                        Result(
                            business_id=business_id,
                            run_id=run_id,
                            case_id=case.id,
                            model_key=label,
                            passed=rank is not None and rank <= 5,
                            latency_ms=found.took_ms,
                            rule_checks={
                                "rank": rank,
                                "hit1": rank == 1,
                                "hits": len(found.hits),
                                "corrected": found.corrected_query,
                            },
                        )
                    )
    summary = await runner.finish(business_id, run_id)
    async with tenant_session(business_id) as session:
        rows = list(
            (await session.execute(select(Result).where(Result.run_id == run_id))).scalars()
        )
    for label in summary:
        mine = [r for r in rows if r.model_key == label]
        summary[label]["hit1"] = (
            round(sum(1 for r in mine if r.rule_checks.get("hit1")) / len(mine), 4)
            if mine
            else None
        )
        summary[label]["searched"] = sum(1 for r in mine if r.rule_checks.get("hits"))
    async with tenant_session(business_id) as session:
        run = await session.get(Run, run_id)
        assert run is not None
        run.summary = summary
    return summary


# ---------------------------------------------------------------- Step 4: the 10 URL cases


FINISHED = ("done", "failed", "stopped")


async def _wait(
    brain: BrainClient, source_id: str, *, after_run: str | None = None, wait_seconds: float = 180
) -> dict[str, Any]:
    """Wait until a read is finished. With `after_run`, it must be a newer run than that one:
    a recheck starts its run a moment after it is asked for, and until then the progress shows
    the previous run, already "done"."""
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        progress: dict[str, Any] = await brain.get(f"/v1/sources/{source_id}/progress")
        newer = after_run is None or str(progress.get("run_id")) != after_run
        if newer and progress.get("status") in FINISHED:
            return progress
        if asyncio.get_running_loop().time() > deadline:
            return progress
        await asyncio.sleep(1.5)


async def url_cases(
    business_id: uuid.UUID, run_id: uuid.UUID, *, site: str, user_id: uuid.UUID
) -> dict[str, Any]:
    """The URL cases run against the fixture site in a throwaway business (deleted afterwards),
    through the Brain's API like everything else. Results land in this business's run."""
    temp_id = uuid.uuid4()
    async with user_session(user_id) as session:
        user = await session.get(User, user_id)
    assert user is not None
    async with tenant_session(temp_id, user_id=user_id) as session:
        await create_business(session, business_id=temp_id, owner=user, name="URL test (temporary)")
    observed: dict[str, dict[str, Any]] = {}
    try:
        async with (
            httpx.AsyncClient(base_url=site, timeout=30) as control,
            await BrainClient.for_business(temp_id) as brain,
        ):

            async def day(n: int) -> None:
                await control.post(f"/_control/day/{n}")

            async def page(path: str) -> dict[str, Any]:
                listing = jdict(await brain.get(f"/v1/sources/{source_id}/pages", q=path, limit=50))
                pages = [jdict(p) for p in jlist(listing.get("pages"))]
                found = next((p for p in pages if str(p.get("url", "")).endswith(path)), None)
                return found if found is not None else {}

            async def recheck() -> None:
                before = await brain.get(f"/v1/sources/{source_id}/progress")
                await brain.post(f"/v1/sources/{source_id}/recheck")
                await _wait(brain, source_id, after_run=str(before.get("run_id")))

            await day(1)
            plan = await brain.post(
                "/v1/sources/sitemaps",
                {"urls": [f"{site}/sitemap_index.xml"], "miss_threshold": 2, "rate": 5},
            )
            source_id = str(plan["source"]["id"])
            observed["UR-001"] = {"sitemap_count": plan["plan"]["sitemap_count"]}
            observed["UR-002"] = {"duplicates": [d["url"] for d in plan["plan"]["duplicates"]]}
            await brain.post(f"/v1/sources/{source_id}/start", {})
            progress = await _wait(brain, source_id)
            observed["UR-003"] = {"fetched": progress.get("done")}
            await day(2)
            await recheck()
            for key, path in (
                ("UR-004", "/articles/3"),
                ("UR-005", "/articles/4"),
                ("UR-006", "/articles/5"),
            ):
                observed[key] = await page(path)
            await day(3)
            await recheck()
            pending = await brain.get("/v1/health/pending")
            observed["UR-007"] = {"pending": [p["url"] for p in pending]}
            target = next((p["id"] for p in pending if str(p["url"]).endswith("/articles/3")), None)
            if target:
                await brain.post("/v1/health/apply", {"page_ids": [str(target)]})
            hits = (
                await brain.post(
                    "/v1/search", {"query": "export the member list csv", "mode": "keyword"}
                )
            )["hits"]
            observed["UR-008"] = {
                **(await page("/articles/3")),
                "found_in_search": any(str(h.get("url", "")).endswith("/articles/3") for h in hits),
            }
            await day(4)
            await recheck()
            observed["UR-009"] = await page("/articles/3")
            observed["UR-010"] = await page("/articles/5")
            await day(1)
    finally:
        async with tenant_session(temp_id, user_id=user_id) as session:
            from core.models import Business

            business = await session.get(Business, temp_id)
            if business is not None:
                await session.delete(business)
    return await _score_url_cases(business_id, run_id, observed)


async def _score_url_cases(
    business_id: uuid.UUID, run_id: uuid.UUID, observed: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    async with tenant_session(business_id) as session:
        run = await session.get(Run, run_id)
        assert run is not None
        cases = list(
            (
                await session.execute(select(Case).where(Case.benchmark_id == run.benchmark_id))
            ).scalars()
        )
        for case in cases:
            got = observed.get(case.case_key, {})
            expect: dict[str, Any] = dict(case.checks.get("expect", {}))
            checks: dict[str, bool] = {}
            if "sitemap_count" in expect:
                checks["sitemap_count"] = got.get("sitemap_count") == expect["sitemap_count"]
            if "duplicate" in expect:
                checks["duplicate"] = any(
                    str(u).endswith(expect["duplicate"]) for u in got.get("duplicates", [])
                )
            if "fetched" in expect:
                checks["fetched"] = got.get("fetched") == expect["fetched"]
            if "level" in expect:
                checks["level"] = got.get("level") == expect["level"]
            if "count" in expect:
                checks["count"] = got.get("miss_count") == expect["count"]
            if "pending" in expect:
                checks["pending"] = all(
                    any(str(u).endswith(p) for u in got.get("pending", []))
                    for p in expect["pending"]
                )
            if "not_searchable" in expect:
                checks["not_in_search"] = got.get("found_in_search") is False
            if "moved_to" in expect:
                checks["moved"] = got.get("level") == "quarantined"
            session.add(
                Result(
                    business_id=business_id,
                    run_id=run_id,
                    case_id=case.id,
                    model_key="no model",
                    passed=bool(checks) and all(checks.values()),
                    rule_checks={**checks, "_observed": got},
                    answer=str(got)[:2000],
                )
            )
    return await runner.finish(business_id, run_id)


# ---------------------------------------------------------------- Task 3.5: teaching


async def teach_tests(
    business_id: uuid.UUID, run_id: uuid.UUID, user_id: uuid.UUID | None
) -> dict[str, Any]:
    async with tenant_session(business_id) as session:
        run = await session.get(Run, run_id)
        assert run is not None
        cases = list(
            (
                await session.execute(
                    select(Case)
                    .where(Case.benchmark_id == run.benchmark_id)
                    .order_by(Case.case_key)
                )
            ).scalars()
        )
    role = await default_role(business_id)
    better = 0
    for case in cases:
        before = await runner.answer_case(
            business_id, case, job="chat_answer", model_key=None, role=role
        )
        before_judge = await _judge(business_id, case, before)
        correction = str((case.teach or {}).get("correction", case.expected))
        lesson_id: uuid.UUID | None = None
        try:
            _, lesson = await teach.teach_turn(
                business_id,
                chat_id=uuid.uuid4(),
                user_id=user_id,
                history=[
                    ChatTurn("user", case.question),
                    ChatTurn("assistant", before.seen.answer),
                    ChatTurn("user", correction),
                ],
            )
            lesson_id = lesson.id if lesson else None
        except LlmError:
            lesson = None
        if lesson_id is None:  # the writer didn't understand: a lesson straight from the correction
            async with tenant_session(business_id) as session:
                made = await teach.save_lesson(
                    session,
                    business_id,
                    topic=f"teach-test-{case.case_key}",
                    title=case.question,
                    rule=correction,
                    markdown=teach.lesson_markdown(
                        case.question, correction, case.question, case.expected
                    ),
                    confidence=1.0,
                    taught_by=user_id,
                    chat_id=None,
                    answer_id=None,
                    example={"question": case.question, "right_answer": case.expected},
                )
                lesson_id = made.id
        async with tenant_session(business_id) as session:
            lesson_row = await session.get(Lesson, lesson_id)
            assert lesson_row is not None
            await teach.approve(session, lesson_row, user_id)
        after = await runner.answer_case(
            business_id, case, job="chat_answer", model_key=None, role=role
        )
        after_judge = await _judge(business_id, case, after)
        b, a = (before_judge or 0.0), (after_judge or 0.0)
        rules_after = all(
            t.lower() in after.seen.answer.lower()
            for t in case.checks.get("after_must_contain", [])
        )
        improved = a > b or (
            rules_after
            and not all(
                t.lower() in before.seen.answer.lower()
                for t in case.checks.get("after_must_contain", [])
            )
        )
        better += improved
        async with tenant_session(business_id) as session:
            session.add(
                Result(
                    business_id=business_id,
                    run_id=run_id,
                    case_id=case.id,
                    model_key="before",
                    answer=before.seen.answer,
                    judge_score=before_judge,
                    passed=None,
                    cost_usd=before.cost,
                    latency_ms=before.latency_ms,
                )
            )
            session.add(
                Result(
                    business_id=business_id,
                    run_id=run_id,
                    case_id=case.id,
                    model_key="after",
                    answer=after.seen.answer,
                    judge_score=after_judge,
                    passed=improved,
                    rule_checks={
                        "improved": improved,
                        "contains_rule": rules_after,
                        "lesson_id": str(lesson_id),
                    },
                    cost_usd=after.cost,
                    latency_ms=after.latency_ms,
                )
            )
            lesson_row = await session.get(Lesson, lesson_id)
            if (
                lesson_row is not None
            ):  # test lessons are removed so the business's lessons stay real
                await teach.delete(session, lesson_row)
    summary = await runner.finish(business_id, run_id)
    summary["teach"] = {"cases": len(cases), "better_after": better, "bar": "at least 8 of 10"}
    async with tenant_session(business_id) as session:
        run = await session.get(Run, run_id)
        assert run is not None
        run.summary = summary
    return summary


async def _judge(business_id: uuid.UUID, case: Case, done: runner.Answered) -> float | None:
    from eval_lab.judge import judge

    result = await judge(
        business_id,
        question=case.question,
        expected=case.expected,
        answer=done.seen.answer,
        answered_by=done.model_key,
        trace_id=done.trace_id,
    )
    return result.score if result else None


async def lesson_check(
    business_id: uuid.UUID, lesson_id: uuid.UUID, user_id: uuid.UUID | None
) -> uuid.UUID:
    """One lesson's question, without and with lessons, judged against the lesson's right answer."""
    async with tenant_session(business_id) as session:
        lesson = await session.get(Lesson, lesson_id)
        assert lesson is not None
        version = await teach.latest_version(session, lesson_id)
        benchmark = (
            (await session.execute(select(Benchmark).where(Benchmark.kind == "teach")))
            .scalars()
            .first()
        )
        question = str(version.example.get("question") or lesson.title)
        expected = str(version.example.get("right_answer") or version.rule)
    if benchmark is None:
        raise LookupError("Load the teach benchmark first")
    run_id = await runner.new_run(
        business_id,
        benchmark_kind="teach",
        name=f"Lesson check: {lesson.title}",
        job="chat_answer",
        models=["before", "after"],
        mode="lesson_check",
        user_id=user_id,
    )
    async with tenant_session(business_id) as session:
        case = Case(
            business_id=business_id,
            benchmark_id=benchmark.id,
            case_key=f"LESSON-{str(lesson_id)[:8]}",
            category="teach",
            question=question,
            expected=expected,
            checks={},
        )
        existing = (
            await session.execute(select(Case).where(Case.case_key == case.case_key))
        ).scalar_one_or_none()
        if existing is None:
            session.add(case)
            await session.flush()
        else:
            case = existing
    role = await default_role(business_id)
    for label, include in (("before", False), ("after", True)):
        done = await runner.answer_case(
            business_id,
            case,
            job="chat_answer",
            model_key=None,
            role=role,
            overrides=pipeline.Overrides(include_lessons=include),
        )
        score = await _judge(business_id, case, done)
        async with tenant_session(business_id) as session:
            session.add(
                Result(
                    business_id=business_id,
                    run_id=run_id,
                    case_id=case.id,
                    model_key=label,
                    answer=done.seen.answer,
                    judge_score=score,
                    passed=None if score is None else score >= 0.7,
                    cost_usd=done.cost,
                    latency_ms=done.latency_ms,
                    trace_id=done.trace_id,
                )
            )
    await runner.finish(business_id, run_id)
    async with tenant_session(business_id) as session:  # the lesson file now shows the scores
        await teach.write_topic_file(session, business_id, lesson.topic)
    return run_id


# ---------------------------------------------------------------- Tasks 1.3 and 4.3: proofs


async def paired_runs(
    business_id: uuid.UUID,
    *,
    name: str,
    a: tuple[str, dict[str, Any], list[str]],
    b: tuple[str, dict[str, Any], list[str]],
    limit: int,
    user_id: uuid.UUID | None,
) -> dict[str, Any]:
    """Two runs on the same chat cases, differing in one thing; totals side by side."""
    results: dict[str, Any] = {}
    for mode, overrides, models in (a, b):
        run_id = await runner.new_run(
            business_id,
            benchmark_kind="chat",
            name=f"{name}: {mode}",
            job="chat_answer",
            models=models,
            mode=mode,
            user_id=user_id,
            params={"categories": ["regular"], "limit": limit, "overrides": overrides},
        )
        await runner.run_benchmark(business_id, run_id)
        async with tenant_session(business_id) as session:
            run = await session.get(Run, run_id)
            assert run is not None
            results[mode] = {
                "run_id": str(run_id),
                "cost": float(run.total_cost_usd),
                "summary": run.summary,
            }
    first, second = results[a[0]]["cost"], results[b[0]]["cost"]
    results["saved_percent"] = round(100 * (first - second) / first, 1) if first else None
    return results


async def caching_proof(
    business_id: uuid.UUID, tasks: int, user_id: uuid.UUID | None
) -> dict[str, Any]:
    return await paired_runs(
        business_id,
        name="Caching proof",
        limit=tasks,
        user_id=user_id,
        a=("cache_off", {"caching": False}, ["settings"]),
        b=("cache_on", {"caching": True}, ["settings"]),
    )


async def router_proof(
    business_id: uuid.UUID, cases: int, user_id: uuid.UUID | None
) -> dict[str, Any]:
    return await paired_runs(
        business_id,
        name="Router proof",
        limit=cases,
        user_id=user_id,
        a=("always_sonnet", {}, ["claude-sonnet-5-5"]),
        b=("router", {}, ["router"]),
    )


def threshold_sweep(
    results: list[Result], categories: dict[uuid.UUID, str]
) -> list[dict[str, Any]]:
    """Task 3.2: from one run's results, what each "I don't know" line would have done. An answer
    below the line becomes "I don't know": right for a question the knowledge doesn't cover,
    a lost answer for one it does. No new model calls."""
    rows: list[dict[str, Any]] = []
    for step in range(0, 9):
        line = round(0.1 * step, 1)
        wrong = lost = correct_refusals = answered_right = 0
        for result in results:
            score = result.rule_checks.get("_top_score")
            if score is None or result.passed is None:
                continue
            uncovered = categories.get(result.case_id) in ("no_answer", "not_in_kb")
            if float(score) < line:
                correct_refusals += uncovered
                lost += not uncovered
            elif uncovered:
                wrong += not result.passed
            else:
                answered_right += bool(result.passed)
                wrong += not result.passed
        total = wrong + lost + correct_refusals + answered_right
        rows.append(
            {
                "line": line,
                "wrong_answers": wrong,
                "lost_answers": lost,
                "right_answers": answered_right,
                "right_dont_know": correct_refusals,
                "wrong_rate": round(wrong / total, 4) if total else None,
            }
        )
    return rows


# ---------------------------------------------------------------- Task 4.5: the release gate


async def run_gate(business_id: uuid.UUID, gate_id: uuid.UUID) -> str:
    async with tenant_session(business_id) as session:
        gate = await session.get(Gate, gate_id)
        if gate is None or gate.status != "running":
            return "skipped"
        case_ids = await gating.gate_case_ids(session, gate.target_kind, gate.target_key)
        cases = list((await session.execute(select(Case).where(Case.id.in_(case_ids)))).scalars())
        categories = {c.id: c.category for c in cases}
        kind, key, candidate, baseline = (
            gate.target_kind,
            gate.target_key,
            gate.candidate_ref,
            gate.baseline_ref,
        )
        candidate_body = baseline_role = candidate_role = None
        if kind == "prompt":
            version = await session.get(PromptVersion, uuid.UUID(candidate))
            candidate_body = version.body if version else None
        if kind == "role":
            new = await session.get(RoleVersion, uuid.UUID(candidate))
            old = await session.get(RoleVersion, uuid.UUID(baseline)) if baseline else None
            from core.models import Role

            role_row = await session.get(Role, new.role_id) if new else None
            name = role_row.name if role_row else "Role"
            candidate_role = pipeline.RoleSettings.from_version(name, new) if new else None
            baseline_role = pipeline.RoleSettings.from_version(name, old) if old else None

    async def score(side: str) -> dict[str, float]:
        passed: dict[str, list[bool]] = {}
        for case in cases:
            job, model, overrides, role, ask = "chat_answer", None, pipeline.Overrides(), None, None
            if kind == "prompt" and side == "candidate" and candidate_body is not None:
                if key.startswith("brain."):
                    job, ask = "brain_answer", {key: candidate_body}
                else:
                    overrides.prompts = {key: candidate_body}
            elif kind == "prompt" and key.startswith("brain."):
                job = "brain_answer"
            if kind == "role":
                role = candidate_role if side == "candidate" else baseline_role
            if kind == "skill":
                overrides.skill_version_id = (
                    uuid.UUID(candidate if side == "candidate" else baseline)
                    if (candidate if side == "candidate" else baseline)
                    else None
                )
            if kind == "model_choice" and key == "write_answer":
                model = candidate if side == "candidate" else baseline
            done = await runner.answer_case(
                business_id,
                case,
                job=job,
                model_key=model,
                overrides=overrides,
                role=role,
                ask_overrides=ask,
            )
            scored = await runner.score_case(business_id, case, done, second_opinion=False)
            if scored["passed"] is not None:
                passed.setdefault(case.category, []).append(bool(scored["passed"]))
        return {k: round(sum(v) / len(v), 4) for k, v in passed.items()}

    try:
        base_scores, new_scores = await score("baseline"), await score("candidate")
    except (LlmError, BrainError) as exc:
        async with tenant_session(business_id) as session:
            gate = await session.get(Gate, gate_id)
            assert gate is not None
            gate.status, gate.reason, gate.finished_at = (
                "error",
                f"The gate couldn't run: {exc}"[:500],
                datetime.now(UTC),
            )
        return "error"
    drops = [
        f"{cat}: {base_scores[cat]:.0%} -> {new_scores.get(cat, 0):.0%}"
        for cat in base_scores
        if new_scores.get(cat, 0) < base_scores[cat]
    ]
    async with tenant_session(business_id) as session:
        gate = await session.get(Gate, gate_id)
        assert gate is not None
        gate.baseline_scores, gate.candidate_scores = base_scores, new_scores
        if drops:
            await gating.block(
                session,
                gate,
                "A score dropped, so the change was not published: " + "; ".join(drops),
            )
        else:
            gate.status, gate.finished_at = "passed", datetime.now(UTC)
            gate.reason = f"No score dropped on {len(cases)} gate cases."
            await gating.activate(session, gate)
    if kind == "skill":
        async with await BrainClient.for_business(business_id) as brain:
            skills = await brain.get("/v1/skills")
            for skill in skills:
                detail = await brain.get(f"/v1/skills/{skill['id']}")
                if any(str(v["id"]) == candidate for v in detail["versions"]):
                    await brain.post(
                        f"/v1/skills/{skill['id']}/versions/{candidate}/status",
                        {"status": "blocked" if drops else "live"},
                    )
    _ = categories, Decimal
    return "blocked" if drops else "passed"
