import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from flagsmith_contexts import ContextFlagsmith
from flagsmith_contexts.cache import TieredContextCache
from flagsmith_contexts.schemas import ContextLayer, EvaluateRequest, TrackEventRequest
from flagsmith_contexts.server import create_app

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


ENV = load("real_environment_flags.json")
ORG = load("real_org_identity.json")
USER = load("real_user_identity.json")


def identity_payload(identifier: str, **_):
    if identifier == "ctx:org:acme_corp":
        return ORG
    if identifier.startswith("ctx:user:"):
        return {**USER, "identifier": identifier}
    return {"identifier": identifier, "flags": []}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def sdk():
    mock = MagicMock()
    mock.get_environment_flags.return_value = ENV
    mock.get_identity_flags.side_effect = identity_payload
    return mock


@pytest.fixture
def client(sdk, clock):
    return ContextFlagsmith(
        "test-key", cache=TieredContextCache(clock=clock), sdk_client=sdk
    )


def chain(user="don"):
    return [
        ContextLayer(kind="org", id="acme_corp", traits={"plan": "enterprise"}),
        ContextLayer(kind="user", id=user, traits={"role": "admin"}),
    ]


def test_end_to_end_user_inherits_org_flag(client, sdk):
    resp = client.get_context_flags(EvaluateRequest(contexts=chain()))
    flag = resp.flags["sso_login"]
    assert flag.enabled is True
    assert flag.resolved_by == "org:acme_corp"
    assert flag.reason == "TARGETING_MATCH; segment=enterprise_plan"
    assert resp.meta.hierarchy == ["ctx:org:acme_corp", "ctx:user:don"]
    assert (resp.meta.cache_hits, resp.meta.cache_misses) == (0, 3)
    assert resp.meta.stale is False
    assert resp.meta.evaluation_ms >= 0
    sdk.get_identity_flags.assert_any_call(
        identifier="ctx:org:acme_corp", traits={"plan": "enterprise"}, transient=True
    )
    sdk.get_identity_flags.assert_any_call(
        identifier="ctx:user:don", traits={"role": "admin"}, transient=True
    )


def test_cache_reuse_across_users_in_same_org(client, sdk):
    client.get_context_flags(contexts=chain("don"))
    sdk.get_environment_flags.reset_mock()
    sdk.get_identity_flags.reset_mock()

    resp = client.get_context_flags(contexts=chain("alice"))
    assert resp.meta.cache_hits >= 2
    assert resp.meta.cache_misses == 1
    sdk.get_environment_flags.assert_not_called()
    assert [c.kwargs["identifier"] for c in sdk.get_identity_flags.call_args_list] == [
        "ctx:user:alice"
    ]
    assert resp.flags["sso_login"].resolved_by == "org:acme_corp"


def test_include_baseline_false_skips_environment(client, sdk):
    resp = client.get_context_flags(contexts=chain(), include_baseline=False)
    sdk.get_environment_flags.assert_not_called()
    assert resp.meta.cache_misses == 2


def test_requires_request_or_contexts(client):
    with pytest.raises(ValueError):
        client.get_context_flags()


def test_stale_if_error_after_expiry(client, sdk, clock):
    fresh = client.get_context_flags(contexts=chain())
    clock.now += 10_000  # everything expired
    sdk.get_environment_flags.side_effect = ConnectionError("down")
    sdk.get_identity_flags.side_effect = ConnectionError("down")

    resp = client.get_context_flags(contexts=chain())
    assert resp.meta.stale is True
    assert resp.meta.cache_hits == 0
    assert resp.flags == fresh.flags


def test_error_without_stale_snapshot_raises(client, sdk):
    sdk.get_environment_flags.side_effect = ConnectionError("down")
    with pytest.raises(ConnectionError):
        client.get_context_flags(contexts=chain())


def test_track_event_attributed_to_org(client, sdk):
    resp = client.track_context_event(
        TrackEventRequest(
            event_name="checkout_completed",
            contexts=chain(),
            value=49.5,
            metadata={"source": "test"},
        )
    )
    assert resp.attributed_to == "org:acme_corp"
    assert resp.event_name == "checkout_completed"
    assert resp.status == "tracked"
    sdk.get_identity_flags.assert_called_once_with(
        identifier="ctx:org:acme_corp", traits={"plan": "enterprise"}, transient=True
    )
    args, kwargs = sdk.track_event.call_args
    assert args == ("checkout_completed",)
    assert kwargs["identifier"] == "ctx:org:acme_corp"
    assert kwargs["value"] == 49.5
    assert kwargs["metadata"]["source"] == "test"
    assert kwargs["metadata"]["contexts"] == ["ctx:org:acme_corp", "ctx:user:don"]


def test_track_event_kwargs_user_bucketing(client, sdk):
    resp = client.track_context_event(
        event_name="click", contexts=chain(), bucketing_context="user"
    )
    assert resp.attributed_to == "user:don"
    assert sdk.track_event.call_args.kwargs["identifier"] == "ctx:user:don"


def test_track_event_falls_back_to_flags_track_event():
    flags = MagicMock()
    sdk = MagicMock(spec=["get_environment_flags", "get_identity_flags"])
    sdk.get_identity_flags.return_value = flags
    client = ContextFlagsmith("k", sdk_client=sdk)
    client.track_context_event(event_name="signup", contexts=chain(), value=1)
    flags.track_event.assert_called_once_with(event_name="signup", event_value=1)


def test_ping(client, sdk):
    assert client.ping() is True
    sdk.get_environment_flags.side_effect = ConnectionError("down")
    assert client.ping() is False


def test_server_smoke_with_real_client(client):
    app = create_app(engine=client, cache=client.cache, webhook_secret="s")
    http = TestClient(app)
    body = {
        "contexts": [
            {"kind": "org", "id": "acme_corp", "traits": {"plan": "enterprise"}},
            {"kind": "user", "id": "don"},
        ]
    }
    r = http.post("/v1/evaluate", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["flags"]["sso_login"]["enabled"] is True
    assert data["flags"]["sso_login"]["resolved_by"] == "org:acme_corp"

    r = http.post("/v1/events", json={**body, "event_name": "checkout_completed"})
    assert r.status_code == 202

    health = http.get("/health").json()
    assert health["cache_entries"] > 0
