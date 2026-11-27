import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from flagsmith.models import Flag, Flags

from flagsmith_contexts import HierarchicalMerger, parse_sdk_flags
from flagsmith_contexts.schemas import (
    ContextLayer,
    EvaluateRequest,
    LayerSnapshot,
    RawFlagState,
)

FIXTURES = Path(__file__).parent / "fixtures"
merger = HierarchicalMerger()


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def snap(identifier: str, **flags: RawFlagState) -> LayerSnapshot:
    return LayerSnapshot(identifier=identifier, flags=flags, fetched_at=0.0)


def layers(*pairs: tuple[str, str]) -> list[dict]:
    return [{"kind": k, "id": i} for k, i in pairs]


def ns(kind: str, id_: str) -> str:
    return ContextLayer(kind=kind, id=id_).namespaced_id


BASE_OFF = snap("env", feat=RawFlagState(enabled=False, value=None, reason="STATIC"))


# --- Fixture replay -----------------------------------------------------------


def test_real_fixture_replay_org_wins_over_default_user():
    baseline = parse_sdk_flags("env", load("real_environment_flags.json"), 1.0)
    org = parse_sdk_flags("ctx:org:acme_corp", load("real_org_identity.json"), 1.0)
    user = parse_sdk_flags("ctx:user:don", load("real_user_identity.json"), 1.0)
    req = EvaluateRequest(
        contexts=[
            {"kind": "org", "id": "acme_corp", "traits": {"plan": "enterprise"}},
            {"kind": "user", "id": "don", "traits": {"role": "admin"}},
        ]
    )
    # Snapshot identifiers line up with ContextLayer.namespaced_id
    assert [c.namespaced_id for c in req.contexts] == [org.identifier, user.identifier]
    assert user.flags["sso_login"] == RawFlagState(enabled=False, reason="DEFAULT")

    result = merger.resolve(
        req, baseline, {org.identifier: org, user.identifier: user}
    )["sso_login"]
    assert result.enabled is True
    assert result.resolved_by == "org:acme_corp"
    assert result.reason == "TARGETING_MATCH; segment=enterprise_plan"
    assert result.experiment_bucketed_by is None


def test_baseline_only_when_no_layer_matches():
    baseline = parse_sdk_flags("env", load("real_environment_flags.json"), 1.0)
    user = parse_sdk_flags("ctx:user:don", load("real_user_identity.json"), 1.0)
    req = EvaluateRequest(contexts=[{"kind": "user", "id": "don"}])
    result = merger.resolve(req, baseline, {user.identifier: user})["sso_login"]
    assert (result.enabled, result.resolved_by, result.reason) == (
        False,
        "global_baseline",
        "STATIC",
    )


# --- Child override precedence ------------------------------------------------


@pytest.mark.parametrize("child_kind", ["team", "user"])
@pytest.mark.parametrize("reason", ["OVERRIDE", "TARGETING_MATCH; segment=beta_off"])
def test_explicit_child_overrides_parent_even_when_equal_to_baseline(child_kind, reason):
    req = EvaluateRequest(contexts=layers(("org", "acme"), (child_kind, "c1")))
    snaps = {
        ns("org", "acme"): snap("o", feat=RawFlagState(enabled=True, reason="TARGETING_MATCH; segment=ent")),
        # Child value equals baseline -> only the reason check can detect the override
        ns(child_kind, "c1"): snap("c", feat=RawFlagState(enabled=False, reason=reason)),
    }
    result = merger.resolve(req, BASE_OFF, snaps)["feat"]
    assert result.enabled is False
    assert result.resolved_by == f"{child_kind}:c1"
    assert result.reason == reason


def test_default_reason_but_diverging_from_baseline_overrides():
    # Reason check alone would miss this; baseline dual-check catches it.
    req = EvaluateRequest(contexts=layers(("org", "acme"), ("user", "u")))
    snaps = {
        ns("org", "acme"): snap("o", feat=RawFlagState(enabled=True, value="a", reason="TARGETING_MATCH")),
        ns("user", "u"): snap("u", feat=RawFlagState(enabled=True, value="b", reason="DEFAULT")),
    }
    base = snap("env", feat=RawFlagState(enabled=False, value="x", reason="STATIC"))
    result = merger.resolve(req, base, snaps)["feat"]
    assert (result.value, result.resolved_by, result.reason) == ("b", "user:u", "DEFAULT")


def test_none_reason_diverging_uses_override_reason():
    req = EvaluateRequest(contexts=layers(("user", "u")))
    snaps = {ns("user", "u"): snap("u", feat=RawFlagState(enabled=True))}
    result = merger.resolve(req, BASE_OFF, snaps)["feat"]
    assert (result.enabled, result.resolved_by, result.reason) == (True, "user:u", "OVERRIDE")


