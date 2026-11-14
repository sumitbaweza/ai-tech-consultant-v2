"""The model router (Task 4.3), as plain functions the client calls in order.

1. Count the tokens first (the client asks Claude's or Gemini's free counter, or estimates).
   A prompt over 100,000 tokens never goes to Haiku 5.5: its price is 5 times higher there.
2. Tag the job. Rules decide most jobs; only an unclear customer question asks a cheap model
   for {task_type, complexity}.
3. Pick from the routing table (Settings > Routing): for the job and complexity, the cheapest
   model that passed the Eval Lab's bar. If the answer fails a quick check, the next model up
   the step's ladder is tried; if a free model is busy, its fallbacks are.
"""

import re
from dataclasses import dataclass
from typing import Literal

from core.llm.registry import LOCAL_ONLY, MODELS, STEPS, default_model, get_model, ladder

Complexity = Literal["low", "medium", "high"]
HAIKU_LIMIT_TOKENS = 100_000

# Jobs whose complexity is fixed by what they are: no tagging needed
FIXED_COMPLEXITY: dict[str, Complexity] = {
    "tag_job": "low",
    "find_facts": "low",
    "read_screenshot": "medium",
    "judge": "high",
    "judge_second": "high",
    "write_lesson": "high",
    "improve_skill": "high",
}

_EASY = re.compile(
    r"^\s*(how (do|can) i|where (is|can|do)|can i|what is|what's|is there|do you have)\b",
    re.IGNORECASE,
)
_HARD = re.compile(
    r"\b(error|not working|doesn'?t work|broken|bug|api|integrat\w*|code|css|html|javascript|"
    r"dns|ssl|webhook|migrat\w*|custom field|database|import|export|quote|proposal|pricing)\b",
    re.IGNORECASE,
)


def complexity_by_rules(job: str, text: str) -> Complexity | None:
    """Most jobs are tagged by rules; None means unclear, so a cheap model tags it."""
    if job in FIXED_COMPLEXITY:
        return FIXED_COMPLEXITY[job]
    if job != "write_answer":
        return "medium"
    questions = text.count("?")
    if len(text) > 1500 or questions >= 3:
        return "high"
    if _HARD.search(text):
        return "high" if len(text) > 400 else "medium"
    if len(text) < 200 and questions <= 1 and _EASY.search(text):
        return "low"
    return None


@dataclass(frozen=True)
class Pick:
    model_key: str
    reason: str


def pick_model(
    job: str,
    complexity: Complexity | None,
    *,
    forced: str | None,
    choices: dict[str, str],
    routing: dict[tuple[str, str], str],
    local_only: bool,
) -> Pick:
    """Step 3: the first model to try."""
    if local_only:
        return Pick(LOCAL_ONLY.get(job, "gemma4:e4b"), "local-only mode")
    if forced:
        return Pick(forced, "set by the caller (role, eval run or test)")
    if complexity and (job, complexity) in routing:
        return Pick(routing[(job, complexity)], f"routing table: {job} / {complexity}")
    if job in choices:
        return Pick(choices[job], "model chosen in Settings for this step")
    if job in STEPS:
        return Pick(default_model(job), "default for this step")
    return Pick("claude-sonnet-5-5", "no rule for this job")


def apply_size_rule(model_key: str, token_count: int) -> Pick | None:
    """Never Haiku above 100,000 tokens (Task 4.3): move up to Sonnet."""
    if model_key == "claude-haiku-5-5" and token_count > HAIKU_LIMIT_TOKENS:
        return Pick(
            "claude-sonnet-5-5",
            f"{token_count:,} tokens is over Haiku's 100,000-token price step",
        )
    return None


def escalation(job: str, model_key: str) -> list[str]:
    """The models above this one on the job's ladder, cheapest first."""
    steps = list(ladder(job))
    if model_key in steps:
        return steps[steps.index(model_key) + 1 :]
    # A model off the ladder (a role's own pick, a free model) escalates to the step default
    default = default_model(job) if job in STEPS else "claude-sonnet-5-5"
    return [default] if default != model_key else []


def fallbacks(model_key: str) -> list[str]:
    """Free models are often busy; each keeps 2-3 fallbacks from the same 14."""
    return list(get_model(model_key).fallbacks)


def allowed_for_real_data(model_key: str) -> bool:
    """Free OpenRouter models and Gemini's free tier may keep prompts: made-up data only."""
    model = MODELS[model_key]
    return not (model.free or model.provider == "gemini") or model.local
