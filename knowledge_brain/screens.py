"""The Knowledge screens (screen 6), the source viewer (8) and expert tasks (10).

They run in the web service and reach the Brain's data only through its HTTP API
(core/brain_client.py), like the chat and the Eval Lab: this module imports nothing else from
knowledge_brain.
"""

import html
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from core.brain_api import SourceView
from core.brain_client import BrainClient, BrainError
from core.models import Role
from eval_lab.gate import request_gate
from web.deps import Ctx, bid, business_ctx, db, owner_ctx
from web.state import queue
from web.templating import render

router = APIRouter()
CtxDep = Annotated[Ctx, Depends(business_ctx)]
OwnerDep = Annotated[Ctx, Depends(owner_ctx)]
DbDep = Annotated[AsyncSession, Depends(db)]

TABS = [
    ("Sitemaps", "/knowledge/sitemaps", "sitemaps"),
    ("Page URLs", "/knowledge/page-urls", "page_urls"),
    ("Instructions", "/knowledge/instructions", "instructions"),
    ("Skills", "/knowledge/skills", "skills"),
    ("Past emails", "/knowledge/emails", "emails"),
    ("Documents", "/knowledge/documents", "documents"),
]


async def brain_for(ctx: Ctx = Depends(business_ctx)) -> AsyncGenerator[BrainClient]:
    client = await BrainClient.for_business(bid(ctx), ctx.user_id)
    try:
        yield client
    finally:
        await client.close()


BrainDep = Annotated[BrainClient, Depends(brain_for)]


def page(request: Request, template: str, tab: str, **context: Any) -> HTMLResponse:
    return render(request, template, active="knowledge", tabs=TABS, tab=tab, **context)


def back(url: str, message: str | None = None) -> Response:
    return RedirectResponse(f"{url}?msg={message}" if message else url, status_code=303)


@router.get("/knowledge")
async def knowledge_home() -> Response:
    return RedirectResponse("/knowledge/sitemaps", status_code=303)


# ---------------------------------------------------------------- sitemaps and sources


@router.get("/knowledge/sitemaps", response_class=HTMLResponse)
async def sitemaps(request: Request, ctx: CtxDep, brain: BrainDep) -> HTMLResponse:
    sources: list[dict[str, Any]]
    pending: list[dict[str, Any]]
    try:
        sources = await brain.get("/v1/sources", kind="sitemap")
        pending = await brain.get("/v1/health/pending")
        error = None
    except BrainError as exc:
        sources, pending, error = [], [], exc.detail
    return page(
        request,
        "knowledge/sitemaps.html",
        "sitemaps",
        sources=sources,
        pending_count=len(pending),
        error=error,
        notice=request.query_params.get("msg"),
    )


