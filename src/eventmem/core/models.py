from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def utc(value: str) -> str:
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("A timezone is required")
    return d.astimezone(timezone.utc).isoformat(timespec="microseconds")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scope(Model):
    project: str = "personal"
    persona: str = "default"
    collection: str = "default"
    world: str = "real"

    def key(self) -> str:
        return self.model_dump_json()


Kind = Literal[
    "episode",
    "fact",
    "state",
    "preference",
    "procedure",
    "relationship",
    "commitment",
    "reminder",
    "prediction",
    "diary",
    "summary",
    "portrait",
    "self_narrative",
    "knowledge",
    "checkpoint",
    "observation",
]
State = Literal[
    "active", "unverified", "superseded", "refuted", "retracted", "archived"
]


class SourceInput(Model):
    namespace: str = Field(min_length=1, max_length=200)
    key: str = Field(min_length=1, max_length=500)
    version: str = Field(default="1", min_length=1, max_length=100)
    session: str = ""
    scope: Scope = Field(default_factory=Scope)
    occurred_at: str = Field(default_factory=now)
    text: str = Field(default="", max_length=10_000_000)
    media_type: str = "text/plain"
    title: str = Field(default="", max_length=1000)
    origin: str = ""
    kind: Kind = "observation"
    authority: Literal["explicit", "operation", "document", "model"] = "explicit"
    extract: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    _time = field_validator("occurred_at")(utc)


class RecordInput(Model):
    id: str | None = None
    kind: Kind
    title: str = Field(default="", max_length=1000)
    content: str = Field(min_length=1, max_length=1_000_000)
    scope: Scope = Field(default_factory=Scope)
    source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    valid_from: str = Field(default_factory=now)
    valid_until: str | None = None
    status: State = "active"
    confirmation: Literal[
        "explicit", "observed", "documented", "inferred", "verified"
    ] = "explicit"
    importance: float = Field(default=0.5, ge=0, le=1)
    generated: bool = False
    attributes: dict[str, Any] = Field(default_factory=dict)
    locator: dict[str, Any] = Field(default_factory=dict)

    _time = field_validator("valid_from")(utc)

    @field_validator("valid_until")
    @classmethod
    def check_until(cls, value: str | None) -> str | None:
        return utc(value) if value else None

    @model_validator(mode="after")
    def consistency(self):
        if self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        if self.generated and self.confirmation not in ("inferred", "verified"):
            self.confirmation = "inferred"
        if self.generated and self.kind in (
            "fact",
            "state",
            "preference",
            "relationship",
            "commitment",
        ):
            if self.confirmation != "verified" and self.status == "active":
                self.status = "unverified"
        return self


class RevisionInput(Model):
    expected_revision: int = Field(ge=1)
    command_id: str = Field(min_length=1, max_length=200)
    action: Literal[
        "correct",
        "confirm",
        "retract",
        "replace",
        "refute",
        "archive",
        "restore",
        "rollback",
    ]
    content: str | None = Field(default=None, max_length=1_000_000)
    reason: str = Field(default="", max_length=2000)
    replacement_id: str | None = None
    target_revision: int | None = None
    attributes: dict[str, Any] | None = None


class RecallQuery(Model):
    """Everything a recall may be asked, except what it is for. The chat model's tools take
    this shape, so they cannot declare a purpose: what they recall is experience."""

    query: str = Field(default="", max_length=4000)
    scope: Scope = Field(default_factory=Scope)
    scenario: Literal[
        "tool",
        "knowledge",
        "research",
        "creative",
        "support",
        "operations",
    ] = "tool"
    mode: Literal["fast", "deep"] = "fast"
    at: str | None = None
    known_at: str | None = None
    history: bool = False
    include_shared: bool = True
    kinds: list[Kind] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=100)
    budget: int | None = Field(default=None, ge=0, le=32000)
    session: str | None = None
    phase: Literal["search", "startup", "passive", "read", "compact"] = "search"
    host_mode: Literal["append", "replace"] = "append"
    vector: list[float] | None = Field(default=None, max_length=8192)
    index: str | None = None
    explain: bool = False

    @field_validator("at", "known_at")
    @classmethod
    def check_time(cls, value: str | None) -> str | None:
        return utc(value) if value else None


