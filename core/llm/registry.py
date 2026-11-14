"""The 14 models the app may use (Feature 4), their prices, and the steps they can do.

Exactly the brief's 14 models, with its prices ($ per 1 million tokens, checked 12 Nov 2026).
Free OpenRouter models can be busy or removed, so each keeps 2-3 fallbacks, chosen from the
same 14. A Settings choice or a routing rule refers to a model by its `key`.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Provider = Literal["claude", "gemini", "openrouter", "ollama"]


@dataclass(frozen=True)
class LongPromptPrice:
    """Some prices change above a prompt size (Haiku 5.5: 5 times above 100,000 tokens)."""

    above_tokens: int
    input: Decimal
    output: Decimal


@dataclass(frozen=True)
class ModelInfo:
    key: str
    name: str
    provider: Provider
    model_id: str  # the id sent to the provider
    input_price: Decimal
    output_price: Decimal
    cache_read_price: Decimal | None = None  # Claude only
    long_prompt: LongPromptPrice | None = None
    vision: bool = False
    free: bool = False
    local: bool = False
    fallbacks: tuple[str, ...] = ()
    # The eval jobs the brief compares it on
    jobs: tuple[str, ...] = ()
    note: str = ""


D = Decimal
MODELS: dict[str, ModelInfo] = {
    m.key: m
    for m in (
        ModelInfo(
            "claude-opus-5-5",
            "Claude Opus 5.5",
            "claude",
            "claude-opus-5-5",
            D("4"),
            D("20"),
            cache_read_price=D("0.20"),
            vision=True,
            jobs=("improve_skill", "write_lesson", "judge"),
            note="Top score; improves skills, writes lessons, judges",
        ),
        ModelInfo(
            "claude-sonnet-5-5",
            "Claude Sonnet 5.5",
            "claude",
            "claude-sonnet-5-5",
            D("2"),
            D("10"),
            cache_read_price=D("0.10"),
            vision=True,
            jobs=("write_answer",),
            note="Main writer: support and consultant replies",
        ),
        ModelInfo(
            "claude-haiku-5-5",
            "Claude Haiku 5.5",
            "claude",
            "claude-haiku-5-5",
            D("0.10"),
            D("0.50"),
            cache_read_price=D("0.01"),
            long_prompt=LongPromptPrice(100_000, D("0.50"), D("2.50")),
            vision=True,
            jobs=("tag_job", "find_facts", "read_screenshot"),
            note="Tags the job, finds facts, short replies. Never above 100,000 tokens.",
        ),
        ModelInfo(
            "gemini-3.8-flash",
            "Gemini 3.8 Flash",
            "gemini",
            "gemini-3.8-flash",
            D("0.75"),
            D("3.75"),
            vision=True,
            jobs=("write_answer", "read_screenshot"),
            note="Introductory price; $1.50 / $7.50 from 1 Jan 2027",
        ),
        ModelInfo(
            "gemini-3-flash",
            "Gemini 3 Flash",
            "gemini",
            "gemini-3-flash",
            D("0.50"),
            D("3.00"),
            vision=True,
            jobs=("write_answer", "read_screenshot"),
            note="Cheaper writer",
        ),
        ModelInfo(
            "gemini-2.5-flash-lite",
            "Gemini 2.5 Flash-Lite",
            "gemini",
            "gemini-2.5-flash-lite",
            D("0.10"),
            D("0.40"),
            vision=True,
            jobs=("tag_job", "find_facts"),
        ),
        ModelInfo(
            "nemotron-3-ultra",
            "Nemotron 3 Ultra 550B",
            "openrouter",
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            D("0"),
            D("0"),
            free=True,
            fallbacks=("gemma-4-31b", "gemini-3-flash", "qwen-3.8-max"),
            jobs=("write_answer", "judge"),
            note="Writer at zero cost; second-opinion judge",
        ),
        ModelInfo(
            "nemotron-3.5-super-vl",
            "Nemotron 3.5 Super VL",
            "openrouter",
            "nvidia/nemotron-3.5-super-vl:free",
            D("0"),
            D("0"),
            vision=True,
            free=True,
            fallbacks=("gemini-3-flash", "gemini-3.8-flash", "claude-haiku-5-5"),
            jobs=("read_screenshot",),
            note="Reads screenshots",
        ),
        ModelInfo(
            "nemotron-3.5-lightning",
            "Nemotron 3.5 Lightning",
            "openrouter",
            "nvidia/nemotron-3.5-lightning:free",
            D("0"),
            D("0"),
            free=True,
            fallbacks=("qwen-3.7-flash", "gemini-2.5-flash-lite", "claude-haiku-5-5"),
            jobs=("tag_job",),
            note="Tags the job at zero cost",
        ),
        ModelInfo(
            "gemma-4-31b",
            "Gemma 4 31B",
            "openrouter",
            "google/gemma-4-31b-it:free",
            D("0"),
            D("0"),
            free=True,
            fallbacks=("nemotron-3-ultra", "gemini-3-flash", "qwen-3.8-max"),
            jobs=("write_answer",),
            note="Writer; the big sibling of gemma4:e4b",
        ),
        ModelInfo(
            "qwen-3.8-max",
            "Qwen 3.8 Max",
            "openrouter",
            "qwen/qwen3.8-max-0902",
            D("2"),
            D("6"),
            jobs=("write_answer",),
            note="Cheaper output than Sonnet; support and consultant replies",
        ),
        ModelInfo(
            "qwen-3.7-flash",
            "Qwen 3.7 Flash",
            "openrouter",
            "qwen/qwen3.7-flash",
            D("0.03"),
            D("0.13"),
            jobs=("tag_job",),
            note="Very cheap job tagging (price up to 32,000 tokens)",
        ),
        ModelInfo(
            "gemma4:e4b",
            "gemma4:e4b (local)",
            "ollama",
            "gemma4:e4b",
            D("0"),
            D("0"),
            vision=True,
            local=True,
            fallbacks=("qwen3.5:4b",),
            jobs=("write_answer", "read_screenshot"),
            note="On your laptop: offline replies, screenshots, private data",
        ),
        ModelInfo(
            "qwen3.5:4b",
            "qwen3.5:4b (local)",
            "ollama",
            "qwen3.5:4b",
            D("0"),
            D("0"),
            local=True,
            fallbacks=("gemma4:e4b",),
            jobs=("tag_job", "find_facts"),
            note="On your laptop: offline job tagging and fact finding",
        ),
    )
}


@dataclass(frozen=True)
class StepInfo:
    label: str
    default: str
    # The router's ladder: when an answer fails the quick check, the next model up is tried
    ladder: tuple[str, ...]


# The steps a business picks a model for (Settings > Models, Task 1.2), with the default
# and the router's ladder: if an answer fails the quick check, the next model up is tried
STEPS: dict[str, StepInfo] = {
    "tag_job": StepInfo(
        "Tag the job (router)", "claude-haiku-5-5", ("claude-haiku-5-5", "claude-sonnet-5-5")
    ),
    "find_facts": StepInfo(
        "Find facts in the Knowledge Brain",
        "claude-haiku-5-5",
        ("claude-haiku-5-5", "claude-sonnet-5-5"),
    ),
    "write_answer": StepInfo(
        "Write the answer",
        "claude-sonnet-5-5",
        ("claude-haiku-5-5", "gemini-3-flash", "claude-sonnet-5-5", "claude-opus-5-5"),
    ),
    "read_screenshot": StepInfo(
        "Read a screenshot",
        "claude-haiku-5-5",
        ("claude-haiku-5-5", "gemini-3.8-flash", "claude-sonnet-5-5"),
    ),
    "write_lesson": StepInfo(
        "Turn a correction into a lesson", "claude-opus-5-5", ("claude-opus-5-5",)
    ),
    "improve_skill": StepInfo("Improve or draft a skill", "claude-opus-5-5", ("claude-opus-5-5",)),
    "judge": StepInfo("Judge the answers", "claude-opus-5-5", ("claude-opus-5-5",)),
    "judge_second": StepInfo("Second-opinion judge", "nemotron-3-ultra", ("nemotron-3-ultra",)),
    # Go Live (Task 6.2): questions and answers written from the business's own pages
    "make_tests": StepInfo(
        "Write the starter tests", "claude-sonnet-5-5", ("claude-sonnet-5-5", "claude-opus-5-5")
    ),
}

# Local-only mode (Task 4.2): each step's model when no paid API may be used
LOCAL_ONLY: dict[str, str] = {
    "tag_job": "qwen3.5:4b",
    "find_facts": "qwen3.5:4b",
    "write_answer": "gemma4:e4b",
    "read_screenshot": "gemma4:e4b",
    "write_lesson": "gemma4:e4b",
    "improve_skill": "gemma4:e4b",
    "judge": "gemma4:e4b",
    "judge_second": "qwen3.5:4b",
    "make_tests": "gemma4:e4b",
}


def get_model(key: str) -> ModelInfo:
    try:
        return MODELS[key]
    except KeyError as exc:
        raise ValueError(
            f"Unknown model {key!r}; pick one of the 14 in core/llm/registry.py"
        ) from exc


def models_for_job(job: str) -> list[ModelInfo]:
    return [m for m in MODELS.values() if job in m.jobs]


def ladder(step: str) -> tuple[str, ...]:
    info = STEPS.get(step)
    return info.ladder if info else ()


def default_model(step: str) -> str:
    return STEPS[step].default
