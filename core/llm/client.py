"""The one door to every model: all 14 models, all 4 providers, every call logged.

For each call, in order:
1. Mask personal data (phone, card, address, email) in everything sent (Task 1.5).
2. Route it (core/llm/router.py): tag the job, pick the model, count the tokens, apply the
   100,000-token rule for Haiku, respect local-only mode and keep customer text away from
   free and Gemini models.
3. Call the provider with the business's own key. A busy free model falls back to the next
   one; a paid provider that is down raises ProviderUnavailable, so the job that made the call
   fails and Procrastinate retries it later (nothing is lost).
4. Parse the reply against the schema and run the caller's quick check; on failure, try the
   next model up the step's ladder.
5. Log the call to core.model_calls: model, tokens, $, time, prompt version, role, trace and
   the router's decision, in its own transaction so failed calls are logged too.
"""

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from core.config import DbRole, get_settings
from core.crypto import decrypt
from core.db import tenant_session
from core.llm import router
from core.llm.pricing import cost_usd, estimate_tokens
from core.llm.providers.anthropic_provider import AnthropicProvider
from core.llm.providers.base import Credentials, Provider
from core.llm.providers.fake import FakeProvider
from core.llm.providers.gemini import GeminiProvider
from core.llm.providers.ollama import OllamaProvider
from core.llm.providers.openrouter import OpenRouterProvider
from core.llm.registry import ModelInfo, default_model, get_model
from core.llm.types import (
    BadReply,
    ChatTurn,
    LlmError,
    LlmRequest,
    LlmResult,
    LocalOnlyViolation,
    MissingConnection,
    ModelRefused,
    ProviderRejected,
    ProviderReply,
    ProviderUnavailable,
    RouteInfo,
)
from core.models import Business, Connection, ModelCall, ModelChoice, RoutingRule
from core.pii import mask
from core.prompts import live_prompt

PREVIEW_CHARS = 600
# Our own retries for providers reached over plain HTTP (the Claude SDK retries by itself)
HTTP_RETRY_DELAYS = (1.0, 3.0)

# A quick check returns None when the result is fine, or what's wrong with it
QuickCheck = Callable[[LlmResult], str | None]


class JobTag(BaseModel):
    """What the cheap tagging model returns for an unclear question (Task 4.3)."""

    task_type: str
    complexity: str
    risk: str


@dataclass
class ModelSettings:
    """A business's model settings, loaded once per client."""

    local_only: bool = False
    allow_free_with_customer_data: bool = False
    count_tokens: str = "always"  # always | large_only
    choices: dict[str, str] = field(default_factory=dict[str, str])
    routing: dict[tuple[str, str], str] = field(default_factory=dict[tuple[str, str], str])
    credentials: dict[str, Credentials] = field(default_factory=dict[str, Credentials])


def default_providers() -> dict[str, Provider]:
    if get_settings().llm_fake:
        fake = FakeProvider()
        return {"claude": fake, "gemini": fake, "openrouter": fake, "ollama": fake}
    return {
        "claude": AnthropicProvider(),
        "gemini": GeminiProvider(),
        "openrouter": OpenRouterProvider(),
        "ollama": OllamaProvider(),
    }


