"""Embeddings from Voyage AI (voyage-4-lite, 1,024 numbers per text), STACK.md M1.

Voyage is a platform-level service, not one of a business's 5 connections: its key is the
VOYAGE_API_KEY environment variable. Without it, or in local-only mode, nothing is embedded and
search uses keywords and typo fixing only. Each request is logged to core.model_calls like a
model call, so embedding costs appear in the same ledger (about $0.045 per 1,000 pages).
"""

import asyncio
import time
import uuid
from decimal import Decimal
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel

from core.config import get_settings
from core.db import tenant_session
from core.models import ModelCall
from knowledge_brain.crawler.fetch import RETRY_STATUSES, backoff, parse_retry_after
from knowledge_brain.models import EMBEDDING_DIMENSIONS

VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
EMBEDDING_MODEL = "voyage-4-lite"
PRICE_PER_MILLION_TOKENS = Decimal("0.02")  # the first 200M tokens are free
BATCH_SIZE = 128
MAX_ATTEMPTS = 5
InputType = Literal["document", "query"]


class EmbeddingError(Exception):
    """Voyage couldn't embed the texts, even after retries."""


class Embedder(Protocol):
    model: str

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]: ...


class _Embedding(BaseModel):
    embedding: list[float]
    index: int


class _Usage(BaseModel):
    total_tokens: int


class _Response(BaseModel):
    data: list[_Embedding]
    usage: _Usage


class Voyage:
    def __init__(
        self,
        business_id: uuid.UUID,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        model: str = EMBEDDING_MODEL,
    ) -> None:
        self.model = model
        self.business_id = business_id
        self._api_key = api_key
        self._transport = transport

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(timeout=60, transport=self._transport) as client:
            for start in range(0, len(texts), BATCH_SIZE):
                batch = texts[start : start + BATCH_SIZE]
                vectors.extend(await self._embed_batch(client, batch, input_type))
        return vectors

    async def _embed_batch(
        self, client: httpx.AsyncClient, texts: list[str], input_type: InputType
    ) -> list[list[float]]:
        body = {
            "input": texts,
            "model": self.model,
            "input_type": input_type,
            "output_dimension": EMBEDDING_DIMENSIONS,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            try:
                response = await client.post(VOYAGE_URL, json=body, headers=headers)
            except httpx.TransportError as error:
                if attempt == MAX_ATTEMPTS:
                    await self._log(input_type, 0, started, f"Network error: {error}")
                    raise EmbeddingError(f"Network error after {attempt} tries") from error
                await asyncio.sleep(backoff(attempt))
                continue
            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                wait = parse_retry_after(response.headers.get("retry-after")) or 0.0
                await asyncio.sleep(max(backoff(attempt), wait))
                continue
            if response.status_code != 200:
                await self._log(input_type, 0, started, f"HTTP {response.status_code}")
                raise EmbeddingError(f"Voyage answered HTTP {response.status_code}")
            parsed = _Response.model_validate_json(response.content)
            await self._log(input_type, parsed.usage.total_tokens, started, None)
            return [item.embedding for item in sorted(parsed.data, key=lambda item: item.index)]
        raise AssertionError("unreachable")

    async def _log(
        self, input_type: InputType, tokens: int, started: float, error: str | None
    ) -> None:
        cost = (tokens * PRICE_PER_MILLION_TOKENS / 1_000_000).quantize(Decimal("0.000001"))
        async with tenant_session(self.business_id, role="brain") as session:
            session.add(
                ModelCall(
                    business_id=self.business_id,
                    step=f"brain.embed_{input_type}",
                    job="embed",
                    provider="voyage",
                    model_key=self.model,
                    provider_model=self.model,
                    input_tokens=tokens,
                    cost_usd=cost,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    status="ok" if error is None else "error",
                    error=error,
                )
            )


def embedder_for(business_id: uuid.UUID, *, local_only: bool) -> Embedder | None:
    """Voyage when its key is set and the business isn't in local-only mode; else None."""
    key = get_settings().voyage_api_key.get_secret_value()
    if not key or local_only:
        return None
    return Voyage(business_id, key)
