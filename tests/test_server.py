
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from flagsmith_contexts.server import create_app
from flagsmith_contexts.schemas import (
    EvaluateResponse,
    EvaluationMeta,
    FlagEvaluation,
)


class MockContextEngine:

    def __init__(self):
        self.evaluate_called = False
        self.event_called = False
        self.should_fail = False

    def get_context_flags(self, request):
        self.evaluate_called = True

        if self.should_fail:
            raise RuntimeError("Flagsmith unavailable")

        return EvaluateResponse(
            flags={
                "sso_login": FlagEvaluation(
                    enabled=True,
                    value=None,
                    resolved_by="org:acme_corp",
                    reason="TARGETING_MATCH",
                )
            },
            meta=EvaluationMeta(
                hierarchy=["org:acme_corp"],
                cache_hits=1,
                cache_misses=0,
                stale=False,
                evaluation_ms=2.5,
            ),
        )

    def track_context_event(self, request):
        self.event_called = True

        if self.should_fail:
            raise RuntimeError("Event tracking failed")


class MockCache:

    def __init__(self):
        self.cleared = False
        self._entry_count = 3

    @property
    def entry_count(self):
        return self._entry_count

    def get(self, key):
        return None

    def get_stale(self, key):
        return None

    def set(self, key, value, ttl_seconds=None):
        pass

    def invalidate_prefix(self, prefix):
        return 0

    def clear(self):
        self.cleared = True
        self._entry_count = 0


@pytest.fixture
def setup_client():
    engine = MockContextEngine()
    cache = MockCache()

    app = create_app(
        engine=engine,
        cache=cache,
        webhook_secret="test-secret",
    )

    return TestClient(app), engine, cache


def test_health_endpoint(setup_client):
    client, _, _ = setup_client

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["flagsmith_connected"] is True
    assert response.json()["cache_entries"] == 3


def test_evaluate_endpoint(setup_client):
    client, engine, _ = setup_client

    payload = {
        "contexts": [
            {
                "kind": "org",
                "id": "acme_corp",
                "traits": {"plan": "enterprise"},
                "transient": True,
            }
        ],
        "bucketing_context": "org",
        "include_baseline": True,
    }

    response = client.post("/v1/evaluate", json=payload)

    assert response.status_code == 200
    assert engine.evaluate_called is True
    assert response.json()["flags"]["sso_login"]["enabled"] is True


def test_evaluate_rejects_invalid_payload(setup_client):
    client, _, _ = setup_client

    response = client.post(
        "/v1/evaluate",
        json={"contexts": []},
    )

    assert response.status_code == 422


def test_evaluate_handles_engine_failure(setup_client):
    client, engine, _ = setup_client
    engine.should_fail = True

    payload = {
        "contexts": [
            {"kind": "org", "id": "acme_corp"}
        ]
    }

    response = client.post("/v1/evaluate", json=payload)

    assert response.status_code == 502


def test_event_tracking_endpoint(setup_client):
    client, engine, _ = setup_client

    payload = {
        "event_name": "purchase_completed",
        "contexts": [
            {"kind": "org", "id": "acme_corp"}
        ],
        "bucketing_context": "org",
        "value": 100,
        "metadata": {"currency": "USD"},
    }

    response = client.post("/v1/events", json=payload)

    assert response.status_code == 202
    assert engine.event_called is True
    assert response.json()["status"] == "tracked"
    assert response.json()["event_name"] == "purchase_completed"


def test_event_tracking_handles_failure(setup_client):
    client, engine, _ = setup_client
    engine.should_fail = True

    payload = {
        "event_name": "purchase_completed",
        "contexts": [
            {"kind": "org", "id": "acme_corp"}
        ],
    }

    response = client.post("/v1/events", json=payload)

    assert response.status_code == 502


def generate_signature(body, secret):
    return hmac.new(
        secret.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()


def test_webhook_rejects_missing_signature(setup_client):
    client, _, _ = setup_client

    response = client.post(
        "/v1/webhooks/flagsmith",
        json={"event": "feature.updated"},
    )

    assert response.status_code == 401


def test_webhook_rejects_invalid_signature(setup_client):
    client, _, cache = setup_client

    response = client.post(
        "/v1/webhooks/flagsmith",
        json={"event": "feature.updated"},
        headers={
            "X-Flagsmith-Signature": "invalid-signature"
        },
    )

    assert response.status_code == 401
    assert cache.cleared is False


def test_webhook_clears_cache(setup_client):
    client, _, cache = setup_client

    payload = b'{"event":"feature.updated"}'

    signature = generate_signature(
        payload,
        "test-secret",
    )

    response = client.post(
        "/v1/webhooks/flagsmith",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Flagsmith-Signature": signature,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "invalidated"
    assert cache.cleared is True


def test_evaluate_without_engine():
    app = create_app(cache=MockCache())
    client = TestClient(app)

    payload = {
        "contexts": [
            {"kind": "org", "id": "acme_corp"}
        ]
    }

    response = client.post("/v1/evaluate", json=payload)

    assert response.status_code == 503


def test_webhook_without_secret():
    app = create_app(cache=MockCache())
    client = TestClient(app)

    response = client.post(
        "/v1/webhooks/flagsmith",
        json={"event": "feature.updated"},
        headers={
            "X-Flagsmith-Signature": "anything"
        },
    )

    assert response.status_code == 503
