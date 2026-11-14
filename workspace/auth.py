"""Sign up, log in, log out, and the list of businesses (Task 1.1, screens 1-3)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import delete, select

from core.config import get_settings
from core.db import tenant_session, user_session
from core.models import Membership, User, WebSession
from core.security import new_token, sha256_hex, verify_password
from web.deps import COOKIE, Ctx, signed_in
from web.templating import render
from workspace.service import AccountError, create_business, create_user, normalise_email

router = APIRouter()
SESSION_DAYS = 14


async def _start_session(
    response: Response, user_id: uuid.UUID, business_id: uuid.UUID | None
) -> None:
    token = new_token()
    async with user_session(user_id) as session:
        session.add(
            WebSession(
                token_hash=sha256_hex(token),
                user_id=user_id,
                business_id=business_id,
                csrf_token=new_token(),
                expires_at=datetime.now(UTC) + timedelta(days=SESSION_DAYS),
            )
        )
    response.set_cookie(
        COOKIE,
        token,
        max_age=SESSION_DAYS * 86400,
        httponly=True,  # scripts can't read it
        samesite="lax",  # not sent on cross-site form posts
        secure=get_settings().public_url.startswith("https"),
    )


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request) -> HTMLResponse:
    return render(request, "workspace/signup.html", title="Sign up")


@router.post("/signup")
async def signup(
    request: Request,
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    business_name: Annotated[str, Form()],
) -> Response:
    try:
        async with user_session(None) as session:
            user = await create_user(session, email=email, name=name, password=password)
    except AccountError as exc:
        return render(
            request,
            "workspace/signup.html",
            status_code=400,
            error=str(exc),
            form={"name": name, "email": email, "business_name": business_name},
        )
    business_id = uuid.uuid4()
    # The new business's id is set for the transaction, so row-level security accepts its rows
    async with tenant_session(business_id, user_id=user.id) as session:
        await create_business(session, business_id=business_id, owner=user, name=business_name)
    response = RedirectResponse("/setup", status_code=303)
    await _start_session(response, user.id, business_id)
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return render(request, "workspace/login.html", title="Log in")


@router.post("/login")
async def login(
    request: Request, email: Annotated[str, Form()], password: Annotated[str, Form()]
) -> Response:
    async with user_session(None) as session:
        user = (
            await session.execute(select(User).where(User.email == normalise_email(email)))
        ).scalar_one_or_none()
        valid = user is not None and verify_password(password, user.password_hash)
        if user is not None and valid:
            user.last_login_at = datetime.now(UTC)
    if user is None or not valid:
        return render(
            request,
            "workspace/login.html",
            status_code=400,
            error="Wrong email or password.",
            form={"email": email},
        )
    async with user_session(user.id) as session:
        first = await session.scalar(
            select(Membership.business_id)
            .where(Membership.user_id == user.id)
            .order_by(Membership.created_at)
            .limit(1)
        )
    response = RedirectResponse("/" if first else "/businesses", status_code=303)
    await _start_session(response, user.id, first)
    return response


@router.post("/logout")
async def logout(ctx: Annotated[Ctx, Depends(signed_in)]) -> Response:
    async with user_session(ctx.user_id) as session:
        await session.execute(delete(WebSession).where(WebSession.token_hash == ctx.token_hash))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@router.get("/businesses", response_class=HTMLResponse)
async def businesses(request: Request, ctx: Annotated[Ctx, Depends(signed_in)]) -> HTMLResponse:
    return render(request, "workspace/businesses.html", title="Your businesses")


@router.post("/businesses")
async def new_business(
    request: Request, ctx: Annotated[Ctx, Depends(signed_in)], name: Annotated[str, Form()]
) -> Response:
    business_id = uuid.uuid4()
    async with user_session(ctx.user_id) as session:
        user = await session.get(User, ctx.user_id)
    assert user is not None
    async with tenant_session(business_id, user_id=ctx.user_id) as session:
        await create_business(session, business_id=business_id, owner=user, name=name)
    await _switch(ctx, business_id)
    return RedirectResponse("/setup", status_code=303)


async def _switch(ctx: Ctx, business_id: uuid.UUID) -> None:
    async with user_session(ctx.user_id) as session:
        session_row = await session.get(WebSession, ctx.token_hash)
        if session_row is not None:
            session_row.business_id = business_id


@router.post("/businesses/switch")
async def switch(
    ctx: Annotated[Ctx, Depends(signed_in)], business_id: Annotated[uuid.UUID, Form()]
) -> Response:
    if business_id in {bid for bid, _ in ctx.businesses}:  # only a business the user belongs to
        await _switch(ctx, business_id)
    return RedirectResponse("/", status_code=303)
