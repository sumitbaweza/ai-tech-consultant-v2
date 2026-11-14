"""What an answer writer returns, and the citation check every answer goes through.

Shared by the Knowledge Brain's /v1/ask and the Ask and Teach Chat: both ask the model for this
JSON, and both keep only citations that point at sources the model was actually given.
"""

import re

from pydantic import BaseModel, Field

from core.brain_api import SearchHit


class WrittenAnswer(BaseModel):
    answer: str
    answered: bool
    citations: list[str] = Field(default_factory=list)  # source ids: S1, S2 ...
    confidence: float = Field(ge=0, le=1)
    needs_human: bool = False
    human_reason: str = ""


_CITE = re.compile(r"\[(S\d+)\]")


def source_block(hits: list[SearchHit]) -> tuple[str, dict[str, SearchHit]]:
    """The <source> tags the writer reads, numbered S1, S2 ...; and the map back to hits."""
    numbered = {f"S{index}": hit for index, hit in enumerate(hits, start=1)}
    parts = [
        f'<source id="{sid}" type="{hit.knowledge_type}" title="{_attr(hit.title)}">\n{hit.content}\n</source>'
        for sid, hit in numbered.items()
    ]
    return "\n\n".join(parts) if parts else "(no knowledge found)", numbered


def _attr(value: str) -> str:
    return value.replace('"', "'").replace("<", "(").replace(">", ")")[:200]


def check_citations(
    answer: WrittenAnswer, numbered: dict[str, SearchHit]
) -> tuple[list[str], list[str]]:
    """(valid ids, invented ids). Ids come from the JSON list and from [S#] marks in the text;
    an id the writer wasn't given is invented and is dropped from the answer."""
    cited = list(dict.fromkeys([*answer.citations, *_CITE.findall(answer.answer)]))
    valid = [sid for sid in cited if sid in numbered]
    invented = [sid for sid in cited if sid not in numbered]
    return valid, invented


def strip_invented(text: str, invented: list[str]) -> str:
    for sid in invented:
        text = text.replace(f"[{sid}]", "")
    return text
