from __future__ import annotations

import hashlib
import math
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(slots=True)
class _Bucket:
    started_at: float
    count: int


class FixedWindowRateLimiter:
    """Small bounded in-memory limiter for the single-process API topology."""

    def __init__(
        self,
        *,
        window_seconds: int,
        clock: Callable[[], float] = time.monotonic,
        cleanup_interval: int = 256,
        max_buckets: int = 10_000,
    ) -> None:
        self.window_seconds = window_seconds
        self._clock = clock
        self._cleanup_interval = cleanup_interval
        self._max_buckets = max_buckets
        self._buckets: dict[str, _Bucket] = {}
        self._operations = 0
        self._lock = threading.Lock()

    def consume(self, *, keys: Iterable[tuple[str, int]]) -> int | None:
        """Consume all keys atomically, returning Retry-After seconds when blocked."""
        now = self._clock()
        requested = tuple(keys)
        with self._lock:
            self._maybe_cleanup(now=now)
            retry_after = 0
            for key, limit in requested:
                bucket = self._current_bucket(key=key, now=now)
                if bucket.count >= limit:
                    retry_after = max(
                        retry_after,
                        max(1, math.ceil(self.window_seconds - (now - bucket.started_at))),
                    )
            if retry_after:
                return retry_after
            for key, _ in requested:
                self._current_bucket(key=key, now=now).count += 1
            return None

    def retry_after(self, *, key: str, limit: int) -> int | None:
        now = self._clock()
        with self._lock:
            self._maybe_cleanup(now=now)
            bucket = self._current_bucket(key=key, now=now)
            if bucket.count < limit:
                return None
            return max(1, math.ceil(self.window_seconds - (now - bucket.started_at)))

    def record(self, *, key: str) -> None:
        now = self._clock()
        with self._lock:
            self._maybe_cleanup(now=now)
            self._current_bucket(key=key, now=now).count += 1

    def clear(self, *, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    def _current_bucket(self, *, key: str, now: float) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None or now - bucket.started_at >= self.window_seconds:
            if bucket is None and len(self._buckets) >= self._max_buckets:
                oldest_key = min(
                    self._buckets, key=lambda candidate: self._buckets[candidate].started_at
                )
                self._buckets.pop(oldest_key, None)
            bucket = _Bucket(started_at=now, count=0)
            self._buckets[key] = bucket
        return bucket

    def _maybe_cleanup(self, *, now: float) -> None:
        self._operations += 1
        if self._operations % self._cleanup_interval:
            return
        stale_before = now - self.window_seconds
        self._buckets = {
            key: bucket for key, bucket in self._buckets.items() if bucket.started_at > stale_before
        }


def client_rate_limit_identity(request: Request) -> str:
    """Use only Starlette's proxy-resolved peer; never parse forwarding headers here."""
    host = request.client.host if request.client is not None else "unknown"
    return hashlib.sha256(host.encode("utf-8")).hexdigest()


def account_rate_limit_identity(value: str) -> str:
    normalized = value.strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def enforce_rate_limit(*, limiter: FixedWindowRateLimiter, keys: Iterable[tuple[str, int]]) -> None:
    retry_after = limiter.consume(keys=keys)
    if retry_after is not None:
        raise_rate_limited(retry_after=retry_after)


def raise_rate_limited(*, retry_after: int) -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "error": "rate_limit_exceeded",
            "message": "Too many attempts. Try again later.",
        },
        headers={"Retry-After": str(retry_after)},
    )


def request_origin_is_allowed(*, request: Request, public_web_base_url: str) -> bool:
    if request.method.upper() not in _UNSAFE_METHODS:
        return True
    origin = request.headers.get("origin")
    if origin is not None:
        return _canonical_origin(origin) == _canonical_origin(public_web_base_url)
    return request.headers.get("sec-fetch-site", "").casefold() != "cross-site"


def _canonical_origin(value: str) -> tuple[str, str, int] | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.hostname.casefold(), effective_port


__all__ = [
    "FixedWindowRateLimiter",
    "account_rate_limit_identity",
    "client_rate_limit_identity",
    "enforce_rate_limit",
    "raise_rate_limited",
    "request_origin_is_allowed",
]
