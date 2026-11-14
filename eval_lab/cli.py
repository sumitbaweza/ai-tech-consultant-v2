"""The Eval Lab from the command line, for the checks that cost nothing.

    python -m eval_lab.cli url-cases  [--business "Solution Center Demo"] [--site URL]
    python -m eval_lab.cli retrieval  [--business "Solution Center Demo"]
    python -m eval_lab.cli projections

url-cases needs the fixture website (`docker compose --profile eval up -d fixture-site`) and
calls no model. retrieval runs the search on the brain cases (keywords need nothing; meaning
needs the Voyage key). projections prints the projected cost table: arithmetic, not a run.
"""

import argparse
import asyncio
import json
import sys
import uuid

from sqlalchemy import select

from core.config import get_settings
from core.db import dispose_engines, owner_session
from core.models import Business, Membership
from eval_lab import runner, special
from eval_lab.projections import projections

DEFAULT_BUSINESS = "Solution Center Demo"


async def find_business(name: str) -> tuple[uuid.UUID, uuid.UUID]:
    """The business by name and its owner (an admin lookup, so it uses the owner role)."""
    async with owner_session() as session:
        row = (
            await session.execute(
                select(Business.id, Membership.user_id)
                .join(Membership, Membership.business_id == Business.id)
                .where(Business.name == name, Membership.role == "owner")
                .limit(1)
            )
        ).first()
    if row is None:
        raise SystemExit(f'No business named "{name}" with an owner. Run the seed first.')
    return row[0], row[1]


async def url_cases(business: str, site: str) -> dict[str, object]:
    business_id, owner_id = await find_business(business)
    run_id = await runner.new_run(
        business_id,
        benchmark_kind="url",
        name="URL cases on the fixture site (CLI)",
        job="url_cases",
        models=["no model"],
        mode="url",
        user_id=owner_id,
    )
    summary = await special.url_cases(business_id, run_id, site=site, user_id=owner_id)
    return {"run_id": str(run_id), "summary": summary}


async def retrieval(business: str) -> dict[str, object]:
    business_id, owner_id = await find_business(business)
    run_id = await runner.new_run(
        business_id,
        benchmark_kind="brain",
        name="Retrieval check (CLI)",
        job="retrieval",
        models=["keyword", "hybrid+rerank", "vector"],
        mode="retrieval",
        user_id=owner_id,
    )
    summary = await special.retrieval_check(business_id, run_id)
    return {"run_id": str(run_id), "summary": summary}


def print_projections() -> None:
    print("PROJECTED cost per 100 calls: typical tokens per call x the price list. Not a run.")
    print(f"{'step':<16} {'model':<34} {'$ per 100':>10}")
    for p in projections():
        tag = " (free)" if p.free and "(free)" not in p.model_name else ""
        print(f"{p.job:<16} {p.model_name + tag:<34} {p.cost_per_100:>10.4f}")


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval_lab.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    url = commands.add_parser(
        "url-cases", help="the 10 URL cases on the fixture site (no model calls)"
    )
    url.add_argument("--business", default=DEFAULT_BUSINESS)
    url.add_argument("--site", default=get_settings().fixture_site_url)
    found = commands.add_parser("retrieval", help="top-5 hit rate of the search on the brain cases")
    found.add_argument("--business", default=DEFAULT_BUSINESS)
    commands.add_parser("projections", help="the projected cost table")
    args = parser.parse_args(argv)
    try:
        if args.command == "projections":
            print_projections()
            return 0
        result = await (
            url_cases(args.business, args.site)
            if args.command == "url-cases"
            else retrieval(args.business)
        )
        print(json.dumps(result, indent=2, default=str))
        return 0
    finally:
        await dispose_engines()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
