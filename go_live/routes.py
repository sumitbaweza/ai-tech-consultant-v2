"""Screen 4, Go Live: one guided setup, timed with a stopwatch (Feature 6).

Business and tone -> keys -> knowledge (sitemap, page URLs, documents) -> Gmail -> roles ->
build (starter slice read, starting test set written, Eval Lab run) -> readiness score, and the
chat switches on only when the score reaches the owner's level.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.brain_client import BrainClient, BrainError
from core.config import get_settings
from core.llm.providers.base import jdict
from core.models import Business, Connection, Role
from go_live import service
from go_live.models import STEPS, Setup
from web.deps import Ctx, bid, db, owner_ctx
from web.state import queue
from web.templating import render
from workspace.connections import KINDS, save_key, test_connection

router = APIRouter(prefix="/setup")
OwnerDep = Annotated[Ctx, Depends(owner_ctx)]
DbDep = Annotated[AsyncSession, Depends(db)]
KEY_KINDS = ("claude", "gemini", "openrouter", "ollama")


def go(step: str, message: str | None = None) -> Response:
    return RedirectResponse(
        f"/setup/{step}" + (f"?msg={message}" if message else ""), status_code=303
    )


async def _setup(session: AsyncSession, ctx: Ctx) -> Setup:
    return await service.current(session, bid(ctx), ctx.user_id)


async def _sources(ctx: Ctx, setup: Setup) -> list[dict[str, Any]]:
    """Each source of this setup with its plan and live progress, from the Brain's API."""
    found: list[dict[str, Any]] = []
    try:
        async with await BrainClient.for_business(bid(ctx), ctx.user_id) as brain:
            for source_id in setup.source_ids:
                source = await brain.get(f"/v1/sources/{source_id}")
                source["progress"] = await brain.get(f"/v1/sources/{source_id}/progress")
                found.append(source)
            documents = await brain.get("/v1/documents")
    except BrainError:
        return found
    if documents:
        found.append({"kind": "documents", "documents": documents})
    return found


@router.get("")
async def home(ctx: OwnerDep, session: DbDep) -> Response:
    setup = await _setup(session, ctx)
    return go(setup.current_step)


@router.get("/{step}", response_class=HTMLResponse)
async def show(request: Request, step: str, ctx: OwnerDep, session: DbDep) -> HTMLResponse:
    if step not in STEPS:
        raise HTTPException(404)
    setup = await _setup(session, ctx)
    service.mark(setup, step, "started")
    business = await session.get(Business, bid(ctx))
    assert business is not None
    context: dict[str, Any] = {}
    if step == "keys":
        connections = {c.kind: c for c in (await session.execute(select(Connection))).scalars()}
        context["keys"] = [(kind, KINDS[kind], connections.get(kind)) for kind in KEY_KINDS]
    elif step == "knowledge" or step == "build":
        context["sources"] = await _sources(ctx, setup)
    elif step == "gmail":
        google = (
            await session.execute(select(Connection).where(Connection.kind == "google"))
        ).scalar_one_or_none()
        context["gmail"] = google
        context["google_ready"] = bool(get_settings().google_client_id)
    elif step == "roles":
        context["roles"] = (await session.execute(select(Role).order_by(Role.name))).scalars().all()
    if step == "build":
        context["error"] = (setup.step_times or {}).get("build", {}).get("error")
    return render(
        request,
        "setup/step.html",
        active="setup",
        step=step,
        steps=STEPS,
        labels=service.LABELS,
        setup=setup,
        done=service.done_steps(setup),
        business=business,
        tones=service.TONES,
        elapsed=service.seconds_taken(setup),
        step_seconds=service.step_seconds(setup),
        score=service.score(setup),
        notice=request.query_params.get("msg"),
        **context,
    )


