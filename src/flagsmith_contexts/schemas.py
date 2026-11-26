"""Shared Pydantic v2 data contracts for flagsmith-contexts."""

from __future__ import annotations

import re
from typing import Annotated, Any, Protocol
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TraitValue = str | int | float | bool | None

_KIND_RE = re.compile(r"^[a-z0-9_]+$")


class ContextLayer(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    id: str = Field(min_length=1)
    traits: dict[str, TraitValue] = Field(default_factory=dict)
    transient: bool = True

    @field_validator("kind")
    @classmethod
    def _normalize_kind(cls, v: str) -> str:
        v = v.strip().lower()
        if not _KIND_RE.fullmatch(v):
            raise ValueError(
                "kind must be non-empty and contain only lowercase alphanumerics or underscores"
            )
        return v

    @property
    def namespaced_id(self) -> str:
        return f"ctx:{self.kind}:{quote(self.id, safe='_-')}"


def validate_context_chain(
    contexts: list[ContextLayer], bucketing_context: str | None
) -> None:
    """Shared chain rules: non-empty, unique (kind, id), bucketing kind present."""
    if not contexts:
        raise ValueError("contexts must contain at least one layer")
    seen: set[tuple[str, str]] = set()
    for layer in contexts:
        key = (layer.kind, layer.id)
        if key in seen:
            raise ValueError(f"duplicate context layer: {layer.kind}:{layer.id}")
        seen.add(key)
    if bucketing_context is not None and not any(
        layer.kind == bucketing_context for layer in contexts
    ):
        raise ValueError(
            f"bucketing_context '{bucketing_context}' not found among context kinds"
        )


class ContextChainMixin(BaseModel):
    model_config = ConfigDict(frozen=True)

    contexts: Annotated[list[ContextLayer], Field(min_length=1)]
    bucketing_context: str | None = None

    @model_validator(mode="after")
    def _check_chain(self) -> "ContextChainMixin":
        validate_context_chain(self.contexts, self.bucketing_context)
        return self


class EvaluateRequest(ContextChainMixin):
    include_baseline: bool = True


class TrackEventRequest(ContextChainMixin):
    event_name: str = Field(min_length=1)
    bucketing_context: str | None = "org"
    value: TraitValue = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RawFlagState(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool
    value: TraitValue = None
    reason: str | None = None
    variant: str | None = None


class LayerSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    identifier: str
    flags: dict[str, RawFlagState]
    fetched_at: float


class FlagEvaluation(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool
    value: TraitValue = None
    resolved_by: str
    reason: str
    experiment_bucketed_by: str | None = None


class EvaluationMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    hierarchy: list[str]
    cache_hits: int = 0
    cache_misses: int = 0
    stale: bool = False
    evaluation_ms: float = 0.0


class EvaluateResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    flags: dict[str, FlagEvaluation]
    meta: EvaluationMeta


class TrackEventResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str = "tracked"
    attributed_to: str
    event_name: str


class WebhookResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str = "invalidated"
    purged_keys: int = 0


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str = "ok"
    flagsmith_connected: bool = True
    cache_entries: int = 0


class CacheBackend(Protocol):
    def get(self, key: str) -> LayerSnapshot | None: ...

    def get_stale(self, key: str) -> LayerSnapshot | None: ...

    def set(
        self, key: str, value: LayerSnapshot, ttl_seconds: int | None = None
    ) -> None: ...

    def invalidate_prefix(self, prefix: str) -> int: ...

    def clear(self) -> None: ...
