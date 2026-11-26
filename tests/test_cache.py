
import hashlib
import hmac

import pytest

from flagsmith_contexts.cache import (
    TieredContextCache,
    verify_webhook_signature,
)
from flagsmith_contexts.schemas import LayerSnapshot, RawFlagState


# --------------------------------------------------
# Helper: Create sample cached data
# --------------------------------------------------

def make_snapshot(identifier="ctx:org:123"):
    return LayerSnapshot(
        identifier=identifier,
        flags={
            "sso_login": RawFlagState(
                enabled=True,
                value=None,
                reason="TARGETING_MATCH",
            )
        },
        fetched_at=100.0,
    )


# --------------------------------------------------
# 1. Cache miss and cache hit
# --------------------------------------------------

def test_cache_miss():
    cache = TieredContextCache()

    result = cache.get("ctx:org:123")

    assert result is None


def test_cache_hit():
    cache = TieredContextCache()
    snapshot = make_snapshot()

    cache.set("ctx:org:123", snapshot)

    result = cache.get("ctx:org:123")

    assert result == snapshot


# --------------------------------------------------
# 2. TTL expiration
# --------------------------------------------------

def test_expired_entry_returns_none():
    current_time = [100.0]

    cache = TieredContextCache(
        ttl_by_prefix={"ctx:org:": 10},
        clock=lambda: current_time[0],
    )

    snapshot = make_snapshot()
    cache.set("ctx:org:123", snapshot)

    # Move time forward by 11 seconds.
    current_time[0] += 11

    assert cache.get("ctx:org:123") is None


# --------------------------------------------------
# 3. Stale data retrieval
# --------------------------------------------------

def test_get_stale_returns_expired_snapshot():
    current_time = [100.0]

    cache = TieredContextCache(
        ttl_by_prefix={"ctx:org:": 10},
        clock=lambda: current_time[0],
    )

    snapshot = make_snapshot()
    cache.set("ctx:org:123", snapshot)

    current_time[0] += 11

    # Normal get() rejects expired entries.
    assert cache.get("ctx:org:123") is None

    # get_stale() can still retrieve the old snapshot.
    assert cache.get_stale("ctx:org:123") == snapshot


# --------------------------------------------------
# 4. Different TTLs for different context layers
# --------------------------------------------------

def test_different_layer_ttls():
    current_time = [100.0]

    cache = TieredContextCache(
        clock=lambda: current_time[0],
    )

    cache.set("global_baseline", make_snapshot())
    cache.set("ctx:org:123", make_snapshot())
    cache.set("ctx:team:456", make_snapshot())

    # Global TTL = 300 seconds
    # Organization TTL = 180 seconds
    # Team TTL = 120 seconds

    current_time[0] += 130

    assert cache.get("global_baseline") is not None
    assert cache.get("ctx:org:123") is not None
    assert cache.get("ctx:team:456") is None


# --------------------------------------------------
# 5. Explicit TTL override
# --------------------------------------------------

def test_explicit_ttl_overrides_default():
    current_time = [100.0]

    cache = TieredContextCache(
        clock=lambda: current_time[0],
    )

    cache.set(
        "ctx:org:123",
        make_snapshot(),
        ttl_seconds=5,
    )

    current_time[0] += 6

    assert cache.get("ctx:org:123") is None


# --------------------------------------------------
# 6. Cache invalidation
# --------------------------------------------------

def test_invalidate_prefix():
    cache = TieredContextCache()

    cache.set("ctx:org:123", make_snapshot())
    cache.set("ctx:org:456", make_snapshot())
    cache.set("ctx:team:789", make_snapshot())

    removed = cache.invalidate_prefix("ctx:org:")

    assert removed == 2
    assert cache.get_stale("ctx:org:123") is None
    assert cache.get_stale("ctx:org:456") is None
    assert cache.get_stale("ctx:team:789") is not None


# --------------------------------------------------
# 7. Clear entire cache
# --------------------------------------------------

def test_clear_cache():
    cache = TieredContextCache()

    cache.set("ctx:org:123", make_snapshot())
    cache.set("ctx:team:456", make_snapshot())

    cache.clear()

    assert cache.entry_count == 0


# --------------------------------------------------
# 8. Invalid input handling
# --------------------------------------------------

def test_empty_cache_key_raises_error():
    cache = TieredContextCache()

    with pytest.raises(ValueError):
        cache.set("", make_snapshot())


def test_negative_ttl_raises_error():
    with pytest.raises(ValueError):
        TieredContextCache(default_ttl_seconds=-1)


def test_wrong_value_type_raises_error():
    cache = TieredContextCache()

    with pytest.raises(TypeError):
        cache.set("ctx:org:123", {"enabled": True})


# --------------------------------------------------
# 9. Webhook HMAC signature verification
# --------------------------------------------------

def test_valid_webhook_signature():
    payload = b'{"event": "flags_updated"}'
    secret = "test-secret"

    signature = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    assert verify_webhook_signature(
        payload,
        signature,
        secret,
    ) is True


def test_valid_webhook_signature_with_prefix():
    payload = b'{"event": "flags_updated"}'
    secret = "test-secret"

    signature = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    assert verify_webhook_signature(
        payload,
        f"sha256={signature}",
        secret,
    ) is True


def test_invalid_webhook_signature():
    payload = b'{"event": "flags_updated"}'

    assert verify_webhook_signature(
        payload,
        "incorrect-signature",
        "test-secret",
    ) is False


def test_missing_signature_returns_false():
    assert verify_webhook_signature(
        b"test",
        None,
        "test-secret",
    ) is False


def test_missing_secret_returns_false():
    assert verify_webhook_signature(
        b"test",
        "some-signature",
        "",
    ) is False
