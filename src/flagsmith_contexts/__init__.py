"""flagsmith-contexts: multi-context hierarchical evaluation for Flagsmith."""

from .schemas import (
    CacheBackend,
    ContextLayer,
    EvaluateRequest,
    EvaluateResponse,
    EvaluationMeta,
    FlagEvaluation,
    HealthResponse,
    LayerSnapshot,
    RawFlagState,
    TraitValue,
    TrackEventRequest,
    TrackEventResponse,
    WebhookResponse,
)
from .merger import HierarchicalMerger, parse_sdk_flags

__version__ = "0.1.0"

__all__ = [
    "CacheBackend",
    "HierarchicalMerger",
    "parse_sdk_flags",
    "ContextLayer",
    "EvaluateRequest",
    "EvaluateResponse",
    "EvaluationMeta",
    "FlagEvaluation",
    "HealthResponse",
    "LayerSnapshot",
    "RawFlagState",
    "TraitValue",
    "TrackEventRequest",
    "TrackEventResponse",
    "WebhookResponse",
]
