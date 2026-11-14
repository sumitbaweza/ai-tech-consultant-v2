"""Accounts and businesses (Task 1.1): sign-up, creating a business with everything it starts
with, keys for the Brain's API, and deleting a business completely.

Functions take a session; the caller decides which business the session works for (row-level
security), so these functions never pass a business id around unchecked.
"""

import re
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.crypto import decrypt, encrypt
from core.models import (
    ApiKey,
    Business,
    Membership,
    Role,
    RoleVersion,
    RoutingRule,
    User,
)
from core.prompts import seed_business_prompts
from core.security import hash_password, new_token, sha256_hex
from workspace.defaults import ALWAYS_HAND_OVER, DEFAULT_ROUTING, default_roles

API_KEY_PREFIX = "btk_"


class AccountError(ValueError):
    """Something the person can fix: an email already used, a weak password."""


@dataclass(frozen=True)
class NewApiKey:
    id: uuid.UUID
    key: str  # shown once, never stored in clear


def normalise_email(email: str) -> str:
    return email.strip().lower()


def check_password(password: str) -> None:
    if len(password) < 8:
        raise AccountError("Use at least 8 characters for the password.")


async def create_user(session: AsyncSession, *, email: str, name: str, password: str) -> User:
    email = normalise_email(email)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise AccountError("That doesn't look like an email address.")
    check_password(password)
    exists = await session.scalar(select(func.count()).select_from(User).where(User.email == email))
    if exists:
        raise AccountError("An account with this email already exists. Log in instead.")
    user = User(email=email, name=name.strip() or email, password_hash=hash_password(password))
    session.add(user)
    await session.flush()
    return user


def _new_slug(name: str) -> str:
    # A random suffix rather than a lookup: row-level security hides other businesses' slugs,
    # so "is this slug taken?" can't be asked. 6 hex characters make a clash negligible.
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "business"
    return f"{base}-{secrets.token_hex(3)}"


async def create_business(
    session: AsyncSession, *, business_id: uuid.UUID, owner: User, name: str
) -> Business:
    """A new business with its owner, every default prompt, the 3 roles, the routing table and
    the web app's own Brain API key. The session must be working for `business_id`."""
    business = Business(
        id=business_id,
        name=name.strip() or "My business",
        slug=_new_slug(name),
        created_by=owner.id,
    )
    session.add(business)
    await session.flush()
    session.add(Membership(business_id=business_id, user_id=owner.id, role="owner"))
    await seed_business_prompts(session, business_id)
    for role in default_roles():
        row = Role(business_id=business_id, key=role.key, name=role.name, builtin=True)
        session.add(row)
        await session.flush()
        session.add(
            RoleVersion(
                business_id=business_id,
                role_id=row.id,
                version=1,
                goal=role.goal,
                prompt=role.prompt,
                tone=role.tone,
                capabilities=list(role.capabilities),
                knowledge_types=list(role.knowledge_types),
                skill_keys=[],
                model_key=None,
                handover={"always": list(ALWAYS_HAND_OVER), "note": role.handover_note},
                author="default",
                status="live",
            )
        )
    for (job, complexity), model_key in DEFAULT_ROUTING.items():
        session.add(
            RoutingRule(
                business_id=business_id, job=job, complexity=complexity, model_key=model_key
            )
        )
    internal = await create_api_key(
        session, business_id, name="Web app (internal)", user_id=owner.id
    )
    business.internal_key_enc = encrypt(internal.key, business_id=business_id)
    await session.flush()
    return business


async def create_api_key(
    session: AsyncSession, business_id: uuid.UUID, *, name: str, user_id: uuid.UUID | None
) -> NewApiKey:
    key = new_token(API_KEY_PREFIX)
    row = ApiKey(
        business_id=business_id,
        name=name,
        prefix=key[:10],
        key_hash=sha256_hex(key),
        created_by=user_id,
    )
    session.add(row)
    await session.flush()
    return NewApiKey(id=row.id, key=key)


def internal_brain_key(business: Business) -> str:
    """The key the web app uses to call the Brain for this business."""
    if business.internal_key_enc is None:
        raise RuntimeError("This business has no internal Brain key")
    return decrypt(business.internal_key_enc, business_id=business.id)


async def create_business_for_owner(
    session: AsyncSession,
    *,
    owner_email: str,
    owner_name: str,
    password: str,
    business_name: str,
) -> uuid.UUID:
    """Sign-up in one step, for the seed script and tests (an owner session, no RLS)."""
    user = await create_user(session, email=owner_email, name=owner_name, password=password)
    business_id = uuid.uuid4()
    await create_business(session, business_id=business_id, owner=user, name=business_name)
    return business_id


async def business_ids_for_user(
    session: AsyncSession, user_id: uuid.UUID
) -> list[tuple[uuid.UUID, str, str]]:
    """(business id, name, role) for every business the user belongs to."""
    rows = await session.execute(
        select(Business.id, Business.name, Membership.role)
        .join(Membership, Membership.business_id == Business.id)
        .where(Membership.user_id == user_id)
        .order_by(Business.created_at)
    )
    return [(row.id, row.name, row.role) for row in rows]
