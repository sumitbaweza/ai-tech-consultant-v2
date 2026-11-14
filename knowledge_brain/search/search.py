"""Search that finds the right knowledge (Task 2.7, DATABASE.md D8).

1. Typos: each question word the business's vocabulary doesn't know is replaced by its closest
   known word (pg_trgm similarity >= 0.45): "custmize" -> "customize".
2. Synonyms: the business's own synonym list adds words ("plan" also searches "membership").
   Word forms need nothing: the english text-search config stems "custom", "customize" and
   "customization" to the same word.
3. Two candidate lists: keywords (Postgres full-text, any question word, ranked by ts_rank_cd)
   and meaning (pgvector cosine distance on the HNSW index), 40 each. Without a vector (no
   Voyage key, local-only mode) only the keyword list runs.
4. Reciprocal rank fusion merges them: score = sum of 1 / (60 + rank). It needs no tuning,
   because it uses ranks, not the two scores, which don't compare.
5. Re-ranking: the fused top candidates are scored again on 0-1: how much of the question's
   wording they cover, whether their heading names the topic, and their meaning similarity.
   That score is what the "I don't know" line is compared with.

Only searchable chunks count: quarantined and archived pages, switched-off instructions and
lessons, retired skill versions never appear.
"""

import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.brain_api import SearchHit, SearchMode, SearchResponse
from knowledge_brain.search.embed import Embedder

CANDIDATES = 40
RRF_K = 60
TYPO_SIMILARITY = 0.2  # candidates only: the edit distance decides
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")


@dataclass
class Candidate:
    chunk_id: int
    rrf: float = 0.0
    similarity: float | None = None


def edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein (optimal string alignment): insertions, deletions, substitutions and
    swaps of two neighbouring letters each cost 1. "domian" -> "domain" is 1 (a swap)."""
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, char_b in enumerate(b, start=1):
            cost = 0 if char_a == char_b else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and char_a == b[j - 2] and a[i - 2] == char_b:
                current[j] = min(current[j], previous2[j - 2] + 1)
        previous2, previous = previous, current
    return previous[len(b)]


def allowed_edits(word: str) -> int:
    return 1 if len(word) <= 5 else 2


async def fix_typos(session: AsyncSession, query: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace unknown words with the closest known word of this business's knowledge.

    pg_trgm finds candidates fast through its GIN index (shared three-letter pieces, a low bar
    so swapped letters still qualify); the edit distance then decides: 1 edit for short words,
    2 for longer ones, the more common word on a tie.
    """
    words = {w.lower() for w in _WORD.findall(query) if len(w) >= 4}
    if not words:
        return query, []
    known = set(
        (
            await session.execute(
                text("SELECT term FROM brain.terms WHERE term = ANY(:words)"),
                {"words": list(words)},
            )
        ).scalars()
    )
    await session.execute(text(f"SET LOCAL pg_trgm.similarity_threshold = {TYPO_SIMILARITY}"))
    fixes: list[tuple[str, str]] = []
    for word in sorted(words - known):
        rows = (
            await session.execute(
                text(
                    "SELECT term, doc_count FROM brain.terms WHERE term % :w "
                    "ORDER BY similarity(term, :w) DESC, doc_count DESC LIMIT 25"
                ),
                {"w": word},
            )
        ).all()
        scored = [
            (edit_distance(word, str(row.term)), -int(row.doc_count), str(row.term)) for row in rows
        ]
        scored = [item for item in scored if item[0] <= allowed_edits(word)]
        if scored:
            fixes.append((word, min(scored)[2]))
    corrected = query
    for wrong, right in fixes:
        corrected = re.sub(rf"\b{re.escape(wrong)}\b", right, corrected, flags=re.IGNORECASE)
    return corrected, fixes


async def synonyms_for(session: AsyncSession, query: str) -> list[str]:
    words = [w.lower() for w in _WORD.findall(query)]
    if not words:
        return []
    rows = await session.execute(
        text("SELECT synonyms FROM brain.synonyms WHERE term = ANY(:words)"), {"words": words}
    )
    extra: list[str] = []
    for (values,) in rows:
        extra.extend(str(v) for v in cast(list[str], values))
    return extra


