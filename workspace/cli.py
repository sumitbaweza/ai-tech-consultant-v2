"""Workspace settings from the command line.

    python -m workspace.cli local-only --business "Solution Center Demo" --on
    python -m workspace.cli local-only --business "Solution Center Demo" --off

Local-only mode sends every step to the Ollama models on this machine (see README).
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from core.db import dispose_engines, owner_session
from core.models import Business


async def set_local_only(name: str, on: bool) -> int:
    async with owner_session() as session:  # an admin tool: the owner role, not a signed-in user
        business = (
            (await session.execute(select(Business).where(Business.name == name))).scalars().first()
        )
        if business is None:
            print(f'No business named "{name}".')
            return 1
        business.local_only = on
    print(f'Local-only mode is {"on" if on else "off"} for "{name}".')
    return 0


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m workspace.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    local = commands.add_parser("local-only", help="switch local-only mode for a business")
    local.add_argument("--business", default="Solution Center Demo")
    switch = local.add_mutually_exclusive_group(required=True)
    switch.add_argument("--on", action="store_true")
    switch.add_argument("--off", action="store_true")
    args = parser.parse_args(argv)
    try:
        return await set_local_only(args.business, bool(args.on))
    finally:
        await dispose_engines()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
