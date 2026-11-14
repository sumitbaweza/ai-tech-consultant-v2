"""What goes into a model call and what comes out, the same for all 4 providers."""

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel


@dataclass(frozen=True)
class ImagePart:
    data: bytes
    media_type: str  # image/png, image/jpeg, image/webp, image/gif


@dataclass(frozen=True)
class ChatTurn:
    role: Literal["user", "assistant"]
    text: str
    images: tuple[ImagePart, ...] = ()


@dataclass
class LlmRequest:
    """One model call. Text is masked for personal data by the client before it is sent."""

    business_id: uuid.UUID
    step: str  # what the app is doing, e.g. "chat.write_answer" (logged)
    job: str  # the router's job: tag_job, find_facts, write_answer, read_screenshot, ...
    system: list[str]  # system prompt blocks, in order; stable blocks first (caching)
    messages: list[ChatTurn]
    # A Pydantic model: the reply must be JSON matching it (structured outputs)
    schema: type[BaseModel] | None = None
    max_tokens: int = 4000
    effort: Literal["low", "medium", "high"] | None = None
    # Cache everything up to the end of the system blocks (Claude prompt caching)
    cache_system: bool = True
    # Force a model; otherwise the router picks one (Settings, routing table)
    model_key: str | None = None
    complexity: Literal["low", "medium", "high"] | None = None
    trace_id: uuid.UUID | None = None
    prompt_key: str | None = None
    prompt_version_id: uuid.UUID | None = None
    role_version_id: uuid.UUID | None = None
    # True when the text holds a real customer's words: then free OpenRouter models and
    # Gemini (whose free tiers may keep prompts) are skipped unless the business allows it
    real_customer_data: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class ProviderReply:
    """What a provider returns, before parsing and pricing."""

    text: str
    usage: Usage
    stop_reason: str  # end_turn | max_tokens | refusal | error
    served_model: str  # the model that answered (a refusal fallback can change it)
    refusal_category: str | None = None


@dataclass
class RouteInfo:
    """The router's decision, logged with every call (Task 4.3)."""

    token_count: int
    counted_by: Literal["api", "estimate"]
    complexity: str | None
    picked: str
    reason: str
    escalated_from: list[str] = field(default_factory=list[str])
    fallback_from: list[str] = field(default_factory=list[str])

    def as_dict(self) -> dict[str, Any]:
        return {
            "token_count": self.token_count,
            "counted_by": self.counted_by,
            "complexity": self.complexity,
            "picked": self.picked,
            "reason": self.reason,
            "escalated_from": self.escalated_from,
            "fallback_from": self.fallback_from,
        }


@dataclass
class LlmResult:
    text: str
    parsed: BaseModel | None
    model_key: str
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    route: RouteInfo
    stop_reason: str
    call_id: int | None = None


class LlmError(RuntimeError):
    """A model call failed after retries and fallbacks. Jobs let Procrastinate retry later."""


class ProviderUnavailable(LlmError):
    """The provider didn't answer (down, timeout, 5xx, rate limit after retries)."""


class ProviderRejected(LlmError):
    """The provider refused the request itself (bad key, bad request): retrying won't help."""


class MissingConnection(LlmError):
    """The business hasn't added the key (or Ollama address) this model needs."""


class LocalOnlyViolation(LlmError):
    """Local-only mode is on and the call would have used a paid API."""


class ModelRefused(LlmError):
    """The model (and its server-side fallback) declined for safety reasons."""


class BadReply(LlmError):
    """Every model tried gave a reply that failed to parse or failed the quick check."""
