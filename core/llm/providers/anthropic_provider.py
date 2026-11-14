"""Claude through the official Anthropic SDK (Opus, Sonnet and Haiku 5.5).

Structured outputs with `output_config.format`, prompt caching with a cache breakpoint after
the system blocks, the free token-counting endpoint, and the server-side refusal fallback on
Sonnet and Opus. Never `temperature`, `top_p` or `top_k`: the API rejects them.
"""

import base64

import anthropic
from anthropic.types.beta import (
    BetaAssistantMessageParam,
    BetaBase64ImageSourceParam,
    BetaImageBlockParam,
    BetaMessageParam,
    BetaOutputConfigParam,
    BetaTextBlockParam,
    BetaUserMessageParam,
)

from core.llm.providers.base import Credentials, strict_json_schema
from core.llm.registry import ModelInfo
from core.llm.types import (
    ChatTurn,
    LlmRequest,
    ProviderRejected,
    ProviderReply,
    ProviderUnavailable,
    Usage,
)

# Sonnet and Opus 5.5 may decline for safety reasons; the API then re-runs the request on a
# fallback model it picks by the refusal's category. Haiku has no fallback.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_HAS_FALLBACK = {"claude-sonnet-5-5", "claude-opus-5-5"}


def _client(credentials: Credentials) -> anthropic.AsyncAnthropic:
    # The SDK retries connection errors, 408, 409, 429 and 5xx twice with backoff
    return anthropic.AsyncAnthropic(api_key=credentials.api_key, max_retries=2, timeout=180)


def _system(request: LlmRequest) -> list[BetaTextBlockParam]:
    blocks: list[BetaTextBlockParam] = [
        {"type": "text", "text": text} for text in request.system if text.strip()
    ]
    if request.cache_system and blocks:
        # Everything up to here is cached for 5 minutes; later calls pay a tenth of the price
        # for it. Stable blocks (master prompt, role, instructions) come first for this reason.
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _message(turn: ChatTurn) -> BetaMessageParam:
    if turn.role == "assistant":
        return BetaAssistantMessageParam(role="assistant", content=turn.text)
    if not turn.images:
        return BetaUserMessageParam(role="user", content=turn.text)
    content: list[BetaImageBlockParam | BetaTextBlockParam] = [
        BetaImageBlockParam(
            type="image",
            source=BetaBase64ImageSourceParam(
                type="base64",
                media_type=image.media_type,  # type: ignore[typeddict-item]
                data=base64.standard_b64encode(image.data).decode("ascii"),
            ),
        )
        for image in turn.images
    ]
    content.append(BetaTextBlockParam(type="text", text=turn.text))
    return BetaUserMessageParam(role="user", content=content)


def _output_config(request: LlmRequest) -> BetaOutputConfigParam:
    config: BetaOutputConfigParam = {}
    if request.effort:
        config["effort"] = request.effort
    if request.schema is not None:
        config["format"] = {"type": "json_schema", "schema": strict_json_schema(request.schema)}
    return config


class AnthropicProvider:
    name = "claude"

    async def complete(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> ProviderReply:
        client = _client(credentials)
        try:
            if model.key in _HAS_FALLBACK:
                response = await client.beta.messages.create(
                    model=model.model_id,
                    max_tokens=request.max_tokens,
                    system=_system(request),
                    messages=[_message(turn) for turn in request.messages],
                    output_config=_output_config(request),
                    betas=[_FALLBACK_BETA],
                    fallbacks="default",
                )
            else:
                response = await client.beta.messages.create(
                    model=model.model_id,
                    max_tokens=request.max_tokens,
                    system=_system(request),
                    messages=[_message(turn) for turn in request.messages],
                    output_config=_output_config(request),
                )
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
            anthropic.BadRequestError,
        ) as exc:
            raise ProviderRejected(f"Claude refused the request: {exc.message}") from exc
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
            raise ProviderUnavailable(f"Claude is unavailable: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderUnavailable(f"Claude answered {exc.status_code}") from exc
        finally:
            await client.close()

        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        category = None
        if response.stop_reason == "refusal" and response.stop_details is not None:
            category = getattr(response.stop_details, "category", None)
        return ProviderReply(
            text=text,
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_creation_input_tokens or 0,
                cache_read_tokens=usage.cache_read_input_tokens or 0,
            ),
            stop_reason=response.stop_reason or "end_turn",
            served_model=response.model,
            refusal_category=category,
        )

    async def count_tokens(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> int | None:
        # Free endpoint: POST /v1/messages/count_tokens
        client = _client(credentials)
        try:
            counted = await client.beta.messages.count_tokens(
                model=model.model_id,
                system=_system(request),
                messages=[_message(turn) for turn in request.messages],
            )
            return counted.input_tokens
        except anthropic.APIError:
            return None  # the estimate is used instead; the call itself will report errors
        finally:
            await client.close()

    async def test(self, credentials: Credentials) -> tuple[bool, str]:
        client = _client(credentials)
        try:
            page = await client.models.list(limit=3)
            names = ", ".join(model.id for model in page.data)
            return True, f"Key works. Models visible: {names}"
        except anthropic.AuthenticationError:
            return False, "The key was rejected (401). Copy it again from the Claude Console."
        except anthropic.APIError as exc:
            return False, f"Claude did not answer: {exc}"
        finally:
            await client.close()
