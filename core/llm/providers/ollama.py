"""The 2 local models through Ollama on the owner's laptop (http://localhost:11434).

No key and no cost per call. From inside Docker the laptop is host.docker.internal.
Ollama's `format` takes a JSON schema, so local models return the same typed JSON.
"""

import base64
from typing import Any

import httpx

from core.llm.providers.base import Credentials, jdict, jlist, strict_json_schema
from core.llm.registry import ModelInfo
from core.llm.types import LlmRequest, ProviderRejected, ProviderReply, ProviderUnavailable, Usage

LOCAL_MODELS = ("gemma4:e4b", "qwen3.5:4b")


def _messages(request: LlmRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    system = "\n\n".join(block for block in request.system if block.strip())
    if system:
        messages.append({"role": "system", "content": system})
    for turn in request.messages:
        message: dict[str, Any] = {"role": turn.role, "content": turn.text}
        if turn.images:
            message["images"] = [
                base64.standard_b64encode(image.data).decode("ascii") for image in turn.images
            ]
        messages.append(message)
    return messages


class OllamaProvider:
    name = "ollama"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _http(self, credentials: Credentials) -> httpx.AsyncClient:
        # A laptop CPU can take minutes for a long answer
        return httpx.AsyncClient(
            base_url=credentials.base_url.rstrip("/"), timeout=600, transport=self._transport
        )

    async def complete(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> ProviderReply:
        body: dict[str, Any] = {
            "model": model.model_id,
            "messages": _messages(request),
            "stream": False,
            "options": {"num_predict": request.max_tokens},
        }
        if request.schema is not None:
            body["format"] = strict_json_schema(request.schema)
        try:
            async with self._http(credentials) as http:
                response = await http.post("/api/chat", json=body)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"Ollama did not answer at {credentials.base_url}: is it running? ({exc})"
            ) from exc
        if response.status_code == 404:
            raise ProviderRejected(
                f"Ollama has no {model.model_id}: run `ollama pull {model.model_id}`"
            )
        if response.status_code >= 400:
            raise ProviderUnavailable(f"Ollama answered {response.status_code}")
        data = jdict(response.json())
        return ProviderReply(
            text=str(jdict(data.get("message")).get("content", "")),
            usage=Usage(
                input_tokens=int(data.get("prompt_eval_count", 0)),
                output_tokens=int(data.get("eval_count", 0)),
            ),
            stop_reason="max_tokens" if data.get("done_reason") == "length" else "end_turn",
            served_model=model.model_id,
        )

    async def count_tokens(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> int | None:
        return None

    async def test(self, credentials: Credentials) -> tuple[bool, str]:
        try:
            async with self._http(credentials) as http:
                response = await http.get("/api/tags")
        except httpx.HTTPError:
            return False, (
                f"Nothing answered at {credentials.base_url}. Start Ollama on your laptop; from "
                "inside Docker use http://host.docker.internal:11434"
            )
        if response.status_code != 200:
            return False, f"Ollama answered {response.status_code}"
        names = [str(jdict(m).get("name", "")) for m in jlist(jdict(response.json()).get("models"))]
        present = [name for name in LOCAL_MODELS if name in names]
        missing = [name for name in LOCAL_MODELS if name not in names]
        message = f"Ollama works. Installed: {', '.join(present) or 'none of the 2'}."
        if missing:
            message += f" Missing: {', '.join(missing)} (ollama pull <name>, one at a time)."
        return True, message
