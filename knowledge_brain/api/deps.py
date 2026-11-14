"""Who is calling: every request carries a business's API key, and the key decides the business.

`Authorization: Bearer btk_...` -> SHA-256 -> core.business_for_api_key() -> the business id,
which the session sets for row-level security. A request can never name another business.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import engine, tenant_session
from core.security import sha256_hex


async def business_id(authorization: Annotated[str | None, Header()] = None) -> uuid.UUID:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Send your API key as: Authorization: Bearer <key>")
    key = authorization.removeprefix("Bearer ").strip()
    async with engine("brain").begin() as connection:
        found = await connection.scalar(
            text("SELECT core.business_for_api_key(:h)"), {"h": sha256_hex(key)}
        )
    if not isinstance(found, uuid.UUID):
        raise HTTPException(401, "Unknown or revoked API key")
    return found


async def acting_user(x_acting_user: Annotated[str | None, Header()] = None) -> uuid.UUID | None:
    """The person behind a request from the web app, recorded on what they create. A label only."""
    try:
        return uuid.UUID(x_acting_user) if x_acting_user else None
    except ValueError:
        return None


async def session(bid: Annotated[uuid.UUID, Depends(business_id)]) -> AsyncGenerator[AsyncSession]:
    async with tenant_session(bid, role="brain") as db:
        yield db


BusinessId = Annotated[uuid.UUID, Depends(business_id)]
UserId = Annotated[uuid.UUID | None, Depends(acting_user)]
Db = Annotated[AsyncSession, Depends(session)]
