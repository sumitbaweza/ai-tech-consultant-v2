"""A stand-in provider for tests, and for clicking through the screens with no key (LLM_FAKE=1).

It never calls anything. Its replies are plain rules over the prompt: the answer writer copies
the first source's opening sentences and cites it, the judge scores word overlap. The screens
show a "fake models" banner while it is on, and nothing it produces is ever reported as an
eval result.
"""

import json
import re
from collections.abc import Callable
from typing import Any

from core.llm.pricing import estimate_tokens
from core.llm.providers.base import Credentials, jdict, strict_json_schema
from core.llm.registry import ModelInfo
from core.llm.types import LlmRequest, ProviderReply, Usage

Responder = Callable[[LlmRequest], str | dict[str, Any]]

# Real source ids only (S1, S2 ...): prompts also show <source id="..."> as an example
_SOURCE = re.compile(r'<source id="(S\d+)"[^>]*>(.*?)</source>', re.DOTALL)
_WORD = re.compile(r"[a-z0-9]+")


def _all_text(request: LlmRequest) -> str:
    return "\n".join([*request.system, *(turn.text for turn in request.messages)])


def _first_sentences(text: str, count: int = 2) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    return " ".join(sentences[:count])[:600]


def example_for(schema: dict[str, Any]) -> Any:
    """The smallest value that matches a JSON schema: what a fake reply needs to parse."""
    if "anyOf" in schema:
        options: list[dict[str, Any]] = schema["anyOf"]
        non_null = [option for option in options if option.get("type") != "null"]
        return example_for(non_null[0]) if non_null else None
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        properties: dict[str, Any] = schema.get("properties", {})
        return {name: example_for(sub) for name, sub in properties.items()}
    if kind == "array":
        return []
    if kind == "string":
        return "fake"
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.5
    if kind == "boolean":
        return False
    return None


def _tag(request: LlmRequest) -> dict[str, Any]:
    text = request.messages[-1].text if request.messages else ""
    complexity = "low" if len(text) < 200 else "medium" if len(text) < 800 else "high"
    return {"task_type": "how_to", "complexity": complexity, "risk": "none"}


def _write_answer(request: LlmRequest) -> dict[str, Any]:
    sources = _SOURCE.findall(_all_text(request))
    if not sources:
        return {
            "answer": "I don't know: nothing in the knowledge base answers this.",
            "answered": False,
            "citations": [],
            "confidence": 0.1,
        }
    source_id, text = sources[0]
    return {
        "answer": f"{_first_sentences(text)} [{source_id}]",
        "answered": True,
        "citations": [source_id],
        "confidence": 0.7,
    }


def _find_facts(request: LlmRequest) -> dict[str, Any]:
    sources = _SOURCE.findall(_all_text(request))[:5]
    return {
        "relevant": [source_id for source_id, _ in sources],
        "facts": [
            {"source": source_id, "fact": _first_sentences(t, 1)} for source_id, t in sources
        ],
    }


def _judge(request: LlmRequest) -> dict[str, Any]:
    text = _all_text(request)
    expected = re.search(r"<expected>(.*?)</expected>", text, re.DOTALL)
    answer = re.search(r"<answer>(.*?)</answer>", text, re.DOTALL)
    want = set(_WORD.findall((expected.group(1) if expected else "").lower()))
    got = set(_WORD.findall((answer.group(1) if answer else "").lower()))
    score = round(len(want & got) / len(want), 2) if want else 0.0
    return {
        "score": score,
        "passed": score >= 0.5,
        "reason": f"Fake judge: {score:.0%} word overlap",
    }


def _make_tests(request: LlmRequest) -> dict[str, Any]:
    """Go Live's starter tests: two questions from the page's title and opening sentences."""
    text = _all_text(request)
    found = re.search(r'<page title="([^"]*)"[^>]*>(.*?)</page>', text, re.DOTALL)
    title, body = (found.group(1), found.group(2)) if found else ("this page", "")
    answer = _first_sentences(body.strip(), 2) or "See the page."
    return {
        "tests": [
            {
                "question": f"How do I {title.lower().rstrip('?')}?",
                "answer": answer,
                "key_facts": [],
            },
            {
                "question": f"Where can I read about {title.lower().rstrip('?')}?",
                "answer": answer,
                "key_facts": [],
            },
        ]
    }


DEFAULT_RESPONDERS: dict[str, Responder] = {
    "tag_job": _tag,
    "write_answer": _write_answer,
    "find_facts": _find_facts,
    "judge": _judge,
    "judge_second": _judge,
    "make_tests": _make_tests,
}


class FakeProvider:
    name = "fake"

    def __init__(self, responders: dict[str, Responder] | None = None) -> None:
        self.responders = {**DEFAULT_RESPONDERS, **(responders or {})}
        self.calls: list[LlmRequest] = []

    async def complete(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> ProviderReply:
        self.calls.append(request)
        responder = self.responders.get(request.job)
        out: str | dict[str, Any]
        if responder is not None:
            out = responder(request)
        elif request.schema is not None:
            out = jdict(example_for(strict_json_schema(request.schema)))
        else:
            out = f"(fake {model.key} reply)"
        if request.schema is not None and isinstance(out, dict):
            # Fill fields the responder didn't set, so the reply always parses
            base = jdict(example_for(strict_json_schema(request.schema)))
            out = {**base, **{key: value for key, value in out.items() if key in base}}
        text = out if isinstance(out, str) else json.dumps(out)
        return ProviderReply(
            text=text,
            usage=Usage(
                input_tokens=estimate_tokens(_all_text(request)),
                output_tokens=estimate_tokens(text),
            ),
            stop_reason="end_turn",
            served_model=model.model_id,
        )

    async def count_tokens(
        self, model: ModelInfo, request: LlmRequest, credentials: Credentials
    ) -> int | None:
        return None

    async def test(self, credentials: Credentials) -> tuple[bool, str]:
        return True, "Fake models are on (development only): nothing was called."