def extract_json(text: str) -> str:
    """The JSON object in a reply, without code fences or words around it."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


def _mask_request(request: LlmRequest) -> LlmRequest:
    return replace(
        request,
        system=[mask(block) for block in request.system],
        messages=[ChatTurn(t.role, mask(t.text), t.images) for t in request.messages],
    )


def _prompt_text(request: LlmRequest) -> str:
    return "\n".join([*request.system, *(turn.text for turn in request.messages)])


class LlmClient:
    def __init__(
        self,
        business_id: uuid.UUID,
        *,
        role: DbRole = "web",
        providers: Mapping[str, Provider] | None = None,
        settings: ModelSettings | None = None,
        force_local: bool = False,
    ) -> None:
        self.business_id = business_id
        self.role: DbRole = role
        self.providers: Mapping[str, Provider] = providers or default_providers()
        self._settings = settings
        # The Eval Lab's local-only runs: this client uses only the 2 local models, whatever
        # the business's setting says
        self._force_local = force_local
        self._fake = get_settings().llm_fake or isinstance(
            next(iter(self.providers.values()), None), FakeProvider
        )

    # ------------------------------------------------------------ settings and keys

    async def settings(self) -> ModelSettings:
        if self._settings is None:
            self._settings = await self._load_settings()
            if self._force_local:
                self._settings.local_only = True
        return self._settings

    async def _load_settings(self) -> ModelSettings:
        async with tenant_session(self.business_id, role=self.role) as session:
            business = await session.get(Business, self.business_id)
            if business is None:
                raise LlmError("Unknown business")
            choices = {
                row.step: row.model_key
                for row in (await session.execute(select(ModelChoice))).scalars()
            }
            routing = {
                (row.job, row.complexity): row.model_key
                for row in (await session.execute(select(RoutingRule))).scalars()
            }
            credentials: dict[str, Credentials] = {}
            for connection in (await session.execute(select(Connection))).scalars():
                if connection.kind == "ollama":
                    url = str(connection.config.get("base_url") or get_settings().ollama_url)
                    credentials["ollama"] = Credentials(base_url=url)
                elif (
                    connection.kind in ("claude", "gemini", "openrouter") and connection.secret_enc
                ):
                    key = decrypt(connection.secret_enc, business_id=self.business_id)
                    credentials[connection.kind] = Credentials(api_key=key)
            advanced = business.advanced or {}
            return ModelSettings(
                local_only=business.local_only,
                allow_free_with_customer_data=bool(advanced.get("allow_free_with_customer_data")),
                count_tokens=str(advanced.get("count_tokens", "always")),
                choices=choices,
                routing=routing,
                credentials=credentials,
            )

    def _credentials(self, model: ModelInfo, settings: ModelSettings) -> Credentials:
        if self._fake:
            return Credentials()
        if model.provider == "ollama":
            return settings.credentials.get(
                "ollama", Credentials(base_url=get_settings().ollama_url)
            )
        creds = settings.credentials.get(model.provider)
        if creds is None or not creds.api_key:
            names = {"claude": "Claude", "gemini": "Gemini", "openrouter": "OpenRouter"}
            raise MissingConnection(
                f"{model.name} needs a {names[model.provider]} key: add it in Settings > Keys"
            )
        return creds

    # ------------------------------------------------------------ the call

    async def call(self, request: LlmRequest, *, check: QuickCheck | None = None) -> LlmResult:
        settings = await self.settings()
        request = _mask_request(request)

        # Router step 2: tag the job (rules first; a cheap model only when unclear)
        complexity = request.complexity
        if complexity is None and request.model_key is None and not settings.local_only:
            last = request.messages[-1].text if request.messages else ""
            complexity = router.complexity_by_rules(request.job, last)
            if complexity is None:
                complexity = await self._tag_with_model(request)

        # Router step 3: pick from the routing table, Settings, or the caller
        pick = router.pick_model(
            request.job,
            complexity,
            forced=request.model_key,
            choices=settings.choices,
            routing=settings.routing,
            local_only=settings.local_only,
        )
        if (
            request.real_customer_data
            and not settings.allow_free_with_customer_data
            and not router.allowed_for_real_data(pick.model_key)
        ):
            safe = (
                default_model(request.job) if request.job in router.STEPS else "claude-sonnet-5-5"
            )
            pick = router.Pick(
                safe, f"customer text never goes to {pick.model_key} (may keep prompts)"
            )

        # Router step 1: count the tokens before sending; Haiku never above 100,000
        tokens, counted_by = await self._count(pick.model_key, request, settings)
        if (bigger := router.apply_size_rule(pick.model_key, tokens)) is not None:
            pick = bigger

        candidates = [pick.model_key]
        if not settings.local_only:
            candidates += router.escalation(request.job, pick.model_key)
        problems: list[str] = []
        for index, model_key in enumerate(candidates):
            route = RouteInfo(
                token_count=tokens,
                counted_by=counted_by,
                complexity=complexity,
                picked=model_key,
                reason=pick.reason if index == 0 else f"moved up after: {problems[-1]}",
                escalated_from=candidates[:index],
            )
            result = await self._attempt(model_key, request, settings, route)
            problem = self._problem(request, result) or (check(result) if check else None)
            if problem is None:
                return result
            problems.append(f"{model_key}: {problem}")
        raise BadReply("; ".join(problems))

    def _problem(self, request: LlmRequest, result: LlmResult) -> str | None:
        if result.stop_reason == "refusal":
            raise ModelRefused(f"{result.model_key} declined to answer")
        if request.schema is not None and result.parsed is None:
            return "the reply was not valid JSON for the schema"
        if result.stop_reason == "max_tokens":
            return "the reply was cut off (max tokens)"
        return None

    async def _tag_with_model(self, request: LlmRequest) -> router.Complexity:
        async with tenant_session(self.business_id, role=self.role) as session:
            prompt = await live_prompt(session, "router.tag_job")
        tag_request = LlmRequest(
            business_id=self.business_id,
            step=f"{request.step}.tag",
            job="tag_job",
            system=[prompt.body],
            messages=[ChatTurn("user", request.messages[-1].text[:4000])],
            schema=JobTag,
            max_tokens=2000,
            effort="low",
            complexity="low",
            trace_id=request.trace_id,
            prompt_key=prompt.key,
            prompt_version_id=prompt.version_id,
            real_customer_data=request.real_customer_data,
        )
        try:
            tagged = await self.call(tag_request)
        except LlmError:
            return "medium"  # tagging is an optimisation; never block an answer on it
        parsed = tagged.parsed
        value = parsed.complexity if isinstance(parsed, JobTag) else "medium"
        return value if value in ("low", "medium", "high") else "medium"  # type: ignore[return-value]

    async def _count(
        self, model_key: str, request: LlmRequest, settings: ModelSettings
    ) -> tuple[int, Literal["api", "estimate"]]:
        estimate = estimate_tokens(_prompt_text(request))
        model = get_model(model_key)
        if self._fake or model.provider not in ("claude", "gemini"):
            return estimate, "estimate"
        if settings.count_tokens == "large_only" and estimate < 50_000:
            return estimate, "estimate"
        try:
            creds = self._credentials(model, settings)
        except MissingConnection:
            return estimate, "estimate"
        counted = await self.providers[model.provider].count_tokens(model, request, creds)
        return (counted, "api") if counted else (estimate, "estimate")

    async def _attempt(
        self, model_key: str, request: LlmRequest, settings: ModelSettings, route: RouteInfo
    ) -> LlmResult:
        """One model, with its fallbacks if it is a free model that is busy."""
        chain = [model_key] + (router.fallbacks(model_key) if get_model(model_key).free else [])
        last_error: LlmError | None = None
        for key in chain:
            model = get_model(key)
            if settings.local_only and not model.local:
                raise LocalOnlyViolation(f"Local-only mode is on; {model.name} is not local")
            if (
                request.real_customer_data
                and not settings.allow_free_with_customer_data
                and not router.allowed_for_real_data(key)
            ):
                continue
            credentials = self._credentials(model, settings)
            provider = self.providers[model.provider]
            route.picked = key
            delays = () if model.provider == "claude" or model.free else HTTP_RETRY_DELAYS
            try:
                return await self._call_provider(
                    provider, model, request, credentials, route, delays
                )
            except ProviderUnavailable as exc:
                last_error = exc
                route.fallback_from.append(key)
        raise last_error or ProviderUnavailable(f"No model available for {model_key}")

    async def _call_provider(
        self,
        provider: Provider,
        model: ModelInfo,
        request: LlmRequest,
        credentials: Credentials,
        route: RouteInfo,
        delays: tuple[float, ...],
    ) -> LlmResult:
        for attempt in range(len(delays) + 1):
            started = time.monotonic()
            try:
                reply = await provider.complete(model, request, credentials)
            except (ProviderUnavailable, ProviderRejected) as exc:
                latency = int((time.monotonic() - started) * 1000)
                await self._log(request, model, None, Decimal(0), latency, route, "error", str(exc))
                if isinstance(exc, ProviderRejected) or attempt == len(delays):
                    raise
                await asyncio.sleep(delays[attempt])
                continue
            latency = int((time.monotonic() - started) * 1000)
            return await self._finish(request, model, reply, latency, route)
        raise ProviderUnavailable(f"{model.name} did not answer")

    async def _finish(
        self,
        request: LlmRequest,
        model: ModelInfo,
        reply: ProviderReply,
        latency_ms: int,
        route: RouteInfo,
    ) -> LlmResult:
        parsed: BaseModel | None = None
        if request.schema is not None and reply.stop_reason != "refusal":
            try:
                parsed = request.schema.model_validate_json(extract_json(reply.text))
            except ValidationError:
                parsed = None
        cost = cost_usd(model, reply.usage)
        status = "refused" if reply.stop_reason == "refusal" else "ok"
        call_id = await self._log(request, model, reply, cost, latency_ms, route, status, None)
        return LlmResult(
            text=reply.text,
            parsed=parsed,
            model_key=model.key,
            usage=reply.usage,
            cost_usd=cost,
            latency_ms=latency_ms,
            route=route,
            stop_reason=reply.stop_reason,
            call_id=call_id,
        )

    async def _log(
        self,
        request: LlmRequest,
        model: ModelInfo,
        reply: ProviderReply | None,
        cost: Decimal,
        latency_ms: int,
        route: RouteInfo,
        status: str,
        error: str | None,
    ) -> int:
        """Write the ledger row in its own transaction, so a failed call is logged too."""
        usage = reply.usage if reply else None
        last_user = request.messages[-1].text if request.messages else ""
        row = ModelCall(
            business_id=request.business_id,
            trace_id=request.trace_id,
            step=request.step,
            job=request.job,
            provider=model.provider,
            model_key=model.key,
            provider_model=reply.served_model if reply else model.model_id,
            prompt_key=request.prompt_key,
            prompt_version_id=request.prompt_version_id,
            role_version_id=request.role_version_id,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cache_write_tokens=usage.cache_write_tokens if usage else 0,
            cache_read_tokens=usage.cache_read_tokens if usage else 0,
            cost_usd=cost,
            latency_ms=latency_ms,
            status=status,
            error=mask(error)[:500] if error else None,
            route=route.as_dict() | ({"fake": True} if self._fake else {}),
            # Already masked: the request was masked before routing
            request_preview=last_user[:PREVIEW_CHARS],
            response_preview=mask(reply.text)[:PREVIEW_CHARS] if reply else None,
        )
        async with tenant_session(self.business_id, role=self.role) as session:
            session.add(row)
            await session.flush()
            return row.id


async def run_with_retry[T](
    func: Callable[[], Awaitable[T]], *, attempts: int = 3, delay: float = 2.0
) -> T:
    """Retry a step on ProviderUnavailable inside a long job, before giving up to the queue."""
    for attempt in range(attempts):
        try:
            return await func()
        except ProviderUnavailable:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(delay * (2**attempt))
    raise AssertionError("unreachable")
