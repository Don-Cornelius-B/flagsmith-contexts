"""Pure, deterministic multi-context resolution engine (no network I/O)."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from typing import Any

from .schemas import (
    ContextLayer,
    EvaluateRequest,
    FlagEvaluation,
    LayerSnapshot,
    RawFlagState,
)

BASELINE = "global_baseline"
_NON_EXPLICIT_REASONS = frozenset({"DEFAULT", "STATIC"})
_SCALARS = (str, int, float, bool, type(None))


def _scalar(value: Any) -> Any:
    return value if isinstance(value, _SCALARS) else str(value)


def _parse_item(item: Any) -> tuple[str, RawFlagState]:
    if isinstance(item, Mapping):
        feature = item.get("feature")
        name = feature["name"] if isinstance(feature, Mapping) else item.get("feature_name") or item["name"]
        value = item["feature_state_value"] if "feature_state_value" in item else item.get("value")
        return name, RawFlagState(
            enabled=bool(item["enabled"]),
            value=_scalar(value),
            reason=item.get("reason"),
            variant=item.get("variant"),
        )
    return item.feature_name, RawFlagState(
        enabled=bool(item.enabled),
        value=_scalar(getattr(item, "value", None)),
        reason=getattr(item, "reason", None),
        variant=getattr(item, "variant", None),
    )


def parse_sdk_flags(
    identifier: str, raw_flags: Any, fetched_at: float | None = None
) -> LayerSnapshot:
    """Convert SDK `Flags` / iterable of `Flag` / REST JSON into a LayerSnapshot."""
    if hasattr(raw_flags, "all_flags") and callable(raw_flags.all_flags):
        items: Iterable[Any] = raw_flags.all_flags()
    elif isinstance(raw_flags, Mapping):
        items = raw_flags.get("flags") or []
    else:
        items = raw_flags or []
    flags = dict(_parse_item(item) for item in items)
    return LayerSnapshot(
        identifier=identifier,
        flags=flags,
        fetched_at=time.time() if fetched_at is None else fetched_at,
    )


def _is_explicit(reason: str | None) -> bool:
    return reason is not None and reason.strip().upper() not in _NON_EXPLICIT_REASONS


def _is_experiment(flag: RawFlagState) -> bool:
    return flag.variant is not None or "SPLIT" in (flag.reason or "").upper()


def _ident(layer: ContextLayer) -> str:
    return f"{layer.kind}:{layer.id}"


class HierarchicalMerger:
    """Resolve per-layer snapshots into final flag decisions.

    Layers are walked broadest -> narrowest. A layer overrides only on an
    explicit reason or when its state diverges from the environment baseline;
    otherwise the inherited decision (and its `resolved_by`) is preserved.
    """

    def resolve(
        self,
        request: EvaluateRequest,
        baseline: LayerSnapshot | None,
        layer_snapshots: dict[str, LayerSnapshot],
    ) -> dict[str, FlagEvaluation]:
        base = baseline if request.include_baseline else None
        layers = [
            (layer, layer_snapshots[layer.namespaced_id])
            for layer in request.contexts
            if layer.namespaced_id in layer_snapshots
        ]

        names: set[str] = set(base.flags) if base else set()
        for _, snap in layers:
            names.update(snap.flags)

        results: dict[str, FlagEvaluation] = {}
        for name in sorted(names):
            base_flag = base.flags.get(name) if base else None
            current = self._seed(name, base_flag, layers)

            for layer, snap in layers:
                flag = snap.flags.get(name)
                if flag is None:
                    continue
                diverges = base_flag is not None and (flag.enabled, flag.value) != (
                    base_flag.enabled,
                    base_flag.value,
                )
                if _is_explicit(flag.reason) or diverges:
                    current = FlagEvaluation(
                        enabled=flag.enabled,
                        value=flag.value,
                        resolved_by=_ident(layer),
                        reason=flag.reason or "OVERRIDE",
                    )
            results[name] = current

        if request.bucketing_context is not None:
            self._apply_bucketing(request, layer_snapshots, results)
        return results

    @staticmethod
    def _seed(
        name: str,
        base_flag: RawFlagState | None,
        layers: list[tuple[ContextLayer, LayerSnapshot]],
    ) -> FlagEvaluation:
        if base_flag is not None:
            return FlagEvaluation(
                enabled=base_flag.enabled,
                value=base_flag.value,
                resolved_by=BASELINE,
                reason=base_flag.reason or "STATIC",
            )
        for layer, snap in layers:
            flag = snap.flags.get(name)
            if flag is not None:
                return FlagEvaluation(
                    enabled=flag.enabled,
                    value=flag.value,
                    resolved_by=_ident(layer) if _is_explicit(flag.reason) else BASELINE,
                    reason=flag.reason or "STATIC",
                )
        raise KeyError(name)  # unreachable: names come from baseline or layers

    @staticmethod
    def _apply_bucketing(
        request: EvaluateRequest,
        layer_snapshots: dict[str, LayerSnapshot],
        results: dict[str, FlagEvaluation],
    ) -> None:
        bucket_layer = next(
            (c for c in request.contexts if c.kind == request.bucketing_context), None
        )
        if bucket_layer is None:
            return
        snap = layer_snapshots.get(bucket_layer.namespaced_id)
        if snap is None:
            return
        ident = _ident(bucket_layer)
        for name, flag in snap.flags.items():
            if _is_experiment(flag):
                results[name] = FlagEvaluation(
                    enabled=flag.enabled,
                    value=flag.value,
                    resolved_by=ident,
                    reason=flag.reason or "OVERRIDE",
                    experiment_bucketed_by=ident,
                )
