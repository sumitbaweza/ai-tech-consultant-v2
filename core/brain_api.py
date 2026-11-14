"""The Knowledge Brain's API contract: the request and response shapes of its main endpoints.

The Brain's service (knowledge_brain/api) answers with these models and the web app
(core/brain_client.py) reads them, so both sides agree on every field. This is the published
contract, like an OpenAPI file, not the Brain's code: the web app still gets every byte through
HTTP (Task 2.8). Full docs: http://localhost:8081/docs
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

KnowledgeType = Literal[
    "sitemap", "page_url", "instruction", "skill", "past_email", "document", "lesson", "expert"
]
SearchMode = Literal["hybrid", "keyword", "vector"]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    knowledge_types: list[KnowledgeType] | None = None
    limit: int = Field(default=8, ge=1, le=50)
    mode: SearchMode = "hybrid"
    rerank: bool = True


class SearchHit(BaseModel):
    chunk_id: int
    knowledge_type: KnowledgeType
    title: str
    heading: str
    content: str
    url: str | None  # the page, or None for an instruction, skill, lesson or email
    anchor: str | None  # the paragraph's anchor on the page
    score: float  # 0-1 after fusion and re-ranking; higher is more relevant
    page_id: uuid.UUID | None = None
    image_id: uuid.UUID | None = None
    skill_version_id: uuid.UUID | None = None
    instruction_id: uuid.UUID | None = None
    lesson_id: uuid.UUID | None = None
    email_message_id: uuid.UUID | None = None


class SearchResponse(BaseModel):
    query: str
    corrected_query: str | None  # typos fixed with pg_trgm, when any were
    mode: SearchMode
    hits: list[SearchHit]
    took_ms: int


class Instruction(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    enabled: bool
    all_roles: bool
    role_ids: list[uuid.UUID]


class SkillText(BaseModel):
    skill_id: uuid.UUID
    version_id: uuid.UUID
    key: str
    name: str
    version: int
    when_to_use: str
    markdown: str
    match_score: float = 0.0


class LessonText(BaseModel):
    id: uuid.UUID
    topic: str
    title: str
    markdown: str
    version: int
    updated_at: datetime | None = None


class ContextRequest(BaseModel):
    """Everything the chat needs to write one answer, in one call."""

    question: str = Field(min_length=1, max_length=8000)
    role_id: uuid.UUID | None = None
    knowledge_types: list[KnowledgeType] | None = None
    skill_keys: list[str] | None = None  # the role's allowed skills; None = all
    skill_version_id: uuid.UUID | None = None  # test a draft skill version (editor, gate)
    include_lessons: bool = True
    limit: int = Field(default=8, ge=1, le=20)
    local_only: bool = False


class ContextResponse(BaseModel):
    hits: list[SearchHit]
    top_score: float
    corrected_query: str | None
    instructions: list[Instruction]
    skill: SkillText | None
    lessons: list[LessonText]
    facts: list[dict[str, str]]  # from the find-facts step: [{source, fact}]
    took_ms: int


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    role_id: uuid.UUID | None = None
    model_key: str | None = None  # force one of the 14 models (the Eval Lab does)
    skill_version_id: uuid.UUID | None = None
    include_lessons: bool = True
    # The release gate tries a new version of brain.answer_system or brain.answer_user
    prompt_overrides: dict[str, str] | None = None


class Citation(BaseModel):
    n: str  # S1, S2 ...
    chunk_id: int
    knowledge_type: KnowledgeType
    title: str
    url: str | None
    anchor: str | None
    quote: str


class AskResponse(BaseModel):
    answer: str
    answered: bool
    citations: list[Citation]
    knowledge_types: list[KnowledgeType]
    confidence: float
    top_score: float
    model_key: str | None
    cost_usd: float
    latency_ms: int
    trace_id: uuid.UUID | None


class SourceView(BaseModel):
    """What one click on a citation opens (Task 3.2)."""

    chunk_id: int
    knowledge_type: KnowledgeType
    title: str
    heading: str
    content: str  # the cited chunk
    url: str | None
    anchor: str | None
    # The whole page as numbered paragraphs (the same split the chunks use), so the cited
    # paragraphs (anchor p-<first>-<last>) show in place
    page_blocks: list[str] | None = None
    image_url: str | None = None
    image_description: str | None = None
    level: str | None = None


class ExpertTaskIn(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    reason: str
    context: dict[str, str] = Field(default_factory=dict)
