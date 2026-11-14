"""Settings (screen 14): business, keys and connections, models, routing, members and API
keys, advanced, danger zone. Prompts and roles have their own editors (workspace/editors.py)."""

import secrets
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core import google, storage
from core.brain_client import BrainClient, BrainError
from core.cache import PostgresCache
from core.config import get_settings
from core.crypto import encrypt
from core.db import tenant_session
from core.jobs import delete_business_jobs
from core.llm.registry import MODELS, STEPS, models_for_job
from core.models import (
    ApiKey,
    AuditEvent,
    Business,
    Connection,
    Membership,
    ModelChoice,
    RoutingRule,
    User,
)
from eval_lab.gate import request_gate
from web.deps import Ctx, bid, business_ctx, db, owner_ctx
from web.state import queue
from web.templating import render
from workspace.connections import KINDS, get_connection, save_key, test_connection
from workspace.service import create_api_key

router = APIRouter(prefix="/settings")
oauth_router = APIRouter()

TABS = [
    ("Business", "/settings/business", "business"),
    ("Keys and connections", "/settings/keys", "keys"),
    ("Models", "/settings/models", "models"),
    ("Prompts", "/settings/prompts", "prompts"),
    ("Roles", "/settings/roles", "roles"),
    ("Skills", "/knowledge/skills", "skills"),
    ("Routing", "/settings/routing", "routing"),
    ("Members and API keys", "/settings/members", "members"),
    ("Advanced", "/settings/advanced", "advanced"),
    ("Danger zone", "/settings/danger", "danger"),
]

CtxDep = Annotated[Ctx, Depends(business_ctx)]
OwnerDep = Annotated[Ctx, Depends(owner_ctx)]
DbDep = Annotated[AsyncSession, Depends(db)]


def page(request: Request, template: str, tab: str, **context: Any) -> HTMLResponse:
    return render(request, template, active="settings", tabs=TABS, tab=tab, **context)


async def audit(session: AsyncSession, ctx: Ctx, action: str, target: str, **detail: Any) -> None:
    session.add(
        AuditEvent(
            business_id=bid(ctx), user_id=ctx.user_id, action=action, target=target, detail=detail
        )
    )


@router.get("")
async def settings_home() -> Response:
    return RedirectResponse("/settings/business", status_code=303)


# ---------------------------------------------------------------- business


