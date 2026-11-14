"""OpenRouter's OpenAI-style chat API: the 4 free models and the 2 Qwen models.

Free models have per-account limits and can be busy: a 429 or 503 raises ProviderUnavailable,
and the client then tries the model's fallbacks (core/llm/registry.py).
"""

import base64
from typing import Any

import httpx

from core.llm.providers.base import Credentials, jdict, jlist, strict_json_schema
from core.llm.registry import ModelInfo
from core.llm.types import LlmRequest, ProviderRejected, ProviderReply, ProviderUnavailable, Usage

BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter asks apps to say who they are; a generic name, nothing about the user
APP_HEADERS = {
    "HTTP-Referer": "https://github.com/ai-tech-consultant",
    "X-Title": "AI Tech Consultant",
}
STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}


def _messages(request: LlmRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = "\n\n".join(block for block in request.system if block.strip())
    if system:
        messages.append({"role": "system", "content": system})
    for turn in request.messages:
        if not turn.images:
            messages.append({"role": turn.role, "content": turn.text})
            continue
        content: list[dict[str, Any]] = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.media_type};base64,"
                    + base64.standard_b64encode(image.data).decode("ascii")
                },
            }
            for image in turn.images
        ]
        content.append({"type": "text", "text": turn.text})
        messages.append({"role": turn.role, "content": content})
    return messages


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _http(self, credentials: Credentials) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=180,
            transport=self._transport,
            headers={"Authorization": f"Bearer {credentials.api_key}", **APP_HEADERS},
        )

    async def complete(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> ProviderReply:
        body: dict[str, Any] = {
            "model": model.model_id,
            "messages": _messages(request),
            "max_tokens": request.max_tokens,
            "usage": {"include": True},
        }
        if request.schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema.__name__,
                    "strict": True,
                    "schema": strict_json_schema(request.schema),
                },
            }
        try:
            async with self._http(credentials) as http:
                response = await http.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"OpenRouter did not answer: {exc}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderUnavailable(f"{model.name} is busy ({response.status_code})")
        if response.status_code >= 400:
            raise ProviderRejected(f"OpenRouter refused the request ({response.status_code})")
        data = jdict(response.json())
        if "error" in data:  # a 200 can carry an error from the model's provider
            message = jdict(data["error"]).get("message", "error")
            raise ProviderUnavailable(f"{model.name}: {message}")
        choice = jdict((jlist(data.get("choices")) or [{}])[0])
        text = str(jdict(choice.get("message")).get("content") or "")
        usage = jdict(data.get("usage"))
        cached = int(jdict(usage.get("prompt_tokens_details")).get("cached_tokens") or 0)
        stop = STOP_REASONS.get(str(choice.get("finish_reason") or "stop"), "end_turn")
        return ProviderReply(
            text=text,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens", 0)) - cached,
                output_tokens=int(usage.get("completion_tokens", 0)),
                cache_read_tokens=cached,
            ),
            stop_reason=stop,
            served_model=str(data.get("model", model.model_id)),
        )

    async def count_tokens(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> int | None:
        return None  # no counting endpoint: the router estimates; the reply's usage is exact

    async def test(self, credentials: Credentials) -> tuple[bool, str]:
        try:
            async with self._http(credentials) as http:
                response = await http.get("/key")
        except httpx.HTTPError as exc:
            return False, f"OpenRouter did not answer: {exc}"
        if response.status_code != 200:
            return False, f"The key was rejected ({response.status_code})."
        info = jdict(jdict(response.json()).get("data"))
        limit = info.get("limit")
        return (
            True,
            f"Key works. Credit limit: {limit if limit is not None else 'none'}; free tier: {info.get('is_free_tier')}",
        )