@router.post("/business")
async def save_business(
    ctx: OwnerDep,
    session: DbDep,
    name: Annotated[str, Form(min_length=2, max_length=120)],
    tone: Annotated[str, Form(max_length=200)] = "",
    tone_other: Annotated[str, Form()] = "",
) -> Response:
    business = await session.get(Business, bid(ctx))
    assert business is not None
    business.name = name.strip()
    business.tone = (tone_other.strip() or tone.strip() or business.tone)[:200]
    setup = await _setup(session, ctx)
    service.mark(setup, "business", "done")
    setup.current_step = "keys"
    return go("keys")


@router.post("/keys/{kind}")
async def save_one_key(
    kind: str, ctx: OwnerDep, session: DbDep, value: Annotated[str, Form(min_length=1)]
) -> Response:
    if kind not in KEY_KINDS:
        raise HTTPException(404)
    await save_key(session, bid(ctx), kind, value)
    connection = await test_connection(session, bid(ctx), kind)
    return go("keys", f"{KINDS[kind][0]}: {connection.last_test_message or connection.status}")


@router.post("/keys")
async def keys_done(ctx: OwnerDep, session: DbDep) -> Response:
    claude = (
        await session.execute(select(Connection).where(Connection.kind == "claude"))
    ).scalar_one_or_none()
    if (claude is None or claude.status != "ok") and not get_settings().llm_fake:
        return go("keys", "Add a working Claude key first: the app runs on Claude.")
    setup = await _setup(session, ctx)
    service.mark(setup, "keys", "done")
    setup.current_step = "knowledge"
    return go("knowledge")


async def _remember(session: AsyncSession, ctx: Ctx, source_id: str) -> None:
    setup = await _setup(session, ctx)
    known = list(setup.source_ids)
    if uuid.UUID(source_id) not in known:
        setup.source_ids = [*known, uuid.UUID(source_id)]


@router.post("/knowledge/sitemap")
async def add_sitemap(
    ctx: OwnerDep, session: DbDep, url: Annotated[str, Form(min_length=8)]
) -> Response:
    try:
        async with await BrainClient.for_business(bid(ctx), ctx.user_id) as brain:
            planned = await brain.post("/v1/sources/sitemaps", {"urls": [url.strip()]})
    except BrainError as exc:
        return go("knowledge", f"That sitemap couldn't be read: {exc}")
    await _remember(session, ctx, str(planned["source"]["id"]))
    plan = jdict(planned.get("plan"))
    return go(
        "knowledge",
        f"Planned: {plan.get('unique_urls', 0)} pages in {plan.get('sitemap_count', 0)} sitemap(s). "
        "Check the plan, then start reading.",
    )


@router.post("/knowledge/start/{source_id}")
async def start_reading(source_id: uuid.UUID, ctx: OwnerDep, session: DbDep) -> Response:
    setup = await _setup(session, ctx)
    if source_id not in setup.source_ids:
        raise HTTPException(404)
    async with await BrainClient.for_business(bid(ctx), ctx.user_id) as brain:
        await brain.post(
            f"/v1/sources/{source_id}/start",
            {"starter_pages": setup.starter_pages, "continue_after_starter": True},
        )
    return go(
        "knowledge",
        f"Reading the first {setup.starter_pages} pages now; the rest follows in the background.",
    )


@router.post("/knowledge/page")
async def add_page(
    ctx: OwnerDep, session: DbDep, url: Annotated[str, Form(min_length=8)]
) -> Response:
    try:
        async with await BrainClient.for_business(bid(ctx), ctx.user_id) as brain:
            added = await brain.post("/v1/page-urls", {"url": url.strip()})
    except BrainError as exc:
        return go("knowledge", f"That page couldn't be added: {exc}")
    await _remember(session, ctx, str(added["source"]["id"]))
    return go("knowledge", "Page added and being read.")


