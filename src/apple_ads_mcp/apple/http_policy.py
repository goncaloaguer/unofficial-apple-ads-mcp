"""Pure HTTP-policy helpers: rate-limit parsing, backoff, error summarization.
Stdlib-only so they are fully unit-testable without network deps.

Apple returns IETF-style ``RateLimit-Limit`` / ``RateLimit-Remaining`` /
``RateLimit-Reset`` (delta seconds) on every response and ``Retry-After`` on
429 (PLAN.md §3.2). Apple's own guidance: throttle proactively when Remaining
is low, prefer Retry-After on 429, double the wait per consecutive 429 up to
~16 s, reset after a success.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass

ALLOWED_HOSTS = frozenset({"api.ads.apple.com", "appleid.apple.com"})
MAX_BACKOFF_SECONDS = 16.0
PROACTIVE_THROTTLE_REMAINING = 3


@dataclass(frozen=True)
class RateLimitState:
    limit: int | None
    remaining: int | None
    reset_seconds: int | None
    retry_after: int | None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    return int(value) if value.isdigit() else None


def parse_rate_limit(headers: dict[str, str]) -> RateLimitState:
    lowered = {k.lower(): v for k, v in headers.items()}
    return RateLimitState(
        limit=_int(lowered.get("ratelimit-limit")),
        remaining=_int(lowered.get("ratelimit-remaining")),
        reset_seconds=_int(lowered.get("ratelimit-reset")),
        retry_after=_int(lowered.get("retry-after")),
    )


def proactive_delay(state: RateLimitState) -> float:
    """Seconds to wait *before* the next request when quota is nearly spent."""
    if state.remaining is not None and state.remaining < PROACTIVE_THROTTLE_REMAINING:
        return float(min(state.reset_seconds or 1, MAX_BACKOFF_SECONDS))
    return 0.0


def retry_delay(state: RateLimitState, consecutive_429: int) -> float:
    """Wait for a 429: Retry-After, else RateLimit-Reset, else doubling backoff."""
    if state.retry_after is not None:
        return float(min(state.retry_after, 30))
    if state.reset_seconds is not None:
        return float(min(state.reset_seconds, 30))
    return min(MAX_BACKOFF_SECONDS, 2.0 * (2 ** max(0, consecutive_429 - 1)))


def backoff_delay(attempt: int, base: float = 0.5, cap: float = MAX_BACKOFF_SECONDS) -> float:
    """Capped exponential backoff with full jitter for transient 5xx. attempt starts at 1."""
    return random.uniform(0, min(cap, base * (2 ** (attempt - 1))))


def summarize_error_body(body_text: str, max_len: int = 400) -> str | None:
    """Extract Apple's structured error code/message/details from a body.

    Returns only ``error.code``, ``error.message`` and ``details[].code/message``
    — API metadata, never row data, tokens, or free-form payloads.
    """
    try:
        payload = json.loads(body_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    parts: list[str] = []
    code = error.get("code")
    message = error.get("message")
    if isinstance(code, str) and code:
        parts.append(code)
    if isinstance(message, str) and message:
        parts.append(message)
    for detail in error.get("details") or []:
        if isinstance(detail, dict):
            d_code = detail.get("code")
            d_msg = detail.get("message")
            bits = [b for b in (d_code, d_msg) if isinstance(b, str) and b]
            if bits:
                parts.append(": ".join(bits))
    if not parts:
        return None
    return "; ".join(parts)[:max_len]


RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
NO_RETRY_STATUS = frozenset({400, 401, 403, 404})


def should_retry(status: int, attempt: int, max_attempts: int = 4) -> bool:
    return status in RETRYABLE_STATUS and attempt < max_attempts