async def lexemes(session: AsyncSession, value: str) -> list[str]:
    """The stemmed words Postgres indexes for this text (english config)."""
    found = await session.scalar(
        text("SELECT tsvector_to_array(to_tsvector('english', :t))"), {"t": value}
    )
    words = cast(list[str], found or [])
    return [str(w) for w in words if re.fullmatch(r"[a-z0-9]+", str(w))]


def _types_clause(knowledge_types: list[str] | None) -> str:
    return " AND c.knowledge_type = ANY(:types)" if knowledge_types else ""


async def keyword_candidates(
    session: AsyncSession, words: list[str], knowledge_types: list[str] | None
) -> list[int]:
    if not words:
        return []
    # Any question word may match (OR); ts_rank_cd ranks chunks with more of them, closer
    # together, higher. Heading words carry weight A, body words weight B.
    rows = await session.execute(
        text(
            f"""
            SELECT c.id
            FROM brain.chunks c, to_tsquery('simple', :q) AS query
            WHERE c.searchable AND c.tsv @@ query {_types_clause(knowledge_types)}
            ORDER BY ts_rank_cd(c.tsv, query, 32) DESC, c.id
            LIMIT :n
            """
        ),
        {"q": " | ".join(words), "types": knowledge_types, "n": CANDIDATES},
    )
    return [int(row[0]) for row in rows]


async def vector_candidates(
    session: AsyncSession, vector: list[float], knowledge_types: list[str] | None
) -> list[tuple[int, float]]:
    # pgvector 0.8: keep scanning the HNSW graph until enough rows pass the filters
    await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    await session.execute(text("SET LOCAL hnsw.ef_search = 100"))
    rows = await session.execute(
        text(
            f"""
            SELECT c.id, 1 - (c.embedding <=> CAST(:v AS halfvec)) AS similarity
            FROM brain.chunks c
            WHERE c.searchable AND c.embedding IS NOT NULL {_types_clause(knowledge_types)}
            ORDER BY c.embedding <=> CAST(:v AS halfvec)
            LIMIT :n
            """
        ),
        {
            "v": "[" + ",".join(f"{x:.6f}" for x in vector) + "]",
            "types": knowledge_types,
            "n": CANDIDATES,
        },
    )
    return [(int(row[0]), float(row[1])) for row in rows]


def fuse(keyword_ids: list[int], vector_hits: list[tuple[int, float]]) -> dict[int, Candidate]:
    candidates: dict[int, Candidate] = {}
    for rank, chunk_id in enumerate(keyword_ids, start=1):
        candidates.setdefault(chunk_id, Candidate(chunk_id)).rrf += 1 / (RRF_K + rank)
    for rank, (chunk_id, similarity) in enumerate(vector_hits, start=1):
        candidate = candidates.setdefault(chunk_id, Candidate(chunk_id))
        candidate.rrf += 1 / (RRF_K + rank)
        candidate.similarity = similarity
    return candidates


DETAILS_SQL = """
SELECT c.id, c.knowledge_type, c.heading, c.content, c.anchor,
       c.page_id, c.image_id, c.instruction_id, c.skill_version_id, c.lesson_doc_id,
       c.email_message_id,
       tsvector_to_array(c.tsv) AS words,
       tsvector_to_array(to_tsvector('english', c.heading)) AS heading_words,
       coalesce(p.title, i.title, s.name, l.title, t.subject, c.heading) AS title,
       CASE WHEN p.origin IN ('sitemap', 'page_url') THEN p.url END AS url
FROM brain.chunks c
LEFT JOIN brain.pages p ON p.id = c.page_id
LEFT JOIN brain.instructions i ON i.id = c.instruction_id
LEFT JOIN brain.skill_versions sv ON sv.id = c.skill_version_id
LEFT JOIN brain.skills s ON s.id = sv.skill_id
LEFT JOIN brain.lesson_docs l ON l.id = c.lesson_doc_id
LEFT JOIN brain.email_messages m ON m.id = c.email_message_id
LEFT JOIN brain.email_threads t ON t.id = m.thread_id
WHERE c.id = ANY(:ids)
"""


