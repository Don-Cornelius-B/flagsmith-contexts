
"""Tiered TTL cache and webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .schemas import LayerSnapshot


@dataclass(frozen=True)
class _CacheEntry:
    """Internal representation of a cached snapshot."""

    value: LayerSnapshot
    expires_at: float


class TieredContextCache:
    """
    Thread-safe in-memory cache for global and upper-context snapshots.

    Supported key conventions:
        global_baseline
        ctx:org:<organization_id>
        ctx:team:<team_id>
    """

    DEFAULT_TTLS = {
        "global_baseline": 300,
        "ctx:org:": 180,
        "ctx:team:": 120,
    }

    def __init__(
        self,
        default_ttl_seconds: int = 60,
        ttl_by_prefix: Mapping[str, int] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if default_ttl_seconds < 0:
            raise ValueError("default_ttl_seconds cannot be negative")

        self._default_ttl_seconds = default_ttl_seconds
        self._ttl_by_prefix = {
            **self.DEFAULT_TTLS,
            **(ttl_by_prefix or {}),
        }

        for prefix, ttl in self._ttl_by_prefix.items():
            if ttl < 0:
                raise ValueError(
                    f"TTL for {prefix!r} cannot be negative"
                )

        self._clock = clock
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = threading.RLock()

    def _resolve_ttl(self, key: str) -> int:
        """Find the most specific configured TTL for a cache key."""

        if key in self._ttl_by_prefix:
            return self._ttl_by_prefix[key]

        matching_prefixes = [
            prefix
            for prefix in self._ttl_by_prefix
            if key.startswith(prefix)
        ]

        if not matching_prefixes:
            return self._default_ttl_seconds

        longest_prefix = max(matching_prefixes, key=len)

        return self._ttl_by_prefix[longest_prefix]

    def get(self, key: str) -> LayerSnapshot | None:
        """Return a cached snapshot if it has not expired."""

        with self._lock:
            entry = self._entries.get(key)

            if entry is None:
                return None

            if self._clock() >= entry.expires_at:
                return None

            return entry.value

    def get_stale(self, key: str) -> LayerSnapshot | None:
        """Return a stored snapshot even if its TTL has expired."""

        with self._lock:
            entry = self._entries.get(key)

            if entry is None:
                return None

            return entry.value

    def set(
        self,
        key: str,
        value: LayerSnapshot,
        ttl_seconds: int | None = None,
    ) -> None:
        """Store a snapshot with an explicit or configured TTL."""

        if not key:
            raise ValueError("cache key cannot be empty")

        if not isinstance(value, LayerSnapshot):
            raise TypeError("value must be a LayerSnapshot")

        ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else self._resolve_ttl(key)
        )

        if ttl < 0:
            raise ValueError("ttl_seconds cannot be negative")

        entry = _CacheEntry(
            value=value,
            expires_at=self._clock() + ttl,
        )

        with self._lock:
            self._entries[key] = entry

    def invalidate_prefix(self, prefix: str) -> int:
        """Delete all cached entries whose keys start with a prefix."""

        with self._lock:
            matching_keys = [
                key
                for key in self._entries
                if key.startswith(prefix)
            ]

            for key in matching_keys:
                del self._entries[key]

            return len(matching_keys)

    def clear(self) -> None:
        """Remove all cached snapshots."""

        with self._lock:
            self._entries.clear()

    @property
    def entry_count(self) -> int:
        """Return the number of stored entries, including expired ones."""

        with self._lock:
            return len(self._entries)


def verify_webhook_signature(
    payload: bytes,
    signature: str | None,
    secret: str,
) -> bool:
    """
    Verify a webhook signature using HMAC-SHA256.

    Accepts either:
        <hexadecimal digest>
        sha256=<hexadecimal digest>
    """

    if not signature or not secret:
        return False

    supplied_signature = signature.strip()

    if supplied_signature.startswith("sha256="):
        supplied_signature = supplied_signature[len("sha256="):]

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(
        expected_signature,
        supplied_signature,
    )
