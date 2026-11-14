"""Connect Gmail with Google sign-in, asking for the smallest permissions (STACK.md S5).

gmail.readonly reads past threads (Task 2.6); gmail.send sends only the weekly email to the
owner (Task 5.4); openid and email show which account is connected. The refresh token is
encrypted with the business id bound in; access tokens (1 hour) are never stored.
"""

import uuid
from typing import Any
from urllib.parse import urlencode

import httpx

from core.config import get_settings
from core.crypto import decrypt
from core.models import Connection

SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def redirect_uri() -> str:
    return get_settings().public_url.rstrip("/") + "/connections/google/callback"


def configured() -> bool:
    settings = get_settings()
    return bool(settings.google_client_id and settings.google_client_secret.get_secret_value())


def auth_url(state: str) -> str:
    params = {
        "client_id": get_settings().google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",  # we need a refresh token to fetch threads later
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict[str, Any]:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret.get_secret_value(),
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
    response.raise_for_status()
    data: dict[str, Any] = response.json()
    return data


async def access_token(refresh_token: str) -> str:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.post(
            TOKEN_URL,
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret.get_secret_value(),
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    response.raise_for_status()
    return str(response.json()["access_token"])


async def account_email(token: str) -> str:
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {token}"},
        )
    response.raise_for_status()
    return str(response.json().get("emailAddress", ""))


async def test_gmail(connection: Connection, business_id: uuid.UUID) -> tuple[bool, str]:
    if connection.secret_enc is None:
        return False, "Gmail is not connected yet."
    try:
        token = await access_token(decrypt(connection.secret_enc, business_id=business_id))
        email = await account_email(token)
    except httpx.HTTPError as exc:
        return False, f"Google refused: {exc}. Connect Gmail again."
    return True, f"Connected as {email}."


async def revoke(connection: Connection, business_id: uuid.UUID) -> None:
    """Part of deleting a business (D14): Google forgets our access."""
    if connection.secret_enc is None:
        return
    token = decrypt(connection.secret_enc, business_id=business_id)
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            await http.post(REVOKE_URL, params={"token": token})
    except httpx.HTTPError:
        pass  # the row is deleted anyway; Google expires unused tokens