@router.get("/business", response_class=HTMLResponse)
async def business_tab(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    business = await session.get(Business, bid(ctx))
    return page(
        request,
        "workspace/settings_business.html",
        "business",
        business=business,
        notice=request.query_params.get("saved") and "Saved.",
    )


@router.post("/business")
async def save_business(
    ctx: OwnerDep,
    session: DbDep,
    name: Annotated[str, Form()],
    tone: Annotated[str, Form()],
    readiness_threshold: Annotated[int, Form(ge=0, le=100)],
    dont_know_threshold: Annotated[float, Form(ge=0, le=1)],
) -> Response:
    business = await session.get(Business, bid(ctx))
    assert business is not None
    business.name, business.tone = name.strip() or business.name, tone.strip()
    business.readiness_threshold = readiness_threshold
    business.dont_know_threshold = dont_know_threshold
    await audit(
        session, ctx, "business.update", "business", readiness_threshold=readiness_threshold
    )
    return RedirectResponse("/settings/business?saved=1", status_code=303)


# ---------------------------------------------------------------- keys and connections


async def _connections(session: AsyncSession) -> dict[str, Connection | None]:
    rows = {c.kind: c for c in (await session.execute(select(Connection))).scalars()}
    return {kind: rows.get(kind) for kind in KINDS}


@router.get("/keys", response_class=HTMLResponse)
async def keys_tab(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    return page(
        request,
        "workspace/settings_keys.html",
        "keys",
        connections=await _connections(session),
        kinds=KINDS,
        google_ready=google.configured(),
        default_ollama=get_settings().ollama_url,
        notice=request.query_params.get("msg"),
    )


@router.post("/keys/{kind}", response_class=HTMLResponse)
async def save_connection(
    request: Request, kind: str, ctx: OwnerDep, session: DbDep, value: Annotated[str, Form()]
) -> Response:
    if kind not in KINDS or kind == "google" or not value.strip():
        raise HTTPException(400, "Unknown connection or empty value")
    connection = await save_key(session, bid(ctx), kind, value)
    await audit(session, ctx, "connection.save", kind)  # the key itself is never logged
    return render(
        request,
        "workspace/_connection_row.html",
        kind=kind,
        info=KINDS[kind],
        connection=connection,
        google_ready=google.configured(),
    )


@router.post("/keys/{kind}/test", response_class=HTMLResponse)
async def test_connection_route(
    request: Request, kind: str, ctx: CtxDep, session: DbDep
) -> HTMLResponse:
    try:
        connection = await test_connection(session, bid(ctx), kind)
    except ValueError as exc:
        connection = await get_connection(session, kind)
        return render(
            request,
            "workspace/_connection_row.html",
            kind=kind,
            info=KINDS[kind],
            connection=connection,
            error=str(exc),
            google_ready=google.configured(),
        )
    return render(
        request,
        "workspace/_connection_row.html",
        kind=kind,
        info=KINDS[kind],
        connection=connection,
        google_ready=google.configured(),
    )


@router.post("/keys/{kind}/delete")
async def delete_connection(kind: str, ctx: OwnerDep, session: DbDep) -> Response:
    connection = await get_connection(session, kind)
    if connection is not None:
        if kind == "google":
            await google.revoke(connection, bid(ctx))
        await session.delete(connection)
        await audit(session, ctx, "connection.delete", kind)
    return RedirectResponse("/settings/keys", status_code=303)


@oauth_router.get("/connections/google/start")
async def google_start(ctx: Annotated[Ctx, Depends(owner_ctx)], next: str = "") -> Response:
    # Where to land afterwards: only our own setup or settings pages (no open redirect)
    back = next if next in ("/setup/gmail", "/settings/keys") else "/settings/keys"
    if not google.configured():
        return RedirectResponse(
            "/settings/keys?msg=Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env first.",
            status_code=303,
        )
    state = secrets.token_urlsafe(24)
    # The state ties Google's answer to this business and user, for 10 minutes
    await PostgresCache("web").set(
        bid(ctx), f"oauth:{state}", {"user": str(ctx.user_id), "back": back}, 600
    )
    return RedirectResponse(google.auth_url(state), status_code=303)


@oauth_router.get("/connections/google/callback")
async def google_callback(
    ctx: Annotated[Ctx, Depends(owner_ctx)], state: str = "", code: str = "", error: str = ""
) -> Response:
    cache = PostgresCache("web")
    saved = await cache.get(bid(ctx), f"oauth:{state}") if state else None
    back = str(saved.get("back", "/settings/keys")) if saved else "/settings/keys"
    if error or not code or saved is None or saved.get("user") != str(ctx.user_id):
        return RedirectResponse(f"{back}?msg=Gmail was not connected.", status_code=303)
    await cache.delete(bid(ctx), f"oauth:{state}")
    tokens = await google.exchange_code(code)
    refresh = tokens.get("refresh_token")
    if not refresh:
        return RedirectResponse(
            "/settings/keys?msg=Google sent no refresh token; remove the app's access in your Google account and try again.",
            status_code=303,
        )
    email = await google.account_email(str(tokens["access_token"]))
    async with tenant_session(bid(ctx), user_id=ctx.user_id) as session:
        connection = await get_connection(session, "google") or Connection(
            business_id=bid(ctx), kind="google"
        )
        connection.secret_enc = encrypt(str(refresh), business_id=bid(ctx))
        connection.masked = email
        connection.config = {"email": email, "scopes": str(tokens.get("scope", "")).split()}
        connection.status = "ok"
        connection.last_test_message = f"Connected as {email}."
        session.add(connection)
    return RedirectResponse(f"{back}?msg=Gmail connected.", status_code=303)


# ---------------------------------------------------------------- models per step


@router.get("/models", response_class=HTMLResponse)
async def models_tab(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    chosen = {c.step: c.model_key for c in (await session.execute(select(ModelChoice))).scalars()}
    business = await session.get(Business, bid(ctx))
    steps = [
        {
            "key": step,
            "label": info.label,
            "current": chosen.get(step, info.default),
            "recommended": [
                m.key for m in models_for_job("judge" if step == "judge_second" else step)
            ],
        }
        for step, info in STEPS.items()
    ]
    return page(
        request,
        "workspace/settings_models.html",
        "models",
        steps=steps,
        models=MODELS,
        local_only=business.local_only if business else False,
        notice=request.query_params.get("msg"),
    )


@router.post("/models")
async def save_models(request: Request, ctx: OwnerDep, session: DbDep) -> Response:
    form = await request.form()
    chosen = {c.step: c.model_key for c in (await session.execute(select(ModelChoice))).scalars()}
    gates = 0
    for step, info in STEPS.items():
        new = form.get(f"step_{step}")
        current = chosen.get(step, info.default)
        if isinstance(new, str) and new in MODELS and new != current:
            gate, needs_run = await request_gate(
                session,
                bid(ctx),
                target_kind="model_choice",
                target_key=step,
                candidate_ref=new,
                baseline_ref=current,
                user_id=ctx.user_id,
            )
            if needs_run:
                gates += 1
                await queue().enqueue(
                    "evals:run_gate", queue="web", business_id=str(bid(ctx)), gate_id=str(gate.id)
                )
    message = (
        f"{gates} change(s) sent to the release gate; they go live if no score drops."
        if gates
        else "Saved."
    )
    return RedirectResponse(f"/settings/models?msg={message}", status_code=303)


# ---------------------------------------------------------------- routing table


@router.get("/routing", response_class=HTMLResponse)
async def routing_tab(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    rules = (
        (
            await session.execute(
                select(RoutingRule).order_by(RoutingRule.job, RoutingRule.complexity)
            )
        )
        .scalars()
        .all()
    )
    return page(
        request,
        "workspace/settings_routing.html",
        "routing",
        rules=rules,
        models=MODELS,
        jobs=list(STEPS),
        notice=request.query_params.get("saved") and "Saved.",
    )


@router.post("/routing")
async def save_routing(request: Request, ctx: OwnerDep, session: DbDep) -> Response:
    form = await request.form()
    for rule in (await session.execute(select(RoutingRule))).scalars():
        value = form.get(f"rule_{rule.id}")
        if isinstance(value, str) and value in MODELS and value != rule.model_key:
            rule.model_key, rule.source = value, "human"
    job, complexity, model = form.get("new_job"), form.get("new_complexity"), form.get("new_model")
    if (
        isinstance(job, str)
        and job in STEPS
        and complexity in ("low", "medium", "high")
        and model in MODELS
    ):
        exists = await session.scalar(
            select(RoutingRule.id).where(
                RoutingRule.job == job, RoutingRule.complexity == complexity
            )
        )
        if exists is None:
            session.add(
                RoutingRule(
                    business_id=bid(ctx),
                    job=job,
                    complexity=str(complexity),
                    model_key=str(model),
                    source="human",
                )
            )
    await audit(session, ctx, "routing.update", "routing_rules")
    return RedirectResponse("/settings/routing?saved=1", status_code=303)


# ---------------------------------------------------------------- members and API keys


@router.get("/members", response_class=HTMLResponse)
async def members_tab(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    members = (
        await session.execute(
            select(User.name, User.email, Membership.role).join(
                Membership, Membership.user_id == User.id
            )
        )
    ).all()
    keys = (
        (await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))).scalars().all()
    )
    return page(
        request,
        "workspace/settings_members.html",
        "members",
        members=members,
        keys=keys,
        brain_url="http://localhost:8081",
    )


@router.post("/api-keys", response_class=HTMLResponse)
async def new_api_key(
    request: Request, ctx: OwnerDep, session: DbDep, name: Annotated[str, Form()]
) -> HTMLResponse:
    created = await create_api_key(
        session, bid(ctx), name=name.strip() or "API key", user_id=ctx.user_id
    )
    await audit(session, ctx, "api_key.create", str(created.id))
    return render(request, "workspace/_new_key.html", key=created.key)


@router.post("/api-keys/{key_id}/revoke")
async def revoke_api_key(key_id: uuid.UUID, ctx: OwnerDep, session: DbDep) -> Response:
    from datetime import UTC, datetime

    await session.execute(
        update(ApiKey).where(ApiKey.id == key_id).values(revoked_at=datetime.now(UTC))
    )
    await audit(session, ctx, "api_key.revoke", str(key_id))
    return RedirectResponse("/settings/members", status_code=303)


# ---------------------------------------------------------------- advanced


@router.get("/advanced", response_class=HTMLResponse)
async def advanced_tab(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    business = await session.get(Business, bid(ctx))
    assert business is not None
    synonyms: list[dict[str, Any]] = []
    brain_error = None
    try:
        async with await BrainClient.for_business(bid(ctx)) as brain:
            synonyms = await brain.get("/v1/synonyms")
    except BrainError as exc:
        brain_error = exc.detail
    return page(
        request,
        "workspace/settings_advanced.html",
        "advanced",
        business=business,
        advanced=business.advanced or {},
        synonyms=synonyms,
        brain_error=brain_error,
        notice=request.query_params.get("msg"),
    )


@router.post("/advanced")
async def save_advanced(
    ctx: OwnerDep,
    session: DbDep,
    local_only: Annotated[bool, Form()] = False,
    allow_free_with_customer_data: Annotated[bool, Form()] = False,
    count_tokens: Annotated[str, Form()] = "always",
    crawl_rate: Annotated[float, Form(gt=0, le=5)] = 1.0,
) -> Response:
    business = await session.get(Business, bid(ctx))
    assert business is not None
    business.local_only = local_only
    business.advanced = {
        **(business.advanced or {}),
        "allow_free_with_customer_data": allow_free_with_customer_data,
        "count_tokens": count_tokens if count_tokens in ("always", "large_only") else "always",
        "crawl_rate": crawl_rate,
    }
    await audit(session, ctx, "advanced.update", "business", local_only=local_only)
    return RedirectResponse("/settings/advanced?msg=Saved.", status_code=303)


@router.post("/synonyms")
async def save_synonyms(
    ctx: OwnerDep, term: Annotated[str, Form()], synonyms: Annotated[str, Form()]
) -> Response:
    words = [w.strip().lower() for w in synonyms.split(",") if w.strip()]
    async with await BrainClient.for_business(bid(ctx)) as brain:
        await brain.put("/v1/synonyms", {"term": term.strip().lower(), "synonyms": words})
    return RedirectResponse("/settings/advanced?msg=Synonyms saved.", status_code=303)


@router.post("/caching-proof")
async def caching_proof(ctx: OwnerDep, tasks: Annotated[int, Form(ge=2, le=100)] = 100) -> Response:
    """Task 1.3: the same answers with caching off, then on; totals compared from the ledger."""
    await queue().enqueue(
        "evals:caching_proof", queue="web", business_id=str(bid(ctx)), tasks=tasks
    )
    return RedirectResponse(
        "/evals/runs?msg=Caching proof started: two runs, off then on.", status_code=303
    )


# ---------------------------------------------------------------- danger zone (D14)


@router.get("/danger", response_class=HTMLResponse)
async def danger_tab(request: Request, ctx: CtxDep) -> HTMLResponse:
    return page(
        request, "workspace/settings_danger.html", "danger", error=request.query_params.get("error")
    )


@router.post("/danger/delete")
async def delete_business(ctx: OwnerDep, confirm_name: Annotated[str, Form()]) -> Response:
    business_id = bid(ctx)
    if confirm_name.strip() != ctx.business_name:
        return RedirectResponse(
            "/settings/danger?error=The name didn't match; nothing was deleted.", status_code=303
        )
    # 1. The Brain deletes its files (screenshots, documents)
    try:
        async with await BrainClient.for_business(business_id) as brain:
            await brain.delete("/v1/business/files")
    except BrainError:
        pass  # the folders are removed below as well; rows go with the cascade
    async with tenant_session(business_id, user_id=ctx.user_id) as session:
        # 2. Google forgets our access
        connection = await get_connection(session, "google")
        if connection is not None:
            await google.revoke(connection, business_id)
        # 3. Queued jobs that name the business
        await delete_business_jobs(session, business_id)
        # 4. The business row; ON DELETE CASCADE removes every other row and embedding
        await session.execute(delete(Business).where(Business.id == business_id))
    # 5. Every file folder of the business
    storage.delete_business(business_id)
    return RedirectResponse("/businesses", status_code=303)
