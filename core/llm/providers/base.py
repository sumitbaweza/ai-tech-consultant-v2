"""What every provider offers, and the JSON schema the 4 providers all accept."""

import copy
from dataclasses import dataclass
from typing import Any, Protocol, cast

from pydantic import BaseModel

from core.llm.registry import ModelInfo
from core.llm.types import LlmRequest, ProviderReply


@dataclass(frozen=True)
class Credentials:
    """A business's key for a provider, or its Ollama address. Never logged."""

    api_key: str = ""
    base_url: str = ""


class Provider(Protocol):
    name: str

    async def complete(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> ProviderReply: ...

    async def count_tokens(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> int | None:
        """The exact prompt size, if the provider can count it for free; None otherwise."""
        ...

    async def test(self, credentials: Credentials) -> tuple[bool, str]:
        """One small, free call that shows whether the connection works (Task 1.2)."""
        ...


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A Pydantic model's JSON schema in the strict form structured outputs want.

    References are inlined, every object lists all its properties as required and allows no
    others, and titles are dropped. Optional fields stay optional in meaning: their type allows
    null.
    """
    schema = model.model_json_schema()
    defs: dict[str, Any] = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            node_dict: dict[str, Any] = dict(node)  # pyright: ignore[reportUnknownArgumentType]
            ref = node_dict.get("$ref")
            if isinstance(ref, str):
                target = copy.deepcopy(defs[ref.split("/")[-1]])
                return resolve(target)
            node_dict.pop("title", None)
            node_dict.pop("default", None)
            if node_dict.get("type") == "object" and "properties" in node_dict:
                properties: dict[str, Any] = node_dict["properties"]
                node_dict["required"] = list(properties)
                node_dict["additionalProperties"] = False
            return {key: resolve(value) for key, value in node_dict.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]  # pyright: ignore[reportUnknownVariableType]
        return node

    resolved = resolve(schema)
    assert isinstance(resolved, dict)
    return resolved  # pyright: ignore[reportUnknownVariableType]


def schema_instructions(model: type[BaseModel]) -> str:
    """For providers without strict schema support: say what JSON to return."""
    import json

    return (
        "Reply with one JSON object only, no prose and no code fences, matching this JSON "
        "schema:\n" + json.dumps(strict_json_schema(model), separators=(",", ":"))
    )


def jdict(value: object) -> dict[str, Any]:
    """A JSON object from a provider's reply, or {} when it isn't one."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def jlist(value: object) -> list[Any]:
    """A JSON array from a provider's reply, or [] when it isn't one."""
    return cast(list[Any], value) if isinstance(value, list) else []