@router.post("/knowledge/sitemaps/check", response_class=HTMLResponse)
async def check_sitemap(
    ctx: CtxDep, brain: BrainDep, urls: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Checked as it is typed (the first URL of the box)."""
    url = next((line.strip() for line in urls.splitlines() if line.strip()), "")
    if len(url) < 8:
        return HTMLResponse("")
    result = await brain.post("/v1/sources/sitemaps/check", {"url": url})
    kind, text = (
        ("ok", result["message"])
        if result.get("ok")
        else ("bad", result.get("error", "Not a sitemap"))
    )
    return HTMLResponse(f'<div class="notice {kind} small">{html.escape(str(text))}</div>')


@router.post("/knowledge/sitemaps")
async def plan_sitemap(
    ctx: OwnerDep,
    brain: BrainDep,
    urls: Annotated[str, Form()],
    recheck_days: Annotated[int, Form()] = 1,
    miss_threshold: Annotated[int, Form()] = 2,
    action_rule: Annotated[str, Form()] = "quarantine_then_archive",
) -> Response:
    listed = [u.strip() for u in urls.replace(",", "\n").splitlines() if u.strip()]
    try:
        result = await brain.post(
            "/v1/sources/sitemaps",
            {
                "urls": listed,
                "recheck_days": recheck_days,
                "miss_threshold": miss_threshold,
                "action_rule": action_rule,
            },
        )
    except BrainError as exc:
        return back("/knowledge/sitemaps", exc.detail)
    return RedirectResponse(f"/knowledge/sources/{result['source']['id']}/plan", status_code=303)


@router.get("/knowledge/sources/{source_id}/plan", response_class=HTMLResponse)
async def plan_view(
    request: Request, source_id: uuid.UUID, ctx: CtxDep, brain: BrainDep
) -> HTMLResponse:
    source = await brain.get(f"/v1/sources/{source_id}")
    return page(
        request, "knowledge/plan.html", "sitemaps", source=source, plan=source.get("plan") or {}
    )


@router.post("/knowledge/sources/{source_id}/start")
async def start_reading(
    source_id: uuid.UUID,
    ctx: OwnerDep,
    brain: BrainDep,
    starter_pages: Annotated[int | None, Form()] = None,
) -> Response:
    await brain.post(
        f"/v1/sources/{source_id}/start",
        {"starter_pages": starter_pages or None, "continue_after_starter": True},
    )
    return RedirectResponse(f"/knowledge/sources/{source_id}?tab=progress", status_code=303)


@router.post("/knowledge/sources/{source_id}/delete")
async def delete_source(source_id: uuid.UUID, ctx: OwnerDep, brain: BrainDep) -> Response:
    await brain.delete(f"/v1/sources/{source_id}")
    return back("/knowledge/sitemaps", "Removed.")


@router.get("/knowledge/sources/{source_id}", response_class=HTMLResponse)
async def source_detail(
    request: Request,
    source_id: uuid.UUID,
    ctx: CtxDep,
    brain: BrainDep,
    tab: str = "progress",
    level: str | None = None,
    q: str | None = None,
) -> HTMLResponse:
    source = await brain.get(f"/v1/sources/{source_id}")
    data: dict[str, Any] = {}
    if tab == "pages":
        data["pages"] = await brain.get(
            f"/v1/sources/{source_id}/pages", level=level, q=q, limit=200
        )
    elif tab == "runs":
        data["runs"] = await brain.get("/v1/runs", source_id=str(source_id))
    elif tab == "health":
        data["pending"] = [
            p
            for p in await brain.get("/v1/health/pending")
            if p.get("source_label") == source["label"]
        ]
        data["watch"] = await brain.get(f"/v1/sources/{source_id}/pages", level="watch", limit=200)
        data["quarantined"] = await brain.get(
            f"/v1/sources/{source_id}/pages", level="quarantined", limit=200
        )
    elif tab == "changes":
        data["changes"] = await brain.get("/v1/changes", limit=100)
    elif tab == "archived":
        data["archived"] = await brain.get(
            f"/v1/sources/{source_id}/pages", level="archived", limit=500
        )
    else:
        data["progress"] = await brain.get(f"/v1/sources/{source_id}/progress")
    return page(
        request,
        "knowledge/source.html",
        "sitemaps" if source["kind"] == "sitemap" else "page_urls",
        source=source,
        stab=tab,
        level=level,
        q=q,
        notice=request.query_params.get("msg"),
        **data,
    )


@router.get("/knowledge/sources/{source_id}/progress", response_class=HTMLResponse)
async def progress_partial(
    request: Request, source_id: uuid.UUID, ctx: CtxDep, brain: BrainDep
) -> HTMLResponse:
    return render(
        request,
        "knowledge/_progress.html",
        source_id=source_id,
        progress=await brain.get(f"/v1/sources/{source_id}/progress"),
    )


@router.post("/knowledge/sources/{source_id}/recheck")
async def recheck(source_id: uuid.UUID, ctx: OwnerDep, brain: BrainDep) -> Response:
    await brain.post(f"/v1/sources/{source_id}/recheck")
    return back(f"/knowledge/sources/{source_id}", "Recheck queued.")


@router.post("/knowledge/sources/{source_id}/settings")
async def source_settings(
    source_id: uuid.UUID,
    ctx: OwnerDep,
    brain: BrainDep,
    recheck_days: Annotated[int, Form()],
    miss_threshold: Annotated[int, Form()],
    action_rule: Annotated[str, Form()],
) -> Response:
    await brain.patch(
        f"/v1/sources/{source_id}",
        {
            "recheck_days": recheck_days,
            "miss_threshold": miss_threshold,
            "action_rule": action_rule,
        },
    )
    return RedirectResponse(
        f"/knowledge/sources/{source_id}?tab=health&msg=Saved.", status_code=303
    )


@router.post("/knowledge/health/apply")
async def apply_health(request: Request, ctx: OwnerDep, brain: BrainDep) -> Response:
    form = await request.form()
    ids = [str(v) for v in form.getlist("page_id")]
    result = await brain.post("/v1/health/apply", {"page_ids": ids})
    return back(
        str(form.get("next") or "/knowledge/sitemaps"), f"Applied {result['applied']} action(s)."
    )


@router.post("/knowledge/pages/{page_id}/restore")
async def restore_page(
    page_id: uuid.UUID,
    ctx: OwnerDep,
    brain: BrainDep,
    next_url: Annotated[str, Form()] = "/knowledge/sitemaps",
) -> Response:
    await brain.post(f"/v1/pages/{page_id}/restore")
    return back(next_url, "Restored: the page is back in answers.")


@router.get("/knowledge/runs/{run_id}", response_class=HTMLResponse)
async def run_view(
    request: Request, run_id: uuid.UUID, ctx: CtxDep, brain: BrainDep
) -> HTMLResponse:
    return page(
        request, "knowledge/run.html", "sitemaps", run=await brain.get(f"/v1/runs/{run_id}")
    )


@router.get("/knowledge/pages/{page_id}", response_class=HTMLResponse)
async def page_view(
    request: Request, page_id: uuid.UUID, ctx: CtxDep, brain: BrainDep
) -> HTMLResponse:
    return page(
        request,
        "knowledge/page.html",
        "sitemaps",
        page_data=await brain.get(f"/v1/pages/{page_id}"),
    )


@router.get("/knowledge/pages/{page_id}/diff", response_class=HTMLResponse)
async def page_diff(
    request: Request, page_id: uuid.UUID, a: int, b: int, ctx: CtxDep, brain: BrainDep
) -> HTMLResponse:
    result = await brain.get(f"/v1/pages/{page_id}/diff", a=a, b=b)
    return render(request, "workspace/_diff.html", text=result["diff"])


# ---------------------------------------------------------------- page URLs and documents


@router.get("/knowledge/page-urls", response_class=HTMLResponse)
async def page_urls(request: Request, ctx: CtxDep, brain: BrainDep) -> HTMLResponse:
    return page(
        request,
        "knowledge/page_urls.html",
        "page_urls",
        sources=await brain.get("/v1/sources", kind="page_url"),
        notice=request.query_params.get("msg"),
    )


@router.post("/knowledge/page-urls")
async def add_page_url(ctx: OwnerDep, brain: BrainDep, url: Annotated[str, Form()]) -> Response:
    try:
        await brain.post("/v1/page-urls", {"url": url.strip()})
    except BrainError as exc:
        return back("/knowledge/page-urls", exc.detail)
    return back("/knowledge/page-urls", "Added: it is being read now.")


@router.get("/knowledge/documents", response_class=HTMLResponse)
async def documents(request: Request, ctx: CtxDep, brain: BrainDep) -> HTMLResponse:
    return page(
        request,
        "knowledge/documents.html",
        "documents",
        documents=await brain.get("/v1/documents"),
        notice=request.query_params.get("msg"),
    )


@router.post("/knowledge/documents")
async def upload_document(
    ctx: OwnerDep, brain: BrainDep, file: Annotated[UploadFile, File()]
) -> Response:
    data = await file.read()
    try:
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
        return back("/knowledge/documents", exc.detail)
    return back("/knowledge/documents", f"{file.filename} added.")


# ---------------------------------------------------------------- instructions


async def _roles(session: AsyncSession) -> list[Role]:
    from sqlalchemy import select

    return list(
        (
            await session.execute(
                select(Role).where(Role.archived_at.is_(None)).order_by(Role.created_at)
            )
        ).scalars()
    )


@router.get("/knowledge/instructions", response_class=HTMLResponse)
async def instructions(
    request: Request, ctx: CtxDep, brain: BrainDep, session: DbDep
) -> HTMLResponse:
    return page(
        request,
        "knowledge/instructions.html",
        "instructions",
        items=await brain.get("/v1/instructions"),
        roles=await _roles(session),
        notice=request.query_params.get("msg"),
    )


@router.post("/knowledge/instructions")
async def save_instruction(request: Request, ctx: OwnerDep, brain: BrainDep) -> Response:
    form = await request.form()
    body = {
        "title": str(form.get("title", "")),
        "body": str(form.get("body", "")),
        "enabled": form.get("enabled") == "true",
        "all_roles": form.get("all_roles") == "true",
        "role_ids": [str(v) for v in form.getlist("role_ids")],
    }
    instruction_id = form.get("id")
    if instruction_id:
        await brain.put(f"/v1/instructions/{instruction_id}", body)
    else:
        await brain.post("/v1/instructions", body)
    return back("/knowledge/instructions", "Saved.")


@router.post("/knowledge/instructions/{instruction_id}/delete")
async def delete_instruction(instruction_id: uuid.UUID, ctx: OwnerDep, brain: BrainDep) -> Response:
    await brain.delete(f"/v1/instructions/{instruction_id}")
    return back("/knowledge/instructions", "Deleted.")


# ---------------------------------------------------------------- skills (also Settings > Skills)


@router.get("/knowledge/skills", response_class=HTMLResponse)
async def skills(request: Request, ctx: CtxDep, brain: BrainDep) -> HTMLResponse:
    return page(
        request,
        "knowledge/skills.html",
        "skills",
        skills=await brain.get("/v1/skills"),
        proposals=await brain.get("/v1/skill-proposals"),
        notice=request.query_params.get("msg"),
    )


@router.get("/knowledge/skills/new", response_class=HTMLResponse)
async def new_skill(request: Request, ctx: CtxDep) -> HTMLResponse:
    template = "# Skill name\n\n## When to use\nThe kinds of questions this is for.\n\n## Steps\n1. ...\n2. ...\n\n## Examples\n**Customer:** ...\n**Reply:** ...\n"
    return page(
        request,
        "knowledge/skill_editor.html",
        "skills",
        skill=None,
        markdown=template,
        versions=[],
        proposals=[],
    )


async def _gate_skill(
    session: AsyncSession,
    ctx: Ctx,
    brain: BrainClient,
    skill_id: str,
    skill_key: str,
    version_id: str,
    live_id: str | None,
) -> str:
    gate, needs_run = await request_gate(
        session,
        bid(ctx),
        target_kind="skill",
        target_key=skill_key,
        candidate_ref=version_id,
        baseline_ref=live_id,
        user_id=ctx.user_id,
    )
    if needs_run:
        await queue().enqueue(
            "evals:run_gate", queue="web", business_id=str(bid(ctx)), gate_id=str(gate.id)
        )
        return "Saved. The release gate is checking it; it goes live if no score drops."
    await brain.post(f"/v1/skills/{skill_id}/versions/{version_id}/status", {"status": "live"})
    return f"Saved and live. {gate.reason}"


@router.post("/knowledge/skills")
async def create_skill(
    ctx: OwnerDep, brain: BrainDep, session: DbDep, markdown: Annotated[str, Form()]
) -> Response:
    created = await brain.post("/v1/skills", {"markdown": markdown, "status": "gating"})
    detail = await brain.get(f"/v1/skills/{created['skill_id']}")
    message = await _gate_skill(
        session,
        ctx,
        brain,
        str(created["skill_id"]),
        detail["key"],
        str(created["version"]["id"]),
        None,
    )
    return RedirectResponse(
        f"/knowledge/skills/{created['skill_id']}?msg={message}", status_code=303
    )


@router.get("/knowledge/skills/{skill_id}", response_class=HTMLResponse)
async def skill_editor(
    request: Request, skill_id: uuid.UUID, ctx: CtxDep, brain: BrainDep
) -> HTMLResponse:
    skill = await brain.get(f"/v1/skills/{skill_id}")
    live = next(
        (v for v in skill["versions"] if v["status"] == "live"),
        skill["versions"][0] if skill["versions"] else None,
    )
    return page(
        request,
        "knowledge/skill_editor.html",
        "skills",
        skill=skill,
        live=live,
        markdown=live["markdown"] if live else "",
        versions=skill["versions"],
        proposals=skill["proposals"],
        notice=request.query_params.get("msg"),
    )


@router.post("/knowledge/skills/{skill_id}")
async def save_skill(
    skill_id: uuid.UUID,
    ctx: OwnerDep,
    brain: BrainDep,
    session: DbDep,
    markdown: Annotated[str, Form()],
    note: Annotated[str, Form()] = "",
) -> Response:
    skill = await brain.get(f"/v1/skills/{skill_id}")
    live = next((v for v in skill["versions"] if v["status"] == "live"), None)
    version = await brain.post(
        f"/v1/skills/{skill_id}/versions",
        {"markdown": markdown, "status": "gating", "note": note or None},
    )
    message = await _gate_skill(
        session,
        ctx,
        brain,
        str(skill_id),
        skill["key"],
        str(version["id"]),
        str(live["id"]) if live else None,
    )
    return RedirectResponse(f"/knowledge/skills/{skill_id}?msg={message}", status_code=303)


@router.post("/knowledge/skills/{skill_id}/restore")
async def restore_skill(
    skill_id: uuid.UUID,
    ctx: OwnerDep,
    brain: BrainDep,
    session: DbDep,
    version_id: Annotated[str, Form()],
) -> Response:
    skill = await brain.get(f"/v1/skills/{skill_id}")
    live = next((v for v in skill["versions"] if v["status"] == "live"), None)
    version = await brain.post(
        f"/v1/skills/{skill_id}/restore", {"version_id": version_id, "status": "gating"}
    )
    message = await _gate_skill(
        session,
        ctx,
        brain,
        str(skill_id),
        skill["key"],
        str(version["id"]),
        str(live["id"]) if live else None,
    )
    return RedirectResponse(f"/knowledge/skills/{skill_id}?msg={message}", status_code=303)


@router.post("/knowledge/skill-draft", response_class=HTMLResponse)
async def draft_skill(
    request: Request, ctx: CtxDep, brain: BrainDep, description: Annotated[str, Form()]
) -> HTMLResponse:
    try:
        draft = await brain.post("/v1/skills/draft", {"description": description})
    except BrainError as exc:
        return HTMLResponse(f'<div class="notice bad">{exc.detail}</div>')
    return render(request, "knowledge/_draft.html", draft=draft)


@router.post("/knowledge/skill-test", response_class=HTMLResponse)
async def test_skill(
    request: Request,
    ctx: CtxDep,
    markdown: Annotated[str, Form()],
    question: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """The test chat beside the skill editor: answers with the unsaved skill text."""
    from ask_teach_chat.preview import preview_answer

    if not question.strip():
        return HTMLResponse('<div class="notice warn">Type a question first.</div>')
    result = await preview_answer(
        bid(ctx), question=question.strip(), skill_markdown=markdown, user_id=ctx.user_id
    )
    return render(request, "ask_teach_chat/_preview.html", result=result, question=question.strip())


@router.post("/knowledge/proposals/{proposal_id}/{decision}")
async def decide_proposal(
    proposal_id: uuid.UUID,
    decision: str,
    ctx: OwnerDep,
    brain: BrainDep,
    session: DbDep,
    next_url: Annotated[str, Form()] = "/knowledge/skills",
) -> Response:
    if decision == "reject":
        await brain.post(f"/v1/skill-proposals/{proposal_id}/reject")
        return back(next_url, "Rejected.")
    result = await brain.post(f"/v1/skill-proposals/{proposal_id}/approve", {"status": "gating"})
    skill = await brain.get(f"/v1/skills/{result['skill_id']}")
    live = next((v for v in skill["versions"] if v["status"] == "live"), None)
    message = await _gate_skill(
        session,
        ctx,
        brain,
        str(result["skill_id"]),
        skill["key"],
        str(result["version"]["id"]),
        str(live["id"]) if live else None,
    )
    return back(next_url, f"Approved: a new skill version. {message}")


# ---------------------------------------------------------------- past emails


@router.get("/knowledge/emails", response_class=HTMLResponse)
async def emails(request: Request, ctx: CtxDep, brain: BrainDep) -> HTMLResponse:
    return page(
        request,
        "knowledge/emails.html",
        "emails",
        threads=await brain.get("/v1/email-threads"),
        notice=request.query_params.get("msg"),
    )


@router.post("/knowledge/emails/sync")
async def sync_emails(ctx: OwnerDep, brain: BrainDep) -> Response:
    await brain.post("/v1/gmail/sync")
    return back("/knowledge/emails", "Fetching threads from Gmail; reload in a minute.")


@router.post("/knowledge/emails/improve")
async def improve_skills(ctx: OwnerDep, brain: BrainDep) -> Response:
    await brain.post("/v1/email-notes/improve")
    return back(
        "/knowledge/skills",
        "Claude Opus is reading your notes; its proposals appear here for approval.",
    )


@router.get("/knowledge/emails/{thread_id}", response_class=HTMLResponse)
async def email_thread(
    request: Request, thread_id: uuid.UUID, ctx: CtxDep, brain: BrainDep
) -> HTMLResponse:
    return page(
        request,
        "knowledge/thread.html",
        "emails",
        thread=await brain.get(f"/v1/email-threads/{thread_id}"),
        notice=request.query_params.get("msg"),
    )


@router.post("/knowledge/emails/{thread_id}/notes")
async def add_notes(
    request: Request, thread_id: uuid.UUID, ctx: CtxDep, brain: BrainDep
) -> Response:
    form = await request.form()
    added = 0
    for key, value in form.multi_items():
        if not key.startswith("note_") or not str(value).strip():
            continue
        target = key.removeprefix("note_")
        await brain.post(
            f"/v1/email-threads/{thread_id}/notes",
            {"body": str(value).strip(), "message_id": None if target == "thread" else target},
        )
        added += 1
    return back(f"/knowledge/emails/{thread_id}", f"{added} note(s) saved.")


# ---------------------------------------------------------------- source viewer and expert tasks


@router.get("/sources/{chunk_id}", response_class=HTMLResponse)
async def source_view(
    request: Request, chunk_id: int, ctx: CtxDep, brain: BrainDep
) -> HTMLResponse:
    view = SourceView.model_validate(await brain.get(f"/v1/chunks/{chunk_id}"))
    return render(request, "knowledge/source_view.html", active="chat", view=view)


@router.get("/sources/images/{image_id}")
async def source_image(image_id: uuid.UUID, ctx: CtxDep, brain: BrainDep) -> Response:
    response = await brain.get_raw(f"/v1/images/{image_id}/file")
    return Response(
        response.content,
        media_type=response.headers.get("content-type", "image/png"),
        status_code=response.status_code,
    )


@router.get("/experts", response_class=HTMLResponse)
async def experts(
    request: Request, ctx: CtxDep, brain: BrainDep, status: str = "open"
) -> HTMLResponse:
    tabs = [
        ("Open", "/experts?status=open", "open"),
        ("Answered", "/experts?status=answered", "answered"),
        ("Dismissed", "/experts?status=dismissed", "dismissed"),
    ]
    return render(
        request,
        "knowledge/experts.html",
        active="experts",
        tabs=tabs,
        tab=status,
        tasks=await brain.get("/v1/expert-tasks", status=status),
        notice=request.query_params.get("msg"),
    )


@router.post("/experts/{task_id}/answer")
async def answer_expert(
    task_id: uuid.UUID, ctx: CtxDep, brain: BrainDep, answer: Annotated[str, Form()]
) -> Response:
    await brain.post(f"/v1/expert-tasks/{task_id}/answer", {"answer": answer})
    return back("/experts", "Answered: it is now knowledge the agent cites.")


@router.post("/experts/{task_id}/dismiss")
async def dismiss_expert(task_id: uuid.UUID, ctx: CtxDep, brain: BrainDep) -> Response:
    await brain.post(f"/v1/expert-tasks/{task_id}/dismiss")
    return back("/experts", "Dismissed.")
