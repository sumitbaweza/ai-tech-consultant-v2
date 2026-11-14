"""Projected cost per model and step, before any run: tokens per call x the brief's prices.

Labelled "projected" wherever it is shown. A real run in the Eval Lab replaces it with the
measured cost from the ledger. Quality is never projected here: it only comes from runs.

Tokens per call come from the v1 prompts as measured (STACK.md M2): a cited answer reads about
4,100 tokens and writes about 350.
"""

from dataclasses import dataclass

from core.llm.pricing import projected_cost
from core.llm.registry import MODELS, STEPS, models_for_job

TYPICAL_TOKENS: dict[str, tuple[int, int]] = {
    "tag_job": (300, 30),
    "find_facts": (2_800, 150),
    "write_answer": (4_100, 350),
    "read_screenshot": (1_480, 150),
    "write_lesson": (3_000, 800),
    "improve_skill": (3_000, 800),
    "judge": (1_200, 80),
    "judge_second": (1_200, 80),
    "make_tests": (3_200, 700),
}


@dataclass(frozen=True)
class Projection:
    job: str
    model_key: str
    model_name: str
    cost_per_100: float
    free: bool
    local: bool


def projections() -> list[Projection]:
    rows: list[Projection] = []
    for job, (tokens_in, tokens_out) in TYPICAL_TOKENS.items():
        # The models the brief compares on this job, the step's default and the router's ladder
        candidates = {m.key for m in models_for_job("judge" if job == "judge_second" else job)}
        candidates.add(STEPS[job].default)
        candidates.update(STEPS[job].ladder)
        for key in sorted(candidates):
            model = MODELS[key]
            rows.append(
                Projection(
                    job,
                    key,
                    model.name,
                    float(projected_cost(model, tokens_in, tokens_out)),
                    model.free,
                    model.local,
                )
            )
    return rows
