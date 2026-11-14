"""The 5 connections (Task 1.2): Claude, Gemini, OpenRouter keys, the Ollama address, Gmail.

Keys are encrypted (core/crypto.py), shown masked, never shown in full again and never logged.
Each connection's Test button makes one small, free call.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.crypto import decrypt, encrypt, mask
from core.llm.providers.anthropic_provider import AnthropicProvider
from core.llm.providers.base import Credentials, Provider
from core.llm.providers.fake import FakeProvider
from core.llm.providers.gemini import GeminiProvider
from core.llm.providers.ollama import OllamaProvider
from core.llm.providers.openrouter import OpenRouterProvider
from core.models import Connection

KINDS = {
    "claude": ("Claude API key", "From the Claude Console. The app runs on Claude."),
    "gemini": ("Gemini API key", "From Google AI Studio, for the 3 Gemini models in the Eval Lab."),
    "openrouter": (
        "OpenRouter API key",
        "From openrouter.ai (your own account): 4 free models and 2 Qwen models.",
    ),
    "ollama": (
        "Local models address",
        "Where Ollama runs on your laptop. From Docker: http://host.docker.internal:11434",
    ),
    "google": (
        "Google account (Gmail)",
        "Read past email threads (gmail.readonly) and send the weekly email (gmail.send).",
    ),
}


def provider_for(kind: str) -> Provider:
    if get_settings().llm_fake:
        return FakeProvider()
    providers: dict[str, Provider] = {
        "claude": AnthropicProvider(),
        "gemini": GeminiProvider(),
        "openrouter": OpenRouterProvider(),
        "ollama": OllamaProvider(),
    }
    return providers[kind]


async def get_connection(session: AsyncSession, kind: str) -> Connection | None:
    return (
        await session.execute(select(Connection).where(Connection.kind == kind))
    ).scalar_one_or_none()


async def save_key(
    session: AsyncSession, business_id: uuid.UUID, kind: str, value: str
) -> Connection:
    value = value.strip()
    connection = await get_connection(session, kind) or Connection(
        business_id=business_id, kind=kind
    )
    if kind == "ollama":
        connection.config = {**(connection.config or {}), "base_url": value.rstrip("/")}
        connection.masked = value.rstrip("/")
        connection.secret_enc = None
    else:
        connection.secret_enc = encrypt(value, business_id=business_id)
        connection.masked = mask(value)
    connection.status = "untested"
    connection.last_test_message = None
    connection.updated_at = datetime.now(UTC)
    session.add(connection)
    await session.flush()
    return connection


def credentials_of(connection: Connection, business_id: uuid.UUID) -> Credentials:
    if connection.kind == "ollama":
        return Credentials(
            base_url=str(connection.config.get("base_url") or get_settings().ollama_url)
        )
    if connection.secret_enc is None:
        return Credentials()
    return Credentials(api_key=decrypt(connection.secret_enc, business_id=business_id))


async def test_connection(session: AsyncSession, business_id: uuid.UUID, kind: str) -> Connection:
    connection = await get_connection(session, kind)
    if connection is None and kind == "ollama":
        connection = await save_key(session, business_id, "ollama", get_settings().ollama_url)
    if connection is None:
        raise ValueError("Add it first, then test it.")
    if kind == "google":
        from core.google import test_gmail

        ok, message = await test_gmail(connection, business_id)
    else:
        ok, message = await provider_for(kind).test(credentials_of(connection, business_id))
    connection.status = "ok" if ok else "error"
    connection.last_test_message = message[:500]
    connection.last_tested_at = datetime.now(UTC)
    await session.flush()
    return connection
