"""Who is signed in, which business they are working in, and the session to use.

Every screen depends on `business_ctx`: it loads the browser's session (cookie -> token hash ->
core.web_sessions), checks the CSRF token on anything that changes data, and gives the route a
database session that sees only the current business (row-level security).
"""

import hmac
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import tenant_session, user_session
from core.models import Business, Membership, User, WebSession
from core.security import sha256_hex

COOKIE = "sid"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class Redirect(Exception):
    def __init__(self, url: str) -> None:
        self.url = url


@dataclass
class Ctx:
    user_id: uuid.UUID
    user_name: str
    user_email: str
    token_hash: str
    csrf: str
    business_id: uuid.UUID | None
    business_name: str
    member_role: str  # owner | member
    businesses: list[tuple[uuid.UUID, str]]

    @property
    def is_owner(self) -> bool:
        return self.member_role == "owner"


async def _load(request: Request) -> Ctx | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    token_hash = sha256_hex(token)
    async with user_session(None) as session:
        web_session = await session.get(WebSession, token_hash)
        if web_session is None or web_session.expires_at < datetime.now(UTC):
            return None
        user = await session.get(User, web_session.user_id)
        if user is None:
            return None
        business_id = web_session.business_id
    async with user_session(user.id) as session:
        # Row-level security on businesses and memberships shows this user's own only
        rows = (
            await session.execute(
                select(Business.id, Business.name, Membership.role)
                .join(Membership, Membership.business_id == Business.id)
                .where(Membership.user_id == user.id)
                .order_by(Business.created_at)
            )
        ).all()
    businesses = [(row.id, row.name) for row in rows]
    roles = {row.id: row.role for row in rows}
    if business_id not in roles:
        business_id = businesses[0][0] if businesses else None
    names = dict(businesses)
    return Ctx(
        user_id=user.id,
        user_name=user.name,
        user_email=user.email,
        token_hash=token_hash,
        csrf=web_session.csrf_token,
        business_id=business_id,
        business_name=names.get(business_id, "") if business_id else "",
        member_role=roles.get(business_id, "member") if business_id else "member",
        businesses=businesses,
    )


async def _check_csrf(request: Request, ctx: Ctx) -> None:
    if request.method in SAFE_METHODS:
        return
    sent = request.headers.get("X-CSRF-Token")
    if sent is None:
        form = await request.form()
        value = form.get("csrf_token")
        sent = value if isinstance(value, str) else None
    if not sent or not hmac.compare_digest(sent, ctx.csrf):
        raise HTTPException(
            status_code=403, detail="The form expired. Reload the page and try again."
        )


async def signed_in(request: Request) -> Ctx:
    ctx = await _load(request)
    if ctx is None:
        raise Redirect("/login")
    await _check_csrf(request, ctx)
    request.state.ctx = ctx
    return ctx


async def business_ctx(ctx: Ctx = Depends(signed_in)) -> Ctx:
    if ctx.business_id is None:
        raise Redirect("/businesses")
    return ctx


async def owner_ctx(ctx: Ctx = Depends(business_ctx)) -> Ctx:
    if not ctx.is_owner:
        raise HTTPException(status_code=403, detail="Only the business owner can do this.")
    return ctx


async def db(ctx: Ctx = Depends(business_ctx)) -> AsyncGenerator[AsyncSession]:
    """A session that sees only the current business; commits when the request succeeds."""
    assert ctx.business_id is not None
    async with tenant_session(ctx.business_id, user_id=ctx.user_id) as session:
        yield session


def bid(ctx: Ctx) -> uuid.UUID:
    assert ctx.business_id is not None
    return ctx.business_id
