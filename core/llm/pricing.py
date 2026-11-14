"""The $ cost of one call, from the registry's prices and the tokens used."""

from decimal import Decimal

from core.llm.registry import ModelInfo
from core.llm.types import Usage

MILLION = Decimal(1_000_000)
# A 5-minute cache write costs 1.25 times the input price (Claude)
CACHE_WRITE_FACTOR = Decimal("1.25")


def cost_usd(model: ModelInfo, usage: Usage) -> Decimal:
    input_price, output_price = model.input_price, model.output_price
    prompt_tokens = usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens
    if model.long_prompt and prompt_tokens > model.long_prompt.above_tokens:
        input_price, output_price = model.long_prompt.input, model.long_prompt.output
    cache_read_price = model.cache_read_price if model.cache_read_price is not None else input_price
    total = (
        usage.input_tokens * input_price
        + usage.output_tokens * output_price
        + usage.cache_write_tokens * input_price * CACHE_WRITE_FACTOR
        + usage.cache_read_tokens * cache_read_price
    ) / MILLION
    return total.quantize(Decimal("0.000001"))


def projected_cost(
    model: ModelInfo, input_tokens: int, output_tokens: int, runs: int = 100
) -> Decimal:
    """Cost of `runs` calls of a typical size: the Eval Lab's projections before a real run."""
    one = cost_usd(model, Usage(input_tokens=input_tokens, output_tokens=output_tokens))
    return (one * runs).quantize(Decimal("0.0001"))


def estimate_tokens(text: str) -> int:
    """About 4 characters of English per token, as the brief says: used before the exact count."""
    return max(1, len(text) // 4)
