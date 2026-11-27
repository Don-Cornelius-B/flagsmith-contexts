"""ContextFlagsmith: multi-context Flagsmith client with tiered caching."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .cache import TieredContextCache
from .merger import HierarchicalMerger, parse_sdk_flags
from .schemas import (
    CacheBackend,
    ContextLayer,
    EvaluateRequest,
    EvaluateResponse,
    EvaluationMeta,
    LayerSnapshot,
    TraitValue,
    TrackEventRequest,
    TrackEventResponse,
)

BASELINE_KEY = "global_baseline"


class ContextFlagsmith:
    """Evaluates ordered context chains against Flagsmith.

    Satisfies `server.ContextEngine`, so it can be passed directly to
    `create_app(engine=client, cache=client.cache)`.
    """

    def __init__(
        self,
        environment_key: str,
        api_url: str = "http://localhost:8000/api/v1/",
        cache: CacheBackend | None = None,
        sdk_client: Any | None = None,
        **flagsmith_kwargs: Any,
    ) -> None:
        if sdk_client is None:
            from flagsmith import Flagsmith

            flagsmith_kwargs.setdefault("enable_events", True)
            sdk_client = Flagsmith(
                environment_key=environment_key, api_url=api_url, **flagsmith_kwargs
            )
        self.sdk_client = sdk_client
        self.cache: CacheBackend = cache if cache is not None else TieredContextCache()
        self.merger = HierarchicalMerger()

    # -- internal ---------------------------------------------------------

    def _fetch(
        self, key: str, loader: Callable[[], Any]
    ) -> tuple[LayerSnapshot, bool, bool]:
        """Return (snapshot, cache_hit, stale). Stale-if-error on SDK failure."""
        cached = self.cache.get(key)
        if cached is not None:
            return cached, True, False
        try:
            snapshot = parse_sdk_flags(key, loader())
        except Exception:
            stale = self.cache.get_stale(key)
            if stale is None:
                raise
            return stale, False, True
        self.cache.set(key, snapshot)
        return snapshot, False, False

    # -- public API -------------------------------------------------------

    def get_context_flags(
        self,
        request: EvaluateRequest | None = None,
        *,
        contexts: list[ContextLayer] | None = None,
        bucketing_context: str | None = None,
        include_baseline: bool = True,
    ) -> EvaluateResponse:
        started = time.perf_counter()
        if request is None:
            if contexts is None:
                raise ValueError("Provide either an EvaluateRequest or contexts")
            request = EvaluateRequest(
                contexts=contexts,
                bucketing_context=bucketing_context,
                include_baseline=include_baseline,
            )

        hits = misses = 0
        stale = False

        baseline: LayerSnapshot | None = None
        if request.include_baseline:
            baseline, hit, was_stale = self._fetch(
                BASELINE_KEY, self.sdk_client.get_environment_flags
            )
            hits += hit
            misses += not hit
            stale |= was_stale

        snapshots: dict[str, LayerSnapshot] = {}
        for layer in request.contexts:
            key = layer.namespaced_id
            snapshot, hit, was_stale = self._fetch(
                key,
                lambda layer=layer: self.sdk_client.get_identity_flags(
                    identifier=layer.namespaced_id,
                    traits=dict(layer.traits),
                    transient=layer.transient,
                ),
            )
            snapshots[key] = snapshot
            hits += hit
            misses += not hit
            stale |= was_stale

        flags = self.merger.resolve(request, baseline, snapshots)
        return EvaluateResponse(
            flags=flags,
            meta=EvaluationMeta(
                hierarchy=[c.namespaced_id for c in request.contexts],
                cache_hits=hits,
                cache_misses=misses,
                stale=stale,
                evaluation_ms=(time.perf_counter() - started) * 1000,
            ),
        )

    def track_context_event(
        self,
        request: TrackEventRequest | None = None,
        *,
        event_name: str | None = None,
        contexts: list[ContextLayer] | None = None,
        bucketing_context: str = "org",
        value: TraitValue = None,
        metadata: dict[str, Any] | None = None,
    ) -> TrackEventResponse:
        if request is None:
            request = TrackEventRequest(
                event_name=event_name or "",
                contexts=contexts or [],
                bucketing_context=bucketing_context,
                value=value,
                metadata=metadata or {},
            )
        kind = request.bucketing_context or "org"
        target = next((c for c in request.contexts if c.kind == kind), None)
        if target is None:  # only reachable if bucketing_context was None and no org
            raise ValueError(f"No context layer of kind {kind!r}")

        traits = dict(target.traits)
        flags = self.sdk_client.get_identity_flags(
            identifier=target.namespaced_id, traits=traits, transient=target.transient
        )
        event_metadata = {
            **request.metadata,
            "contexts": [c.namespaced_id for c in request.contexts],
            "bucketing_context": kind,
            "attributed_to": target.namespaced_id,
        }
        sdk_track = getattr(self.sdk_client, "track_event", None)
        if callable(sdk_track):
            sdk_track(
                request.event_name,
                identifier=target.namespaced_id,
                value=request.value,
                traits=traits,
                metadata=event_metadata,
            )
        elif callable(getattr(flags, "track_event", None)):
            flags.track_event(event_name=request.event_name, event_value=request.value)
        else:
            raise RuntimeError("Flagsmith SDK client does not support event tracking")

        return TrackEventResponse(
            attributed_to=f"{target.kind}:{target.id}", event_name=request.event_name
        )

    def ping(self) -> bool:
        try:
            self.sdk_client.get_environment_flags()
        except Exception:
            return False
        return True
