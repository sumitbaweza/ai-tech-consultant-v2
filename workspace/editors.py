"""The prompt editor (Task 1.3) and the role editor with its test chat (Task 1.4).

Saving never overwrites: it adds a version. The new version goes through the release gate
(eval_lab/gate.py) and is live only when no score dropped. Restoring an old version, or the
default, copies it into a new version the same way.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.llm.registry import MODELS
from core.models import Prompt, PromptVersion, Role, RoleVersion
from core.prompts import add_version, defaults, diff, missing_variables, variables_in
from eval_lab.gate import TargetKind, request_gate
from web.deps import Ctx, bid, business_ctx, db, owner_ctx
from web.state import queue
from web.templating import render
from workspace.defaults import ALWAYS_HAND_OVER, CAPABILITIES
from workspace.settings import TABS, audit

router = APIRouter(prefix="/settings")
CtxDep = Annotated[Ctx, Depends(business_ctx)]
OwnerDep = Annotated[Ctx, Depends(owner_ctx)]
DbDep = Annotated[AsyncSession, Depends(db)]

KNOWLEDGE_TYPES = [
    "sitemap",
    "page_url",
    "instruction",
    "skill",
    "past_email",
    "document",
    "lesson",
    "expert",
]


def page(request: Request, template: str, tab: str, **context: Any) -> HTMLResponse:
    return render(request, template, active="settings", tabs=TABS, tab=tab, **context)


async def _gate(
    session: AsyncSession,
    ctx: Ctx,
    kind: TargetKind,
    key: str,
    candidate: uuid.UUID,
    baseline: uuid.UUID | None,
) -> str:
    gate, needs_run = await request_gate(
        session,
        bid(ctx),
        target_kind=kind,
        target_key=key,
        candidate_ref=str(candidate),
        baseline_ref=str(baseline) if baseline else None,
        user_id=ctx.user_id,
    )
    if needs_run:
        await queue().enqueue(
            "evals:run_gate", queue="web", business_id=str(bid(ctx)), gate_id=str(gate.id)
        )
        return "Saved as a new version. The release gate is checking it; it goes live if no score drops."
    return f"Saved and live. {gate.reason}"


# ---------------------------------------------------------------- prompts


@router.get("/prompts", response_class=HTMLResponse)
async def prompts_tab(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    rows = (
        await session.execute(
            select(Prompt, PromptVersion)
            .join(
                PromptVersion,
                (PromptVersion.prompt_id == Prompt.id) & (PromptVersion.status == "live"),
                isouter=True,
            )
            .order_by(Prompt.advanced, Prompt.key)
        )
    ).all()
    pending = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(PromptVersion.prompt_id, func.count())
                .where(PromptVersion.status.in_(("gating", "blocked")))
                .group_by(PromptVersion.prompt_id)
            )
        ).all()
    }
    return page(request, "workspace/prompts.html", "prompts", rows=rows, pending=pending)


async def _prompt(session: AsyncSession, key: str) -> Prompt:
    prompt = (await session.execute(select(Prompt).where(Prompt.key == key))).scalar_one_or_none()
    if prompt is None:
        raise HTTPException(404, "No such prompt")
    return prompt


@router.get("/prompts/{key}", response_class=HTMLResponse)
async def prompt_editor(request: Request, key: str, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    prompt = await _prompt(session, key)
    versions = (
        (
            await session.execute(
                select(PromptVersion)
                .where(PromptVersion.prompt_id == prompt.id)
                .order_by(PromptVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    live = next((v for v in versions if v.status == "live"), versions[0] if versions else None)
    return page(
        request,
        "workspace/prompt_editor.html",
        "prompts",
        prompt=prompt,
        versions=versions,
        live=live,
        body=live.body if live else "",
        variables=variables_in(live.body if live else ""),
        notice=request.query_params.get("msg"),
    )


@router.post("/prompts/{key}", response_class=HTMLResponse)
async def save_prompt(
    request: Request,
    key: str,
    ctx: OwnerDep,
    session: DbDep,
    body: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
    confirm: Annotated[bool, Form()] = False,
) -> Response:
    prompt = await _prompt(session, key)
    missing = missing_variables(body, prompt.required_vars)
    if missing and not confirm:
        # Task 1.3: warn before saving when a variable the app fills in is gone
        versions = (
            (
                await session.execute(
                    select(PromptVersion)
                    .where(PromptVersion.prompt_id == prompt.id)
                    .order_by(PromptVersion.version.desc())
                )
            )
            .scalars()
            .all()
        )
        return page(
            request,
            "workspace/prompt_editor.html",
            "prompts",
            prompt=prompt,
            versions=versions,
            live=next((v for v in versions if v.status == "live"), None),
            body=body,
            variables=variables_in(body),
            missing=missing,
            note=note,
        )
    live_id = await session.scalar(
        select(PromptVersion.id).where(
            PromptVersion.prompt_id == prompt.id, PromptVersion.status == "live"
        )
    )
    version = await add_version(
        session,
        bid(ctx),
        key,
        body,
        author="human",
        status="gating",
        note=note or None,
        created_by=ctx.user_id,
    )
    await audit(session, ctx, "prompt.save", key, version=version.version)
    message = await _gate(session, ctx, "prompt", key, version.id, live_id)
    return RedirectResponse(f"/settings/prompts/{key}?msg={message}", status_code=303)


@router.get("/prompts/{key}/diff", response_class=HTMLResponse)
async def prompt_diff(
    request: Request, key: str, ctx: CtxDep, session: DbDep, a: uuid.UUID, b: str
) -> HTMLResponse:
    prompt = await _prompt(session, key)
    old = await session.get(PromptVersion, a)
    if old is None or old.prompt_id != prompt.id:
        raise HTTPException(404)
    if b == "default":
        new_text, label = defaults()[key].body, "default"
    else:
        new = await session.get(PromptVersion, uuid.UUID(b))
        if new is None or new.prompt_id != prompt.id:
            raise HTTPException(404)
        new_text, label = new.body, f"v{new.version}"
    return render(
        request,
        "workspace/_diff.html",
        text=diff(old.body, new_text, old_label=f"v{old.version}", new_label=label),
    )


@router.post("/prompts/{key}/restore")
async def restore_prompt(
    key: str, ctx: OwnerDep, session: DbDep, source: Annotated[str, Form()]
) -> Response:
    """One click: copy an older version (or the default) into a new version."""
    prompt = await _prompt(session, key)
    if source == "default":
        body, note = defaults()[key].body, "Restored the default"
    else:
        old = await session.get(PromptVersion, uuid.UUID(source))
        if old is None or old.prompt_id != prompt.id:
            raise HTTPException(404)
        body, note = old.body, f"Restored v{old.version}"
    live_id = await session.scalar(
        select(PromptVersion.id).where(
            PromptVersion.prompt_id == prompt.id, PromptVersion.status == "live"
        )
    )
    version = await add_version(
        session,
        bid(ctx),
        key,
        body,
        author="human",
        status="gating",
        note=note,
        created_by=ctx.user_id,
    )
    await audit(session, ctx, "prompt.restore", key, source=source)
    message = await _gate(session, ctx, "prompt", key, version.id, live_id)
    return RedirectResponse(f"/settings/prompts/{key}?msg={message}", status_code=303)


# ---------------------------------------------------------------- roles


@router.get("/roles", response_class=HTMLResponse)
async def roles_tab(request: Request, ctx: CtxDep, session: DbDep) -> HTMLResponse:
    rows = (
        await session.execute(
            select(Role, RoleVersion)
            .join(
                RoleVersion,
                (RoleVersion.role_id == Role.id) & (RoleVersion.status == "live"),
                isouter=True,
            )
            .where(Role.archived_at.is_(None))
            .order_by(Role.builtin.desc(), Role.created_at)
        )
    ).all()
    return page(request, "workspace/roles.html", "roles", rows=rows)


def _role_form(form: Any) -> dict[str, Any]:
    def text(name: str) -> str:
        value = form.get(name)
        return value.strip() if isinstance(value, str) else ""

    model_key = text("model_key")
    return {
        "name": text("name"),
        "goal": text("goal"),
        "prompt": text("prompt"),
        "tone": text("tone"),
        "capabilities": [c for c in CAPABILITIES if form.get(f"cap_{c}")],
        "knowledge_types": [k for k in KNOWLEDGE_TYPES if form.get(f"kt_{k}")],
        "skill_keys": [s.strip() for s in text("skill_keys").split(",") if s.strip()],
        "model_key": model_key if model_key in MODELS else None,
        "handover": {"always": list(ALWAYS_HAND_OVER), "note": text("handover_note")},
    }


@router.get("/roles/new", response_class=HTMLResponse)
async def new_role_page(request: Request, ctx: CtxDep) -> HTMLResponse:
    draft = {
        "name": "",
        "goal": "",
        "prompt": "You are ...",
        "tone": "",
        "capabilities": ["answer_from_brain", "draft_reply"],
        "knowledge_types": KNOWLEDGE_TYPES,
        "skill_keys": [],
        "model_key": None,
        "handover": {"note": ""},
    }
    return page(
        request,
        "workspace/role_editor.html",
        "roles",
        role=None,
        version=draft,
        versions=[],
        capabilities=CAPABILITIES,
        knowledge_types=KNOWLEDGE_TYPES,
        models=MODELS,
    )


@router.post("/roles")
async def create_role(request: Request, ctx: OwnerDep, session: DbDep) -> Response:
    data = _role_form(await request.form())
    if not data["name"] or not data["prompt"]:
        raise HTTPException(400, "A role needs a name and a prompt.")
    key = "-".join(data["name"].lower().split())[:40] + "-" + uuid.uuid4().hex[:4]
    role = Role(business_id=bid(ctx), key=key, name=data["name"])
    session.add(role)
    await session.flush()
    version = RoleVersion(
        business_id=bid(ctx),
        role_id=role.id,
        version=1,
        author="human",
        status="live",
        created_by=ctx.user_id,
        **{k: v for k, v in data.items() if k != "name"},
    )
    session.add(version)
    await audit(session, ctx, "role.create", key)
    # A brand-new role has no live version to compare with: it starts live (no business uses it yet)
    return RedirectResponse(
        f"/settings/roles/{role.id}?msg=Role created. It is available in the chat now.",
        status_code=303,
    )


@router.get("/roles/{role_id}", response_class=HTMLResponse)
async def role_editor(
    request: Request, role_id: uuid.UUID, ctx: CtxDep, session: DbDep
) -> HTMLResponse:
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(404)
    versions = (
        (
            await session.execute(
                select(RoleVersion)
                .where(RoleVersion.role_id == role_id)
                .order_by(RoleVersion.version.desc())
            )
        )
        .scalars()
        .all()
    )
    live = next((v for v in versions if v.status == "live"), versions[0])
    return page(
        request,
        "workspace/role_editor.html",
        "roles",
        role=role,
        version=live,
        versions=versions,
        capabilities=CAPABILITIES,
        knowledge_types=KNOWLEDGE_TYPES,
        models=MODELS,
        notice=request.query_params.get("msg"),
    )


@router.post("/roles/{role_id}")
async def save_role(
    request: Request, role_id: uuid.UUID, ctx: OwnerDep, session: DbDep
) -> Response:
    role = await session.get(Role, role_id)
    if role is None:
        raise HTTPException(404)
    data = _role_form(await request.form())
    role.name = data.pop("name") or role.name
    live_id = await session.scalar(
        select(RoleVersion.id).where(RoleVersion.role_id == role_id, RoleVersion.status == "live")
    )
    next_version = (
        int(
            await session.scalar(
                select(func.max(RoleVersion.version)).where(RoleVersion.role_id == role_id)
            )
            or 0
        )
        + 1
    )
    version = RoleVersion(
        business_id=bid(ctx),
        role_id=role_id,
        version=next_version,
        author="human",
        status="gating",
        created_by=ctx.user_id,
        **data,
    )
    session.add(version)
    await session.flush()
    await audit(session, ctx, "role.save", role.key, version=next_version)
    message = await _gate(session, ctx, "role", role.key, version.id, live_id)
    return RedirectResponse(f"/settings/roles/{role_id}?msg={message}", status_code=303)


@router.post("/roles/{role_id}/archive")
async def archive_role(role_id: uuid.UUID, ctx: OwnerDep, session: DbDep) -> Response:
    from datetime import UTC, datetime

    await session.execute(
        update(Role)
        .where(Role.id == role_id, Role.builtin.is_(False))
        .values(archived_at=datetime.now(UTC))
    )
    return RedirectResponse("/settings/roles", status_code=303)


@router.post("/role-test", response_class=HTMLResponse)
async def test_role(request: Request, ctx: CtxDep) -> HTMLResponse:
    """The test chat beside the editor: the unsaved role answers one question at once."""
    from ask_teach_chat.preview import preview_answer

    form = await request.form()
    data = _role_form(form)
    question = form.get("question")
    if not isinstance(question, str) or not question.strip():
        return HTMLResponse(
            '<div class="notice warn">Type a question or paste an email first.</div>'
        )
    result = await preview_answer(
        bid(ctx), question=question.strip(), role_settings=data, user_id=ctx.user_id
    )
    return render(request, "ask_teach_chat/_preview.html", result=result, question=question.strip())