RecallPurpose = Literal["experience_recall", "audit"]


class RecallRequest(RecallQuery):
    recall_purpose: RecallPurpose = Field(
        default="experience_recall",
        description="Experience recall excludes configuration, synthetic examples and host envelopes. Audit returns all classes with provenance labels.",
    )


class ReminderPolicy(Model):
    id: str = "default"
    scope: Scope = Field(default_factory=Scope)
    enabled: bool = False
    channel: str | None = None
    timezone: str = "UTC"
    quiet_start: int = Field(default=22, ge=0, le=23)
    quiet_end: int = Field(default=8, ge=0, le=23)
    max_per_day: int = Field(default=3, ge=0, le=100)
    min_interval_minutes: int = Field(default=60, ge=0)
    require_confirmation: bool = True
    idempotent_channel: bool = False
    triggers: list[Literal["reminder", "commitment"]] = Field(
        default_factory=lambda: ["reminder", "commitment"]
    )
    allowed_kinds: list[Kind] = Field(
        default_factory=lambda: ["reminder", "commitment"]
    )

    @field_validator("timezone")
    @classmethod
    def zone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo

        ZoneInfo(value)
        return value

    @field_validator("channel")
    @classmethod
    def valid_channel(cls, value: str | None) -> str | None:
        from urllib.parse import urlparse

        if value:
            url = urlparse(value)
            if (
                url.scheme not in ("https", "http")
                or not url.hostname
                or url.username
                or url.password
            ):
                raise ValueError("Use an HTTP(S) callback URL without credentials")
            if url.scheme == "http" and url.hostname not in (
                "127.0.0.1",
                "localhost",
                "::1",
            ):
                raise ValueError("Non-local callbacks require HTTPS")
        return value


class ReminderInput(Model):
    command_id: str
    policy_id: str = "default"
    record_id: str
    due_at: str
    trigger: Literal["reminder", "commitment"] = "reminder"
    recurrence: Literal["none", "daily", "weekly", "yearly"] = "none"
    _time = field_validator("due_at")(utc)


class ModelRole(Model):
    endpoint: str
    model: str
    protocol: Literal["openai", "anthropic"] = "openai"
    api_key_env: str | None = None
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_output_tokens: int = Field(default=8192, ge=1, le=131072)
    reasoning_effort: str | None = None
    dimensions: int | None = Field(default=None, ge=1, le=8192)
    preprocessing: str = "text-v1"
    local_embedding: bool = False
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)


class Page(Model):
    items: list[dict[str, Any]]
    cursor: str | None = None


class MaintenanceSettings(Model):
    concurrency: int = Field(default=2, ge=1)
    job_timeout_seconds: float = Field(
        default=300, ge=0.1, le=3600, allow_inf_nan=False
    )
    interval_seconds: float = Field(default=60, ge=30, le=86400, allow_inf_nan=False)


class RankingSettings(Model):
    use_weight: float = Field(default=0, ge=0, allow_inf_nan=False)
    half_life_days: float | None = Field(default=30, ge=0, allow_inf_nan=False)


class BudgetSettings(Model):
    startup: int = Field(default=2000, ge=0, le=128000)
    passive: int = Field(default=256, ge=0, le=128000)
    cumulative: int = Field(default=12000, ge=0, le=128000)


class ScenarioSettings(Model):
    preferred_kinds: list[Kind] = Field(default_factory=list)
    max_rounds: int = Field(default=3, ge=0)


def validate_settings(key, value):
    """Validate once before persistence, including direct Python callers."""
    if not isinstance(value, dict):
        raise ValueError("Settings must be an object")
    schema = {"maintenance": MaintenanceSettings, "ranking": RankingSettings}.get(key)
    if schema:
        return schema.model_validate(value, strict=True).model_dump(exclude_unset=True)
    schema = {"budgets": BudgetSettings, "scenarios": ScenarioSettings}.get(key)
    if schema:
        return {
            name: schema.model_validate(config, strict=True).model_dump(
                exclude_unset=True
            )
            for name, config in value.items()
        }
    return value


class Result(Model):
    id: str
    revision: int
    status: str
    source_ids: list[str] = Field(default_factory=list)