def rerank_score(
    query_words: set[str], chunk_words: set[str], heading_words: set[str], similarity: float | None
) -> float:
    """0-1: wording covered, heading on topic, and meaning, when a vector was compared."""
    if not query_words:
        lexical = heading = 0.0
    else:
        lexical = len(query_words & chunk_words) / len(query_words)
        heading = len(query_words & heading_words) / len(query_words)
    if similarity is not None:
        score = 0.55 * max(similarity, 0.0) + 0.35 * lexical + 0.10 * heading
    else:
        score = 0.80 * lexical + 0.20 * heading
    return round(min(score, 1.0), 4)


async def search(
    session: AsyncSession,
    query: str,
    *,
    knowledge_types: list[str] | None = None,
    limit: int = 8,
    mode: SearchMode = "hybrid",
    rerank: bool = True,
    embedder: Embedder | None = None,
    query_vector: list[float] | None = None,
) -> SearchResponse:
    started = time.monotonic()
    corrected, fixes = await fix_typos(session, query)
    extra = await synonyms_for(session, corrected)
    words = await lexemes(session, " ".join([corrected, *extra]))
    keyword_ids = (
        await keyword_candidates(session, words, knowledge_types) if mode != "vector" else []
    )
    vector_hits: list[tuple[int, float]] = []
    if mode != "keyword":
        if query_vector is None and embedder is not None:
            query_vector = (await embedder.embed([corrected], "query"))[0]
        if query_vector is not None:
            vector_hits = await vector_candidates(session, query_vector, knowledge_types)
    candidates = fuse(keyword_ids, vector_hits)
    if not candidates:
        return SearchResponse(
            query=query,
            corrected_query=corrected if fixes else None,
            mode=mode,
            hits=[],
            took_ms=int((time.monotonic() - started) * 1000),
        )
    rows = (await session.execute(text(DETAILS_SQL), {"ids": list(candidates)})).mappings().all()
    query_words = set(await lexemes(session, corrected))
    max_rrf = max(c.rrf for c in candidates.values()) or 1.0
    hits: list[SearchHit] = []
    for row in rows:
        candidate = candidates[int(row["id"])]
        if rerank:
            score = rerank_score(
                query_words,
                set(row["words"] or []),
                set(row["heading_words"] or []),
                candidate.similarity,
            )
        elif mode == "vector" and candidate.similarity is not None:
            score = round(candidate.similarity, 4)
        else:
            score = round(candidate.rrf / max_rrf, 4)
        hits.append(_hit(row, score))
    hits.sort(key=lambda hit: (-hit.score, hit.chunk_id))
    return SearchResponse(
        query=query,
        corrected_query=corrected if fixes else None,
        mode=mode,
        hits=hits[:limit],
        took_ms=int((time.monotonic() - started) * 1000),
    )


def _hit(row: Any, score: float) -> SearchHit:
    def as_uuid(value: object) -> uuid.UUID | None:
        return value if isinstance(value, uuid.UUID) else None

    return SearchHit(
        chunk_id=int(row["id"]),
        knowledge_type=row["knowledge_type"],
        title=str(row["title"] or ""),
        heading=str(row["heading"]),
        content=str(row["content"]),
        url=row["url"],
        anchor=row["anchor"],
        score=score,
        page_id=as_uuid(row["page_id"]),
        image_id=as_uuid(row["image_id"]),
        skill_version_id=as_uuid(row["skill_version_id"]),
        instruction_id=as_uuid(row["instruction_id"]),
        lesson_id=as_uuid(row["lesson_doc_id"]),
        email_message_id=as_uuid(row["email_message_id"]),
    )
