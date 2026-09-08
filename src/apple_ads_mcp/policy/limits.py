"""Personal-use execution safeguards (PLAN.md §7.4).

Protects against accidental AI-client loops: rolling-hour tool-call budget,
per-call subrequest ceiling, duplicate-call suppression. In-memory by design —
the personal-use profile pins Cloud Run to a single instance.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque


class LimitExceeded(RuntimeError):
    pass


class RollingWindowLimiter:
    def __init__(self, max_events: int, window_seconds: float = 3600.0) -> None:
        self._max = max_events
        self._window = window_seconds
        self._events: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            while self._events and now - self._events[0] > self._window:
                self._events.popleft()
            if len(self._events) >= self._max:
                retry_in = int(self._window - (now - self._events[0])) + 1
                raise LimitExceeded(
                    f"tool-call budget reached ({self._max} calls per rolling "
                    f"hour); retry in ~{retry_in}s or narrow your workflow"
                )
            self._events.append(now)


class SubrequestBudget:
    """Per-tool-call ceiling on upstream Apple Ads requests."""

    def __init__(self, max_subrequests: int) -> None:
        self._max = max_subrequests
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        return self._used

    def spend(self, n: int = 1) -> None:
        with self._lock:
            if self._used + n > self._max:
                raise LimitExceeded(
                    f"subrequest ceiling reached ({self._max} upstream requests "
                    "per tool call); narrow the date range, add filters, or "
                    "request fewer entities"
                )
            self._used += n


class DuplicateSuppressor:
    """Suppress identical calls received in rapid succession.

    Returns True when the call should be *rejected* as a rapid duplicate.
    Keyed by tool name + canonicalized arguments; never returns cached data,
    so results can never leak across accounts.
    """

    def __init__(self, window_seconds: float = 2.0, max_keys: int = 512) -> None:
        self._window = window_seconds
        self._seen: dict[str, float] = {}
        self._max_keys = max_keys
        self._lock = threading.Lock()

    @staticmethod
    def key(tool: str, args: dict) -> str:
        canonical = json.dumps(args, sort_keys=True, default=str)
        return hashlib.sha256(f"{tool}:{canonical}".encode()).hexdigest()

    def is_rapid_duplicate(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            last = self._seen.get(key)
            self._seen[key] = now
            if len(self._seen) > self._max_keys:
                cutoff = now - self._window
                self._seen = {k: t for k, t in self._seen.items() if t >= cutoff}
            return last is not None and (now - last) < self._window


def check_date_range(days: float, max_days: int) -> None:
    if days > max_days:
        raise LimitExceeded(
            f"date range of {days:.0f} days exceeds the {max_days}-day ceiling; "
            "split the request into smaller ranges"
        )
    if days <= 0:
        raise LimitExceeded("ends_at must be after starts_at")


def check_entity_ids(ids: list[str] | None, max_ids: int) -> None:
    if ids and len(ids) > max_ids:
        raise LimitExceeded(f"at most {max_ids} entity IDs per request")
