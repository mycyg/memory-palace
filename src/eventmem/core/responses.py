"""Public output schemas shared by HTTP and generated SDK contracts."""

from typing import Any
from pydantic import BaseModel, ConfigDict
from .models import Kind, State


class Output(BaseModel):
    model_config = ConfigDict(extra="allow")


class ScopeResult(BaseModel):
    project: str
    persona: str
    collection: str
    world: str


class SourceResult(Output):
    id: str
    namespace: str
    key: str
    version: str
    scope: ScopeResult
    status: str
    revision: int
    source_ids: list[str]
    mechanical: str
    model: str
    received_at: str
    occurred_at: str
    read_url: str
    title: str
    media_type: str
    byte_length: int
    attachment_url: str


class RecordResult(Output):
    id: str
    kind: Kind
    title: str
    content: str
    scope: ScopeResult
    status: State
    revision: int
    source_ids: list[str]
    generated: bool
    confirmation: str
    valid_from: str
    valid_until: str | None
    received_at: str
    read_url: str
    locator: dict[str, Any]


class RecallItem(Output):
    id: str
    title: str
    kind: Kind
    status: State
    revision: int
    source_ids: list[str]
    read_url: str
    locator: dict[str, Any]
    generated: bool
    confirmation: str


class RecallResult(Output):
    items: list[RecallItem]
    text: str
    tokens: int
    budget: int
    accounts: dict[str, int]
    generation: int
    latency_ms: float
    cursor: str | None
    session_used: int
    instruction_authority: str
