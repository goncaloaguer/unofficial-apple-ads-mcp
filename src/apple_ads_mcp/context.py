"""Shared application context and the per-call guard.

Every tool call passes through guard(): rolling-hour budget, duplicate
suppression, and a fresh per-call subrequest budget.
"""
from __future__ import annotations

from dataclasses import dataclass

from apple_ads_mcp.auth.apple_oauth import TokenManager
from apple_ads_mcp.config import Settings
from apple_ads_mcp.policy.limits import (
    DuplicateSuppressor,
    LimitExceeded,
    RollingWindowLimiter,
    SubrequestBudget,
)
from apple_ads_mcp.apple.client import AppleClient


@dataclass
class AppContext:
    settings: Settings
    client: AppleClient
    limiter: RollingWindowLimiter
    suppressor: DuplicateSuppressor

    @classmethod
    def create(cls, settings: Settings) -> "AppContext":
        tokens = TokenManager(settings)
        return cls(
            settings=settings,
            client=AppleClient(settings, tokens),
            limiter=RollingWindowLimiter(settings.max_tool_calls_per_hour),
            suppressor=DuplicateSuppressor(),
        )

    def guard(self, tool: str, args: dict) -> SubrequestBudget:
        """Admission control for one tool call. Returns the call's budget."""
        key = DuplicateSuppressor.key(tool, args)
        if self.suppressor.is_rapid_duplicate(key):
            raise LimitExceeded(
                f"identical {tool} call received twice in rapid succession; "
                "suppressed as a probable client loop. Re-issue in a moment "
                "if intentional."
            )
        self.limiter.acquire()
        return SubrequestBudget(self.settings.max_subrequests_per_call)