def test_default_matching_baseline_inherits_ancestor():
    req = EvaluateRequest(contexts=layers(("org", "acme"), ("team", "t"), ("user", "u")))
    org_flag = RawFlagState(enabled=True, value="on", reason="IDENTITY_OVERRIDE")
    default = RawFlagState(enabled=False, value=None, reason="DEFAULT")
    snaps = {
        ns("org", "acme"): snap("o", feat=org_flag),
        ns("team", "t"): snap("t", feat=default),
        ns("user", "u"): snap("u", feat=default),
    }
    result = merger.resolve(req, BASE_OFF, snaps)["feat"]
    assert (result.enabled, result.value, result.resolved_by) == (True, "on", "org:acme")
    assert result.reason == "IDENTITY_OVERRIDE"


# --- Arbitrary depth -----------------------------------------------------------


def test_five_layer_chain():
    chain = [("region", "eu"), ("org", "acme"), ("team", "plat"), ("user", "don"), ("session", "s1")]
    req = EvaluateRequest(contexts=layers(*chain))
    default = RawFlagState(enabled=False, reason="DEFAULT")
    base = snap(
        "env",
        a=RawFlagState(enabled=False, reason="STATIC"),
        b=RawFlagState(enabled=False, reason="STATIC"),
        c=RawFlagState(enabled=False, reason="STATIC"),
    )
    snaps = {
        ns("region", "eu"): snap("r", a=RawFlagState(enabled=True, value=1, reason="TARGETING_MATCH; segment=eu"), b=default, c=default),
        ns("org", "acme"): snap("o", a=default, b=RawFlagState(enabled=True, value=2, reason="TARGETING_MATCH; segment=ent"), c=default),
        ns("team", "plat"): snap("t", a=default, b=default, c=default),
        ns("user", "don"): snap("u", a=default, b=default, c=RawFlagState(enabled=True, value=3, reason="IDENTITY_OVERRIDE")),
        ns("session", "s1"): snap("s", a=default, b=default, c=RawFlagState(enabled=True, value=4, reason="TARGETING_MATCH; segment=mobile")),
    }
    out = merger.resolve(req, base, snaps)
    assert (out["a"].value, out["a"].resolved_by) == (1, "region:eu")
    assert (out["b"].value, out["b"].resolved_by) == (2, "org:acme")
    assert (out["c"].value, out["c"].resolved_by) == (4, "session:s1")


def test_missing_layer_snapshot_is_skipped():
    req = EvaluateRequest(contexts=layers(("org", "acme"), ("team", "t"), ("user", "u")))
    snaps = {ns("org", "acme"): snap("o", feat=RawFlagState(enabled=True, reason="TARGETING_MATCH"))}
    assert merger.resolve(req, BASE_OFF, snaps)["feat"].resolved_by == "org:acme"


# --- Account-level experiment bucketing ----------------------------------------


def _experiment_snaps(user_id: str, user_variant: str):
    return {
        ns("org", "acme_corp"): snap(
            "o",
            onboarding=RawFlagState(enabled=True, value="guided_ai", reason="SPLIT; weight=50", variant="guided_ai"),
        ),
        ns("user", user_id): snap(
            "u",
            onboarding=RawFlagState(enabled=True, value=user_variant, reason="SPLIT; weight=50", variant=user_variant),
        ),
    }


def test_bucketing_pins_org_variant_across_users():
    base = snap("env", onboarding=RawFlagState(enabled=True, value="control", reason="STATIC"))
    outcomes = []
    for user_id, user_variant in [("alice", "control"), ("bob", "guided_ai")]:
        req = EvaluateRequest(
            contexts=layers(("org", "acme_corp"), ("user", user_id)),
            bucketing_context="org",
        )
        outcomes.append(merger.resolve(req, base, _experiment_snaps(user_id, user_variant))["onboarding"])
    for r in outcomes:
        assert r.value == "guided_ai"
        assert r.resolved_by == "org:acme_corp"
        assert r.experiment_bucketed_by == "org:acme_corp"
        assert r.reason == "SPLIT; weight=50"
    assert outcomes[0] == outcomes[1]


