"""The benchmark (Task 4.1, Step 4): versioned files in benchmark/, loaded into a business's
Eval Lab. A file is the source of truth; loading the same version again changes nothing."""

import uuid
from pathlib import Path
from typing import Any, cast

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import REPO_ROOT
from core.db import tenant_session
from eval_lab.models import Benchmark, Case

BENCHMARK_DIR = REPO_ROOT / "benchmark"
FILES = ("v1/brain.yaml", "v1/url_cases.yaml", "v1/chat.yaml", "v1/teach.yaml", "hidden/v1.yaml")


def read(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


async def load_file(session: AsyncSession, business_id: uuid.UUID, path: Path) -> Benchmark:
    data = read(path)
    existing = (
        await session.execute(
            select(Benchmark).where(
                Benchmark.name == data["name"], Benchmark.version == data["version"]
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    benchmark = Benchmark(
        business_id=business_id,
        name=data["name"],
        version=data["version"],
        kind=data["kind"],
        file_path=path.relative_to(REPO_ROOT).as_posix(),
        hidden=data["kind"] == "hidden",
        case_count=len(data["cases"]),
    )
    session.add(benchmark)
    await session.flush()
    for item in data["cases"]:
        session.add(
            Case(
                business_id=business_id,
                benchmark_id=benchmark.id,
                case_key=item["key"],
                category=item["category"],
                question=item["question"],
                expected=item["expected"],
                expected_sources=list(item.get("sources") or []),
                checks=dict(item.get("checks") or {}),
                teach=item.get("teach"),
            )
        )
    await session.flush()
    return benchmark


async def load_benchmarks(business_id: uuid.UUID) -> dict[str, int]:
    loaded: dict[str, int] = {}
    async with tenant_session(business_id) as session:
        for name in FILES:
            path = BENCHMARK_DIR / name
            if path.exists():
                benchmark = await load_file(session, business_id, path)
                loaded[f"{benchmark.kind} {benchmark.version}"] = benchmark.case_count
    return loaded
