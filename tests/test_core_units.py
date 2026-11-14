"""Core building blocks that need no database: encryption, passwords, masking, prompts,
pricing, the router's rules and the schema helper."""

import uuid
from decimal import Decimal

import pytest
from pydantic import BaseModel

from core.crypto import DecryptError, decrypt, encrypt, mask
from core.llm import router
from core.llm.client import extract_json
from core.llm.pricing import cost_usd, estimate_tokens
from core.llm.providers.base import strict_json_schema
from core.llm.providers.fake import example_for
from core.llm.registry import MODELS, STEPS, get_model
from core.llm.types import Usage
from core.pii import luhn_ok, mask_pii
from core.prompts import defaults, missing_variables, render, variables_in
from core.security import hash_password, verify_password


def test_encrypt_round_trip_and_business_binding() -> None:
    business, other = uuid.uuid4(), uuid.uuid4()
    blob = encrypt("sk-ant-secret-value-1234", business_id=business)
    assert b"sk-ant" not in blob
    assert decrypt(blob, business_id=business) == "sk-ant-secret-value-1234"
    with pytest.raises(DecryptError):
        decrypt(blob, business_id=other)  # copied into another business's row: unreadable


def test_mask_shows_only_start_and_end() -> None:
    assert mask("sk-ant-api03-abcdefghijklmnop") == "sk-ant...mnop"
    assert mask("short") == "****"


def test_password_hash_verifies_and_rejects() -> None:
    stored = hash_password("correct horse")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong horse", stored)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Card 4242 4242 4242 4242 was charged twice", "card"),
        ("call me on +1 (415) 555-0132 tomorrow", "phone"),
        ("my number is 07700 900123", "phone"),
        ("ship it to 221B Baker Street, London", "address"),
        ("write to jane.doe@example.com please", "email"),
    ],
)
def test_pii_is_masked(text: str, kind: str) -> None:
    result = mask_pii(text)
    assert result.counts.get(kind, 0) >= 1, result
    assert "4242 4242" not in result.text and "555-0132" not in result.text


@pytest.mark.parametrize(
    "text",
    [
        "Order 123456789 is late",  # an order number is not a phone number
        "Invoice #20261114 attached",
        "The plan costs 1299 a year",
        "Released on 2026-11-14",
        "1234 5678 9012 3456 is not a valid card",  # fails the Luhn check
    ],
)
def test_pii_leaves_ordinary_numbers(text: str) -> None:
    assert mask_pii(text).text == text


def test_luhn() -> None:
    assert luhn_ok("4242424242424242")
    assert not luhn_ok("4242424242424241")


def test_every_default_prompt_parses_and_keeps_its_variables() -> None:
    found = defaults()
    assert {
        "chat.master_system",
        "chat.answer_user",
        "eval.judge",
        "brain.describe_screenshot",
    } <= set(found)
    for prompt in found.values():
        assert prompt.title
        assert missing_variables(prompt.body, prompt.required) == [], prompt.key


def test_render_and_variables() -> None:
    text = 'Hello {customer_name}, about {topic}. JSON: {{"a": 1}}'
    assert variables_in(text) == ["customer_name", "topic"]
    assert render(text, {"customer_name": "Ana", "topic": "DNS"}) == (
        'Hello Ana, about DNS. JSON: {"a": 1}'
    )
    assert missing_variables("Hi {a}", ["a", "b"]) == ["b"]


def test_exactly_fourteen_models_with_fallbacks_for_free_ones() -> None:
    assert len(MODELS) == 14
    for model in MODELS.values():
        if model.free:
            assert 2 <= len(model.fallbacks) <= 3, model.key
        for fallback in model.fallbacks:
            assert fallback in MODELS
    for step, info in STEPS.items():
        assert info.default in MODELS and set(info.ladder) <= set(MODELS), step


def test_haiku_price_steps_up_above_100k_tokens() -> None:
    haiku = get_model("claude-haiku-5-5")
    large = cost_usd(haiku, Usage(input_tokens=150_000))
    small = cost_usd(haiku, Usage(input_tokens=50_000))
    assert small == Decimal("0.005000")  # 50K x $0.10 per million
    assert large == Decimal("0.075000")  # 150K x $0.50 per million


def test_cached_tokens_cost_less() -> None:
    sonnet = get_model("claude-sonnet-5-5")
    plain = cost_usd(sonnet, Usage(input_tokens=4100, output_tokens=350))
    cached = cost_usd(sonnet, Usage(input_tokens=2500, cache_read_tokens=1600, output_tokens=350))
    assert plain == Decimal("0.011700")
    assert cached == Decimal("0.008660")


def test_router_rules_and_size_rule() -> None:
    assert router.complexity_by_rules("judge", "anything") == "high"
    assert router.complexity_by_rules("write_answer", "How do I add a logo?") == "low"
    assert router.complexity_by_rules("write_answer", "I get a DNS error " * 40) == "high"
    assert (
        router.complexity_by_rules("write_answer", "Tell me about your pricing tiers.") == "medium"
    )
    moved = router.apply_size_rule("claude-haiku-5-5", 120_000)
    assert moved is not None and moved.model_key == "claude-sonnet-5-5"
    assert router.apply_size_rule("claude-haiku-5-5", 90_000) is None


def test_router_pick_order() -> None:
    routing = {("write_answer", "low"): "claude-haiku-5-5"}
    pick = router.pick_model(
        "write_answer", "low", forced=None, choices={}, routing=routing, local_only=False
    )
    assert pick.model_key == "claude-haiku-5-5"
    local = router.pick_model(
        "write_answer",
        "low",
        forced="claude-opus-5-5",
        choices={},
        routing=routing,
        local_only=True,
    )
    assert local.model_key == "gemma4:e4b"
    assert router.escalation("write_answer", "claude-haiku-5-5")[0] == "gemini-3-flash"
    assert not router.allowed_for_real_data("nemotron-3-ultra")
    assert router.allowed_for_real_data("claude-sonnet-5-5")
    assert router.allowed_for_real_data("gemma4:e4b")


class _Inner(BaseModel):
    source: str
    fact: str


class _Outer(BaseModel):
    relevant: list[str]
    facts: list[_Inner]
    note: str | None = None


def test_strict_schema_inlines_and_closes_objects() -> None:
    schema = strict_json_schema(_Outer)
    assert "$defs" not in str(schema)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["relevant", "facts", "note"]
    item = schema["properties"]["facts"]["items"]
    assert item["required"] == ["source", "fact"] and item["additionalProperties"] is False
    assert _Outer.model_validate(example_for(schema))


def test_extract_json_and_estimate() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json('Sure! {"a": {"b": 2}} done') == '{"a": {"b": 2}}'
    assert estimate_tokens("x" * 400) == 100
