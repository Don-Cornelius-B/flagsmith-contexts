import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from flagsmith_contexts.schemas import (
    ContextLayer,
    EvaluateRequest,
    EvaluateResponse,
    EvaluationMeta,
    FlagEvaluation,
    HealthResponse,
    LayerSnapshot,
    RawFlagState,
    TrackEventRequest,
    TrackEventResponse,
    WebhookResponse,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def chain() -> list[dict]:
    return [
        {"kind": "org", "id": "acme_corp", "traits": {"plan": "enterprise"}},
        {"kind": "team", "id": "platform"},
        {"kind": "user", "id": "don", "traits": {"role": "admin"}},
    ]


# --- ContextLayer -----------------------------------------------------------


def test_valid_chain_and_defaults():
    req = EvaluateRequest(contexts=chain())
    assert [c.kind for c in req.contexts] == ["org", "team", "user"]
    assert req.include_baseline is True
    assert req.bucketing_context is None
    assert req.contexts[0].transient is True
    assert req.contexts[1].traits == {}


def test_namespaced_id_escaping():
    assert ContextLayer(kind="org", id="acme_corp").namespaced_id == "ctx:org:acme_corp"
    assert (
        ContextLayer(kind="user", id="user@acme.com").namespaced_id
        == "ctx:user:user%40acme.com"
    )
    assert ContextLayer(kind="user", id="a:b/c d").namespaced_id == "ctx:user:a%3Ab%2Fc%20d"


def test_kind_is_normalized():
    assert ContextLayer(kind="  Org ", id="x").kind == "org"


@pytest.mark.parametrize("kind", ["", "   ", "org:team", ":", "my-kind", "a b"])
def test_invalid_kind_rejected(kind):
    with pytest.raises(ValidationError):
        ContextLayer(kind=kind, id="x")


def test_empty_id_rejected():
    with pytest.raises(ValidationError):
        ContextLayer(kind="org", id="")


@pytest.mark.parametrize("bad", [{"a": 1}, [1, 2]])
def test_nested_trait_values_rejected(bad):
    with pytest.raises(ValidationError):
        ContextLayer(kind="org", id="x", traits={"k": bad})


def test_scalar_trait_values_accepted():
    layer = ContextLayer(
        kind="org", id="x", traits={"a": "s", "b": 1, "c": 1.5, "d": True, "e": None}
    )
    assert layer.traits["e"] is None


def test_models_are_frozen():
    layer = ContextLayer(kind="org", id="x")
    with pytest.raises(ValidationError):
        layer.id = "y"


# --- Chain validation (both request models) ---------------------------------


@pytest.mark.parametrize("model", [EvaluateRequest, TrackEventRequest])
def test_empty_contexts_rejected(model):
    kwargs = {"event_name": "e"} if model is TrackEventRequest else {}
    with pytest.raises(ValidationError):
        model(contexts=[], **kwargs)


@pytest.mark.parametrize("model", [EvaluateRequest, TrackEventRequest])
def test_duplicate_pairs_rejected(model):
    kwargs = {"event_name": "e"} if model is TrackEventRequest else {}
    dup = [{"kind": "org", "id": "a"}, {"kind": "org", "id": "a"}]
    with pytest.raises(ValidationError, match="duplicate"):
        model(contexts=dup, **kwargs)


def test_same_kind_different_id_allowed():
    req = EvaluateRequest(
        contexts=[{"kind": "org", "id": "a"}, {"kind": "org", "id": "b"}]
    )
    assert len(req.contexts) == 2


def test_evaluate_missing_bucketing_context_rejected():
    with pytest.raises(ValidationError, match="bucketing_context"):
        EvaluateRequest(contexts=chain(), bucketing_context="session")
    assert EvaluateRequest(contexts=chain(), bucketing_context="team")


def test_track_event_defaults_and_bucketing():
    req = TrackEventRequest(event_name="purchase", contexts=chain())
    assert req.bucketing_context == "org"
    assert req.value is None
    assert req.metadata == {}
    with pytest.raises(ValidationError, match="bucketing_context"):
        TrackEventRequest(event_name="purchase", contexts=chain()[1:])  # no org
    with pytest.raises(ValidationError, match="bucketing_context"):
        TrackEventRequest(event_name="p", contexts=chain(), bucketing_context="session")


def test_track_event_empty_name_rejected():
    with pytest.raises(ValidationError):
        TrackEventRequest(event_name="", contexts=chain())


# --- Fixture-backed serialization -------------------------------------------


def raw_state(flag: dict) -> RawFlagState:
    return RawFlagState(
        enabled=flag["enabled"],
        value=flag["feature_state_value"],
        reason=flag.get("reason"),
        variant=flag.get("variant"),
    )


def snapshot_from(fixture: str) -> LayerSnapshot:
    data = load(fixture)
    return LayerSnapshot(
        identifier=data["identifier"],
        flags={f["feature"]["name"]: raw_state(f) for f in data["flags"]},
        fetched_at=1_700_000_000.0,
    )


def roundtrip(model):
    cls = type(model)
    assert cls.model_validate(model.model_dump()) == model
    assert cls.model_validate_json(model.model_dump_json()) == model


def test_environment_flags_fixture():
    flags = load("real_environment_flags.json")
    state = raw_state(flags[0])
    assert state == RawFlagState(enabled=False, value=None, reason="STATIC", variant=None)
    roundtrip(state)


def test_layer_snapshots_from_fixtures():
    org = snapshot_from("real_org_identity.json")
    user = snapshot_from("real_user_identity.json")
    assert org.identifier == "ctx:org:acme_corp"
    assert org.flags["sso_login"].enabled is True
    assert org.flags["sso_login"].reason == "TARGETING_MATCH; segment=enterprise_plan"
    assert user.identifier == "ctx:user:don"
    assert user.flags["sso_login"].enabled is False
    assert user.flags["sso_login"].reason == "DEFAULT"
    roundtrip(org)
    roundtrip(user)


def test_identifier_matches_context_layer_encoding():
    org = ContextLayer(kind="org", id="acme_corp")
    assert org.namespaced_id == load("real_org_identity.json")["identifier"]


def test_response_models_roundtrip():
    org = snapshot_from("real_org_identity.json")
    user = snapshot_from("real_user_identity.json")
    evaluation = FlagEvaluation(
        enabled=org.flags["sso_login"].enabled,
        value=org.flags["sso_login"].value,
        resolved_by="org:acme_corp",
        reason=org.flags["sso_login"].reason,
    )
    inherited = FlagEvaluation(
        enabled=False,
        resolved_by="global_baseline",
        reason=user.flags["sso_login"].reason,
        experiment_bucketed_by="org",
    )
    meta = EvaluationMeta(hierarchy=["org:acme_corp", "user:don"], cache_hits=1)
    response = EvaluateResponse(
        flags={"sso_login": evaluation, "other": inherited}, meta=meta
    )
    assert meta.cache_misses == 0 and meta.stale is False and meta.evaluation_ms == 0.0
    for model in (
        evaluation,
        inherited,
        meta,
        response,
        TrackEventResponse(attributed_to="org:acme_corp", event_name="purchase"),
        WebhookResponse(purged_keys=2),
        HealthResponse(),
    ):
        roundtrip(model)
    assert TrackEventResponse(attributed_to="x", event_name="e").status == "tracked"
    assert WebhookResponse().status == "invalidated"
    assert HealthResponse().status == "ok"
