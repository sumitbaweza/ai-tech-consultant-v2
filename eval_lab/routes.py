"""The Eval Lab (screen 11): benchmarks, runs, the 14-model comparison, side by side, the judge
check with blind grading, routing, feedback cases, release gates and local-only mode.

Every number on these screens says where it comes from: "measured" (a run in this app) or
"projected" (token counts x the price list, before any run). Quality is never projected.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.llm.providers.base import jdict
from core.llm.registry import LOCAL_ONLY, MODELS, STEPS
from core.models import Business, ModelChoice, RoutingRule
from eval_lab import benchmark as bench
from eval_lab import runner, special
from eval_lab.gate import request_gate
from eval_lab.models import Benchmark, CandidateCase, Case, CaseNote, Gate, HumanGrade, Result, Run
from eval_lab.projections import projections
from web.deps import Ctx, bid, business_ctx, db, owner_ctx
from web.state import queue
from web.templating import render

router = APIRouter(prefix="/evals")
CtxDep = Annotated[Ctx, Depends(business_ctx)]
OwnerDep = Annotated[Ctx, Depends(owner_ctx)]
DbDep = Annotated[AsyncSession, Depends(db)]

TABS = [
    ("Benchmarks", "/evals/benchmarks", "benchmarks"),
    ("Runs", "/evals/runs", "runs"),
    ("Compare models", "/evals/compare", "compare"),
    ("Side by side", "/evals/side", "side"),
    ("Judge check", "/evals/judge", "judge"),
    ("Routing", "/evals/routing", "routing"),
    ("Feedback cases", "/evals/feedback", "feedback"),
    ("Release gates", "/evals/gates", "gates"),
    ("Local-only", "/evals/local", "local"),
]

# Which end-to-end job each benchmark kind tests
JOB_FOR_KIND = {
    "brain": "brain_answer",
    "hidden": "brain_answer",
    "chat": "chat_answer",
    "feedback": "chat_answer",
    "starter": "chat_answer",
}
# "Close enough" when picking the cheapest model: within 2 points of the best pass rate
CLOSE_ENOUGH = 0.02


def page(request: Request, template: str, tab: str, **context: Any) -> HTMLResponse:
    return render(request, template, active="evals", tabs=TABS, tab=tab, **context)


def answer_cost_per_100() -> dict[str, float]:
    """Projected $ for 100 written answers, per model (shown before a run is started)."""
    return {p.model_key: p.cost_per_100 for p in projections() if p.job == "write_answer"}


@router.get("")
async def home() -> Response:
    return RedirectResponse("/evals/benchmarks", status_code=303)


# ---------------------------------------------------------------- benchmarks and runs


@router.get("/benchmarks", response_class=HTMLResponse)
async def benchmarks(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    rows = (
        (await session.execute(select(Benchmark).order_by(Benchmark.kind, Benchmark.version)))
        .scalars()
        .all()
    )
    counts: dict[uuid.UUID, dict[str, int]] = {}
    for benchmark_id, category, n in (
        await session.execute(
            select(Case.benchmark_id, Case.category, func.count()).group_by(
                Case.benchmark_id, Case.category
            )
        )
    ).all():
        counts.setdefault(benchmark_id, {})[category] = int(n)
    categories = sorted({c for per in counts.values() for c in per})
    return page(
        request,
        "evals/benchmarks.html",
        "benchmarks",
        benchmarks=rows,
        counts=counts,
        models=MODELS,
        categories=categories,
        projected=answer_cost_per_100(),
        kinds=JOB_FOR_KIND,
        notice=request.query_params.get("msg"),
    )


@router.post("/benchmarks/load")
async def load(ctx: OwnerDep) -> Response:
    loaded = await bench.load_benchmarks(bid(ctx))
    message = "Loaded " + ", ".join(f"{name} ({count} cases)" for name, count in loaded.items())
    return RedirectResponse(f"/evals/benchmarks?msg={message}", status_code=303)


@router.post("/runs")
async def start_run(request: Request, ctx: OwnerDep) -> Response:
    form = await request.form()
    kind = str(form.get("kind", "brain"))
    if kind not in JOB_FOR_KIND:
        raise HTTPException(400, "Pick a benchmark to run.")
    job = JOB_FOR_KIND[kind]
    models = [
        str(m)
        for m in form.getlist("models")
        if str(m) in MODELS or str(m) in ("router", "settings")
    ]
    categories = [str(c) for c in form.getlist("categories")] or None
    limit = int(str(form.get("limit") or 0) or 0) or None
    local = form.get("local_only") == "true"
    params: dict[str, Any] = {
        "categories": categories,
        "limit": limit,
        "second_opinion": form.get("second_opinion") == "true",
    }
    if local:
        params["overrides"] = {"local_only": True}
    name = (
        str(form.get("name") or "").strip()
        or f"{kind} benchmark on {len(models or ['settings'])} model(s)"
    )
    try:
        run_id = await runner.new_run(
            bid(ctx),
            benchmark_kind=kind,
            name=name,
            job=job,
            models=models or ["settings"],
            mode="local_only" if local else "normal",
            params=params,
            user_id=ctx.user_id,
        )
    except LookupError as exc:
        return RedirectResponse(f"/evals/benchmarks?msg={exc}", status_code=303)
    await queue().enqueue(
        "evals:run_eval", queue="web", business_id=str(bid(ctx)), run_id=str(run_id)
    )
    return RedirectResponse(f"/evals/runs/{run_id}", status_code=303)


@router.post("/special/{what}")
async def special_run(
    what: str, ctx: OwnerDep, cases: Annotated[int, Form(ge=2, le=100)] = 30
) -> Response:
    """The checks that are not "every case on every model": URL cases, retrieval, teach tests
    and the router proof."""
    business = str(bid(ctx))
    try:
        if what == "url-cases":
            run_id = await runner.new_run(
                bid(ctx),
                benchmark_kind="url",
                name="URL cases on the fixture site",
                job="url_cases",
                models=["no model"],
                mode="url",
                user_id=ctx.user_id,
            )
            await queue().enqueue(
                "evals:url_cases",
                queue="web",
                business_id=business,
                run_id=str(run_id),
                user_id=str(ctx.user_id),
            )
        elif what == "retrieval":
            run_id = await runner.new_run(
                bid(ctx),
                benchmark_kind="brain",
                name="Retrieval: keywords, meaning, re-rank",
                job="retrieval",
                models=["keyword", "hybrid+rerank", "vector"],
                mode="retrieval",
                user_id=ctx.user_id,
            )
            await queue().enqueue(
                "evals:retrieval_check", queue="web", business_id=business, run_id=str(run_id)
            )
        elif what == "teach":
            run_id = await runner.new_run(
                bid(ctx),
                benchmark_kind="teach",
                name="Teach tests: before and after",
                job="chat_answer",
                models=["before", "after"],
                mode="teach",
                user_id=ctx.user_id,
            )
            await queue().enqueue(
                "evals:teach_tests",
                queue="web",
                business_id=business,
                run_id=str(run_id),
                user_id=str(ctx.user_id),
            )
        elif what == "router":
            await queue().enqueue(
                "evals:router_proof",
                queue="web",
                business_id=business,
                cases=cases,
                user_id=str(ctx.user_id),
            )
            return RedirectResponse(
                "/evals/routing?msg=Router proof started: the same cases on always-Sonnet, "
                "then through the router.",
                status_code=303,
            )
        else:
            raise HTTPException(404)
    except LookupError as exc:
        return RedirectResponse(f"/evals/benchmarks?msg={exc}", status_code=303)
    return RedirectResponse(f"/evals/runs/{run_id}", status_code=303)


@router.get("/runs", response_class=HTMLResponse)
async def runs(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    rows = (
        (await session.execute(select(Run).order_by(Run.created_at.desc()).limit(100)))
        .scalars()
        .all()
    )
    busy = any(r.status in ("queued", "running") for r in rows)
    return page(
        request,
        "evals/runs.html",
        "runs",
        runs=rows,
        busy=busy,
        notice=request.query_params.get("msg"),
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(
    request: Request, run_id: uuid.UUID, ctx: CtxDep, session: DbDep
) -> HTMLResponse:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404)
    rows = (
        await session.execute(
            select(Result, Case)
            .join(Case, Case.id == Result.case_id)
            .where(Result.run_id == run_id)
            .order_by(Case.case_key, Result.model_key)
        )
    ).all()
    notes = {
        (n.case_id, n.model_key): n.note
        for n in (
            await session.execute(select(CaseNote).where(CaseNote.user_id == ctx.user_id))
        ).scalars()
    }
    categories = {case.id: case.category for _, case in rows}
    sweep = (
        special.threshold_sweep([r for r, _ in rows], categories) if run.job in runner.JOBS else []
    )
    business = await session.get(Business, bid(ctx))
    real_models = [m for m in (run.summary or {}) if m in MODELS]
    failing = sum(1 for result, _ in rows if result.passed is False)
    return page(
        request,
        "evals/run.html",
        "runs",
        run=run,
        rows=rows,
        notes=notes,
        sweep=sweep,
        models=MODELS,
        current_line=business.dont_know_threshold if business else None,
        can_apply=run.status == "done" and run.job in runner.JOBS and len(real_models) >= 2,
        failing=failing,
        busy=run.status in ("queued", "running"),
        notice=request.query_params.get("msg"),
    )


@router.post("/runs/{run_id}/apply")
async def apply_cheapest(run_id: uuid.UUID, ctx: OwnerDep, session: DbDep) -> Response:
    """Use the cheapest model whose pass rate is within 2 points of the best one, for writing
    answers. The change goes through the release gate like any other model change."""
    run = await session.get(Run, run_id)
    if run is None or run.status != "done":
        raise HTTPException(404)
    rated = {
        m: s
        for m, s in (run.summary or {}).items()
        if m in MODELS and s.get("pass_rate") is not None
    }
    if not rated:
        return RedirectResponse(
            f"/evals/runs/{run_id}?msg=No model in this run has a score yet.", status_code=303
        )
    best = max(s["pass_rate"] for s in rated.values())
    close = [m for m, s in rated.items() if s["pass_rate"] >= best - CLOSE_ENOUGH]
    cheapest = min(close, key=lambda m: rated[m].get("cost_per_100") or 0.0)
    current = await session.scalar(
        select(ModelChoice.model_key).where(ModelChoice.step == "write_answer")
    )
    current = current or STEPS["write_answer"].default
    if cheapest == current:
        return RedirectResponse(
            f"/evals/runs/{run_id}?msg={MODELS[cheapest].name} is already in use.", status_code=303
        )
    gate, needs_run = await request_gate(
        session,
        bid(ctx),
        target_kind="model_choice",
        target_key="write_answer",
        candidate_ref=cheapest,
        baseline_ref=current,
        user_id=ctx.user_id,
    )
    if needs_run:
        await queue().enqueue(
            "evals:run_gate", queue="web", business_id=str(bid(ctx)), gate_id=str(gate.id)
        )
        message = (
            f"{MODELS[cheapest].name} sent to the release gate; it goes live if no score drops."
        )
    else:
        message = f"{MODELS[cheapest].name} is live: {gate.reason}"
    return RedirectResponse(f"/evals/runs/{run_id}?msg={message}", status_code=303)


@router.post("/notes", response_class=HTMLResponse)
async def save_note(
    ctx: CtxDep,
    session: DbDep,
    case_id: Annotated[uuid.UUID, Form()],
    model_key: Annotated[str, Form()],
    note: Annotated[str, Form()],
) -> HTMLResponse:
    existing = (
        await session.execute(
            select(CaseNote).where(
                CaseNote.case_id == case_id,
                CaseNote.model_key == model_key,
                CaseNote.user_id == ctx.user_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            CaseNote(
                business_id=bid(ctx),
                case_id=case_id,
                model_key=model_key,
                note=note,
                user_id=ctx.user_id,
            )
        )
    else:
        existing.note = note
    return HTMLResponse('<span class="small" style="color:var(--ok)">saved</span>')


@router.post("/sweep/use")
async def use_line(
    ctx: OwnerDep,
    session: DbDep,
    line: Annotated[float, Form(ge=0, le=1)],
    run_id: Annotated[str, Form()] = "",
) -> Response:
    """Task 3.2: set the "I don't know" line to the one picked from the sweep table."""
    business = await session.get(Business, bid(ctx))
    assert business is not None
    business.dont_know_threshold = line
    target = f"/evals/runs/{run_id}" if run_id else "/evals/runs"
    return RedirectResponse(
        f'{target}?msg=The "I don\'t know" line is now {line:.1f}.', status_code=303
    )


# ---------------------------------------------------------------- comparisons


@router.get("/compare", response_class=HTMLResponse)
async def compare(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    """Per model and step: quality, $ for 100 runs, average and p95 time. Measured where a run
    exists (the end-to-end answer runs count for the "write the answer" step); otherwise the
    projected cost, labelled, and no quality."""
    measured: dict[tuple[str, str], dict[str, Any]] = {}
    for run in (
        await session.execute(select(Run).where(Run.status == "done").order_by(Run.finished_at))
    ).scalars():
        if run.job not in runner.JOBS:
            continue
        for model, raw in (run.summary or {}).items():
            stats = jdict(raw)
            if stats.get("scored"):
                measured[("write_answer", model)] = stats | {
                    "run_id": str(run.id),
                    "run_name": run.name,
                }
    rows: list[dict[str, Any]] = []
    for projection in projections():
        stats = measured.get((projection.job, projection.model_key))
        rows.append(
            {
                "job": projection.job,
                "step": STEPS[projection.job].label,
                "model": projection.model_name,
                "key": projection.model_key,
                "free": projection.free,
                "local": projection.local,
                "measured": stats is not None,
                "quality": stats.get("pass_rate") if stats else None,
                "cost_per_100": stats.get("cost_per_100") if stats else projection.cost_per_100,
                "avg_ms": stats.get("avg_ms") if stats else None,
                "p95_ms": stats.get("p95_ms") if stats else None,
                "run_id": stats.get("run_id") if stats else None,
            }
        )
    return page(request, "evals/compare.html", "compare", rows=rows)


@router.get("/side", response_class=HTMLResponse)
async def side_by_side(
    request: Request, ctx: CtxDep, session: DbDep, case_id: uuid.UUID | None = None
) -> HTMLResponse:
    cases = (
        (
            await session.execute(
                select(Case)
                .where(Case.id.in_(select(Result.case_id)))
                .order_by(Case.case_key)
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    answers: list[Result] = []
    chosen = None
    if case_id:
        chosen = await session.get(Case, case_id)
        answers = list(
            (
                await session.execute(
                    select(Result)
                    .where(Result.case_id == case_id, Result.answer.is_not(None))
                    .order_by(Result.created_at.desc())
                )
            ).scalars()
        )
    return page(
        request, "evals/side.html", "side", cases=cases, case=chosen, answers=answers, models=MODELS
    )


# ---------------------------------------------------------------- the judge


@router.get("/judge", response_class=HTMLResponse)
async def judge_check(request: Request, ctx: CtxDep) -> HTMLResponse:
    return page(
        request, "evals/judge.html", "judge", agreement=await runner.judge_agreement(bid(ctx))
    )


@router.get("/grade", response_class=HTMLResponse)
async def grade(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    """Blind grading: one judged result at a time, without the judge's score (Task 4.1)."""
    graded = select(HumanGrade.result_id).where(HumanGrade.user_id == ctx.user_id)
    row = (
        await session.execute(
            select(Result, Case)
            .join(Case, Case.id == Result.case_id)
            .where(Result.judge_score.is_not(None), Result.id.not_in(graded))
            .order_by(func.random())
            .limit(1)
        )
    ).first()
    done = int(
        await session.scalar(
            select(func.count()).select_from(HumanGrade).where(HumanGrade.user_id == ctx.user_id)
        )
        or 0
    )
    return page(request, "evals/grade.html", "judge", row=row, done=done)


