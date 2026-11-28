"""Live end-to-end tests against the local Flagsmith stack.

Run with:  .venv\\Scripts\\python.exe -m pytest -m integration -v
Requires FLAGSMITH_ENV_KEY (and optionally FLAGSMITH_API_URL) in .env and a
running server (`.\\infra\\start-flagsmith.ps1`).
"""

import hashlib
import hmac
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from flagsmith_contexts import ContextFlagsmith
from flagsmith_contexts.schemas import ContextLayer, EvaluateRequest
from flagsmith_contexts.server import create_app

pytestmark = pytest.mark.integration

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

ENV_KEY = os.getenv("FLAGSMITH_ENV_KEY")
API_URL = os.getenv("FLAGSMITH_API_URL") or "http://localhost:8000/api/v1/"
WEBHOOK_SECRET = "test_secret"
ORG = "org:acme_corp"


def _server_up() -> bool:
    parts = urlsplit(API_URL)
    try:
        return httpx.get(f"{parts.scheme}://{parts.netloc}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def client():
    if not ENV_KEY:
        pytest.skip("FLAGSMITH_ENV_KEY is not set; skipping live integration tests")
    if not _server_up():
        pytest.skip(f"Flagsmith is not reachable at {API_URL}; run .\\infra\\start-flagsmith.ps1")
    c = ContextFlagsmith(ENV_KEY, api_url=API_URL)
    yield c
    close = getattr(c.sdk_client, "close", None)
    if callable(close):
        close()


def chain(user: str, role: str = "member") -> list[ContextLayer]:
    return [
        ContextLayer(kind="org", id="acme_corp", traits={"plan": "enterprise"}),
        ContextLayer(kind="user", id=user, traits={"role": role}),
    ]


def test_live_hierarchical_inheritance(client):
    client.cache.clear()
    resp = client.get_context_flags(contexts=chain("don"))
    flag = resp.flags["sso_login"]
    assert flag.enabled is True
    assert flag.resolved_by == ORG
    assert "TARGETING_MATCH" in flag.reason
    assert resp.meta.stale is False


def test_live_cache_hit_acceleration(client):
    client.get_context_flags(contexts=chain("don"))  # warm baseline + org
    resp = client.get_context_flags(contexts=chain("alice"))
    assert resp.meta.cache_hits >= 2, resp.meta
    assert resp.meta.stale is False
    assert resp.flags["sso_login"].resolved_by == ORG


def test_live_account_level_experiment_bucketing(client):
    results = [
        client.get_context_flags(
            EvaluateRequest(contexts=chain(user), bucketing_context="org")
        )
        for user in ("don", "alice")
    ]
    if "onboarding_flow" not in results[0].flags:
        pytest.skip("Feature 'onboarding_flow' not found in the Flagsmith environment")

    org_raw = client.cache.get_stale("ctx:org:acme_corp").flags.get("onboarding_flow")
    don, alice = (r.flags["onboarding_flow"] for r in results)
    for flag in (don, alice):
        assert flag.experiment_bucketed_by == ORG, (
            f"org snapshot was not treated as an experiment: {org_raw!r}"
        )
        assert flag.resolved_by == ORG
    assert (don.enabled, don.value, don.reason) == (alice.enabled, alice.value, alice.reason)


def test_live_sidecar_and_webhook_invalidation(client):
    client.cache.clear()
    http = TestClient(
        create_app(engine=client, cache=client.cache, webhook_secret=WEBHOOK_SECRET)
    )
    body = {
        "contexts": [layer.model_dump() for layer in chain("don")],
    }
    r = http.post("/v1/evaluate", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["flags"]["sso_login"]["resolved_by"] == ORG

    payload = json.dumps({"event_type": "FLAG_UPDATED", "data": {}}).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    r = http.post(
        "/v1/webhooks/flagsmith",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Flagsmith-Signature": f"sha256={signature}",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "invalidated"
    assert r.json()["purged_keys"] >= 2
    assert client.cache.get_stale("global_baseline") is None


def test_live_event_attribution(client):
    resp = client.track_context_event(
        event_name="onboarding_completed",
        contexts=chain("don"),
        bucketing_context="org",
        value=1,
    )
    assert resp.attributed_to == ORG
    assert resp.event_name == "onboarding_completed"
    assert resp.status == "tracked"