def test_without_bucketing_users_diverge():
    base = snap("env", onboarding=RawFlagState(enabled=True, value="control", reason="STATIC"))
    values = set()
    for user_id, user_variant in [("alice", "control"), ("bob", "guided_ai")]:
        req = EvaluateRequest(contexts=layers(("org", "acme_corp"), ("user", user_id)))
        r = merger.resolve(req, base, _experiment_snaps(user_id, user_variant))["onboarding"]
        assert r.resolved_by == f"user:{user_id}"
        assert r.experiment_bucketed_by is None
        values.add(r.value)
    assert values == {"control", "guided_ai"}


def test_bucketing_split_reason_without_variant_and_non_experiment_untouched():
    req = EvaluateRequest(contexts=layers(("org", "acme"), ("user", "u")), bucketing_context="org")
    snaps = {
        ns("org", "acme"): snap(
            "o",
            exp=RawFlagState(enabled=True, value="B", reason="PERCENTAGE_SPLIT"),
            plain=RawFlagState(enabled=True, reason="TARGETING_MATCH"),
        ),
        ns("user", "u"): snap(
            "u",
            exp=RawFlagState(enabled=True, value="A", reason="IDENTITY_OVERRIDE"),
            plain=RawFlagState(enabled=False, reason="IDENTITY_OVERRIDE"),
        ),
    }
    out = merger.resolve(req, None, snaps)
    assert (out["exp"].value, out["exp"].experiment_bucketed_by) == ("B", "org:acme")
    assert (out["plain"].resolved_by, out["plain"].experiment_bucketed_by) == ("user:u", None)


# --- No baseline / include_baseline=False --------------------------------------


def test_no_baseline_seeds_from_first_layer():
    req = EvaluateRequest(contexts=layers(("org", "acme"), ("user", "u")))
    snaps = {
        ns("org", "acme"): snap("o", feat=RawFlagState(enabled=False, reason="DEFAULT")),
        ns("user", "u"): snap("u", feat=RawFlagState(enabled=False, reason="DEFAULT")),
    }
    r = merger.resolve(req, None, snaps)["feat"]
    assert (r.enabled, r.resolved_by, r.reason) == (False, "global_baseline", "DEFAULT")


def test_include_baseline_false_ignores_baseline():
    req = EvaluateRequest(contexts=layers(("user", "u")), include_baseline=False)
    base = snap("env", only_in_base=RawFlagState(enabled=True), feat=RawFlagState(enabled=False))
    snaps = {ns("user", "u"): snap("u", feat=RawFlagState(enabled=True, reason="DEFAULT"))}
    out = merger.resolve(req, base, snaps)
    assert "only_in_base" not in out
    # Without baseline, a DEFAULT layer cannot "diverge" -> stays global_baseline
    assert (out["feat"].enabled, out["feat"].resolved_by) == (True, "global_baseline")


# --- parse_sdk_flags ---------------------------------------------------------


def test_parse_raw_json_list_and_identity_dict():
    env = parse_sdk_flags("env", load("real_environment_flags.json"), 5.0)
    assert env.flags == {"sso_login": RawFlagState(enabled=False, reason="STATIC")}
    assert env.fetched_at == 5.0
    org = parse_sdk_flags("ctx:org:acme_corp", load("real_org_identity.json"))
    assert org.flags["sso_login"].enabled is True
    assert org.fetched_at > 0


def _sdk_flag(name, enabled, value=None, reason=None, variant=None):
    return Flag(enabled=enabled, value=value, feature_id=1, feature_name=name, reason=reason, variant=variant)


def test_parse_sdk_flags_object():
    flags = Flags(flags={
        "sso_login": _sdk_flag("sso_login", True, reason="TARGETING_MATCH; segment=enterprise_plan"),
        "theme": _sdk_flag("theme", True, value="dark", reason="SPLIT", variant="dark"),
    })
    s = parse_sdk_flags("ctx:org:acme_corp", flags, 1.0)
    assert s.flags["sso_login"].reason == "TARGETING_MATCH; segment=enterprise_plan"
    assert s.flags["theme"] == RawFlagState(enabled=True, value="dark", reason="SPLIT", variant="dark")


def test_parse_iterable_of_flags_and_duck_typed_objects():
    s = parse_sdk_flags("x", [_sdk_flag("a", True, value=3)], 1.0)
    assert s.flags["a"] == RawFlagState(enabled=True, value=3)
    duck = SimpleNamespace(feature_name="b", enabled=False, value=None, reason="DEFAULT", variant=None)
    assert parse_sdk_flags("x", [duck], 1.0).flags["b"].reason == "DEFAULT"


def test_parse_empty_inputs():
    assert parse_sdk_flags("x", [], 1.0).flags == {}
    assert parse_sdk_flags("x", {"flags": []}, 1.0).flags == {}
    assert parse_sdk_flags("x", Flags(), 1.0).flags == {}