@router.post("/grade/{result_id}")
async def save_grade(
    result_id: uuid.UUID,
    ctx: CtxDep,
    session: DbDep,
    passed: Annotated[bool, Form()],
    note: Annotated[str, Form()] = "",
) -> Response:
    session.add(
        HumanGrade(
            business_id=bid(ctx),
            result_id=result_id,
            user_id=ctx.user_id,
            passed=passed,
            note=note.strip() or None,
        )
    )
    return RedirectResponse("/evals/grade", status_code=303)


# ---------------------------------------------------------------- routing, feedback, gates


@router.get("/routing", response_class=HTMLResponse)
async def routing(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    rules = (
        (
            await session.execute(
                select(RoutingRule).order_by(RoutingRule.job, RoutingRule.complexity)
            )
        )
        .scalars()
        .all()
    )
    proofs = list(
        (
            await session.execute(
                select(Run)
                .where(Run.mode.in_(("router", "always_sonnet")))
                .order_by(Run.created_at.desc())
                .limit(6)
            )
        ).scalars()
    )
    latest = {
        mode: next((r for r in proofs if r.mode == mode and r.status == "done"), None)
        for mode in ("always_sonnet", "router")
    }
    saving = None
    sonnet, routed = latest["always_sonnet"], latest["router"]
    if sonnet and routed and sonnet.total_cost_usd:
        saving = float((sonnet.total_cost_usd - routed.total_cost_usd) / sonnet.total_cost_usd)
    return page(
        request,
        "evals/routing.html",
        "routing",
        rules=rules,
        proofs=proofs,
        latest=latest,
        saving=saving,
        models=MODELS,
        projected=answer_cost_per_100(),
        notice=request.query_params.get("msg"),
    )


@router.get("/feedback", response_class=HTMLResponse)
async def feedback_cases(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    rows = (
        (
            await session.execute(
                select(CandidateCase)
                .order_by(CandidateCase.status.desc(), CandidateCase.created_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    return page(
        request,
        "evals/feedback.html",
        "feedback",
        rows=rows,
        notice=request.query_params.get("msg"),
    )


@router.post("/feedback/{candidate_id}/{decision}")
async def decide_case(
    candidate_id: uuid.UUID,
    decision: str,
    ctx: OwnerDep,
    session: DbDep,
    expected: Annotated[str, Form()] = "",
) -> Response:
    """Task 4.4: real feedback becomes a test case, in the "Real feedback" benchmark."""
    candidate = await session.get(CandidateCase, candidate_id)
    if candidate is None or decision not in ("accept", "reject"):
        raise HTTPException(404)
    if candidate.status != "new":
        return RedirectResponse("/evals/feedback?msg=Already decided.", status_code=303)
    if decision == "reject":
        candidate.status = "rejected"
        return RedirectResponse("/evals/feedback?msg=Rejected.", status_code=303)
    benchmark = (
        (await session.execute(select(Benchmark).where(Benchmark.kind == "feedback")))
        .scalars()
        .first()
    )
    if benchmark is None:
        benchmark = Benchmark(
            business_id=bid(ctx), name="Real feedback", version="v1", kind="feedback"
        )
        session.add(benchmark)
        await session.flush()
    number = (
        int(
            await session.scalar(
                select(func.count()).select_from(Case).where(Case.benchmark_id == benchmark.id)
            )
            or 0
        )
        + 1
    )
    case = Case(
        business_id=bid(ctx),
        benchmark_id=benchmark.id,
        case_key=f"FB-{number:03d}",
        category="regular",
        question=candidate.question,
        expected=expected.strip() or candidate.expected,
        checks={"must_answer": True},
    )
    session.add(case)
    await session.flush()
    benchmark.case_count = number
    candidate.status, candidate.accepted_case_id = "accepted", case.id
    return RedirectResponse(
        f"/evals/feedback?msg=Added as {case.case_key} to the Real feedback benchmark.",
        status_code=303,
    )


@router.get("/gates", response_class=HTMLResponse)
async def gates(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    rows = (
        (await session.execute(select(Gate).order_by(Gate.created_at.desc()).limit(100)))
        .scalars()
        .all()
    )
    return page(
        request,
        "evals/gates.html",
        "gates",
        gates=rows,
        busy=any(g.status == "running" for g in rows),
    )


@router.get("/local", response_class=HTMLResponse)
async def local_only(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    """Task 4.2's local-only mode: the same chat cases on local models, next to the normal run."""
    business = await session.get(Business, bid(ctx))
    finished = list(
        (
            await session.execute(
                select(Run)
                .where(Run.status == "done", Run.job.in_(runner.JOBS))
                .order_by(Run.finished_at.desc())
                .limit(50)
            )
        ).scalars()
    )
    local = next((r for r in finished if r.mode == "local_only"), None)
    normal = next(
        (
            r
            for r in finished
            if r.mode == "normal" and (local is None or r.benchmark_id == local.benchmark_id)
        ),
        None,
    )
    steps = [
        {"label": STEPS[step].label, "model": MODELS[key].name if key in MODELS else key}
        for step, key in LOCAL_ONLY.items()
        if step in STEPS
    ]
    return page(
        request,
        "evals/local.html",
        "local",
        business=business,
        local=local,
        normal=normal,
        steps=steps,
    )