@router.post("/knowledge/document")
async def add_document(ctx: OwnerDep, file: Annotated[UploadFile, File()]) -> Response:
    data = await file.read()
    try:
        async with await BrainClient.for_business(bid(ctx), ctx.user_id) as brain:
            await brain.request(
                "POST",
                "/v1/documents",
                files={
                    "file": (
                        file.filename or "document.txt",
                        data,
                        file.content_type or "application/octet-stream",
                    )
                },
            )
    except BrainError as exc:
        return go("knowledge", f"That document couldn't be read: {exc}")
    return go("knowledge", f"{file.filename} added.")


@router.post("/knowledge")
async def knowledge_done(ctx: OwnerDep, session: DbDep) -> Response:
    setup = await _setup(session, ctx)
    if not await _sources(ctx, setup):
        return go("knowledge", "Add a sitemap, a page URL or a document first.")
    service.mark(setup, "knowledge", "done")
    setup.current_step = "gmail"
    return go("gmail")


@router.post("/gmail")
async def gmail_done(ctx: OwnerDep, session: DbDep) -> Response:
    setup = await _setup(session, ctx)
    service.mark(setup, "gmail", "done")
    setup.current_step = "roles"
    return go("roles")


@router.post("/roles")
async def roles_done(request: Request, ctx: OwnerDep, session: DbDep) -> Response:
    form = await request.form()
    picked = {str(v) for v in form.getlist("role_ids")}
    if not picked:
        return go("roles", "Pick at least one role.")
    for role in (await session.execute(select(Role))).scalars():
        role.archived_at = (
            None if str(role.id) in picked else (role.archived_at or datetime.now(UTC))
        )
    setup = await _setup(session, ctx)
    service.mark(setup, "roles", "done")
    service.mark(setup, "build", "started")
    setup.current_step, setup.status = "build", "building"
    await session.flush()
    await queue().enqueue(
        "golive:build",
        queue="web",
        lock=f"golive:{bid(ctx)}",
        business_id=str(bid(ctx)),
        setup_id=str(setup.id),
    )
    return go("build")


@router.post("/build")
async def build_again(ctx: OwnerDep, session: DbDep) -> Response:
    """Build (again): a new starting test set and a new readiness score."""
    setup = await _setup(session, ctx)
    setup.current_step, setup.status = "build", "building"
    setup.readiness_correct = setup.readiness_total = None
    service.mark(setup, "build", "started")
    await session.flush()
    await queue().enqueue(
        "golive:build",
        queue="web",
        lock=f"golive:{bid(ctx)}",
        business_id=str(bid(ctx)),
        setup_id=str(setup.id),
    )
    return go("build")


@router.post("/readiness/threshold")
async def threshold(
    ctx: OwnerDep, session: DbDep, level: Annotated[int, Form(ge=50, le=100)]
) -> Response:
    business = await session.get(Business, bid(ctx))
    assert business is not None
    business.readiness_threshold = level
    return go("readiness", f"The chat opens at {level}% or more.")


@router.post("/readiness/switch-on")
async def switch_on(ctx: OwnerDep, session: DbDep) -> Response:
    setup = await _setup(session, ctx)
    business = await session.get(Business, bid(ctx))
    assert business is not None
    reached = service.score(setup)
    if reached is None or reached * 100 < business.readiness_threshold:
        return go("readiness", f"Not yet: the score must reach {business.readiness_threshold}%.")
    business.chat_enabled = True
    service.mark(setup, "readiness", "done")
    setup.status, setup.finished_at = "ready", datetime.now(UTC)
    minutes, seconds = divmod(service.seconds_taken(setup), 60)
    return RedirectResponse(
        f"/?msg=The chat is on for your team. Setup took {minutes} min {seconds:02d} s.",
        status_code=303,
    )


@router.post("/restart")
async def restart(ctx: OwnerDep, session: DbDep) -> Response:
    """A new setup run (a new stopwatch); the knowledge already read stays."""
    session.add(Setup(business_id=bid(ctx), created_by=ctx.user_id, started_at=datetime.now(UTC)))
    return go("business")
