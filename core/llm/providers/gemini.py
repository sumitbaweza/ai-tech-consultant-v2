"""Gemini through Google's REST API (generateContent and the free countTokens).

Used by the Eval Lab for the 3 Gemini models. Google's free tier may keep prompts, so the app
sends Gemini only made-up test data unless the business allows more in Settings > Advanced.
"""

import base64
from typing import Any

import httpx

from core.llm.providers.base import Credentials, jdict, jlist, strict_json_schema
from core.llm.registry import ModelInfo
from core.llm.types import LlmRequest, ProviderRejected, ProviderReply, ProviderUnavailable, Usage

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
STOP_REASONS = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens", "SAFETY": "refusal"}


def _body(request: LlmRequest) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    for turn in request.messages:
        parts: list[dict[str, Any]] = [
            {
                "inline_data": {
                    "mime_type": image.media_type,
                    "data": base64.standard_b64encode(image.data).decode("ascii"),
                }
            }
            for image in turn.images
        ]
        parts.append({"text": turn.text})
        contents.append({"role": "model" if turn.role == "assistant" else "user", "parts": parts})
    body: dict[str, Any] = {"contents": contents}
    system = "\n\n".join(block for block in request.system if block.strip())
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    config: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
    if request.schema is not None:
        config["responseMimeType"] = "application/json"
        config["responseJsonSchema"] = strict_json_schema(request.schema)
    body["generationConfig"] = config
    return body


def _headers(credentials: Credentials) -> dict[str, str]:
    # The key goes in a header, never in the URL, so it can't end up in an access log
    return {"x-goog-api-key": credentials.api_key, "content-type": "application/json"}


class GeminiProvider:
    name = "gemini"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=120, transport=self._transport)

    async def complete(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> ProviderReply:
        url = f"{BASE_URL}/models/{model.model_id}:generateContent"
        try:
            async with self._http() as http:
                response = await http.post(url, headers=_headers(credentials), json=_body(request))
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Gemini did not answer: {exc}") from exc
        if response.status_code in (400, 401, 403, 404):
            raise ProviderRejected(f"Gemini refused the request ({response.status_code})")
        if response.status_code >= 300:
            raise ProviderUnavailable(f"Gemini answered {response.status_code}")
        data = jdict(response.json())
        candidates = jlist(data.get("candidates"))
        if not candidates:
            # Blocked by Google's safety filters: no candidate, a promptFeedback instead
            reason = str(jdict(data.get("promptFeedback")).get("blockReason", "unknown"))
            return ProviderReply("", Usage(), "refusal", model.model_id, refusal_category=reason)
        candidate = jdict(candidates[0])
        parts = [jdict(part) for part in jlist(jdict(candidate.get("content")).get("parts"))]
        text = "".join(str(part.get("text", "")) for part in parts if not part.get("thought"))
        meta = jdict(data.get("usageMetadata"))
        cached = int(meta.get("cachedContentTokenCount", 0))
        stop = STOP_REASONS.get(str(candidate.get("finishReason", "STOP")), "end_turn")
        return ProviderReply(
            text=text,
            usage=Usage(
                input_tokens=int(meta.get("promptTokenCount", 0)) - cached,
                # Thinking tokens are billed as output
                output_tokens=int(meta.get("candidatesTokenCount", 0))
                + int(meta.get("thoughtsTokenCount", 0)),
                cache_read_tokens=cached,
            ),
            stop_reason=stop,
            served_model=str(data.get("modelVersion", model.model_id)),
        )

    async def count_tokens(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> int | None:
        """Google's free countTokens method, with the same contents as the real call."""
        url = f"{BASE_URL}/models/{model.model_id}:countTokens"
        body = _body(request)
        inner: dict[str, Any] = {"model": f"models/{model.model_id}", "contents": body["contents"]}
        if "systemInstruction" in body:
            inner["systemInstruction"] = body["systemInstruction"]
        try:
            async with self._http() as http:
                response = await http.post(
                    url, headers=_headers(credentials), json={"generateContentRequest": inner}
                )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        return int(jdict(response.json()).get("totalTokens", 0)) or None

    async def test(self, credentials: Credentials) -> tuple[bool, str]:
        try:
            async with self._http() as http:
                response = await http.get(
                    f"{BASE_URL}/models", headers=_headers(credentials), params={"pageSize": 100}
                )
        except httpx.HTTPError as exc:
            return False, f"Gemini did not answer: {exc}"
        if response.status_code != 200:
            return False, f"The key was rejected ({response.status_code})."
        names = [str(jdict(m).get("name", "")) for m in jlist(jdict(response.json()).get("models"))]
        wanted = ["gemini-3.8-flash", "gemini-3-flash", "gemini-2.5-flash-lite"]
        found = [w for w in wanted if any(w in name for name in names)]
        return (
            True,
            f"Key works. Of our 3 Gemini models, {len(found)} listed: {', '.join(found) or 'none'}",
        )
