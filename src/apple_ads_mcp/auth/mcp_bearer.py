"""Remote MCP authentication — exactly one active mode (PLAN.md §9.2).

bearer (default): static bearer token on /mcp, constant-time comparison.
secret_path: high-entropy path credential /<secret>/mcp; /mcp, prefixes, and
near matches return 404 without revealing whether the service exists; no
bearer header is required or accepted as a credential in this mode.

The decision function is pure so it is testable without ASGI machinery; the
HTTP transport adapts it.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass

from apple_ads_mcp.config import Settings


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    # HTTP status the transport should return when not allowed.
    status: int = 200


def mcp_mount_path(settings: Settings) -> str:
    """Path the MCP endpoint is served under."""
    if settings.mcp_auth_mode == "secret_path" and settings.mcp_path_secret:
        return f"/{settings.mcp_path_secret}/mcp"
    return "/mcp"


def check_request(
    settings: Settings,
    request_path: str,
    authorization_header: str | None,
) -> AuthDecision:
    """Decide whether an HTTP request may reach the MCP endpoint."""
    if settings.mcp_auth_mode == "secret_path":
        if not settings.mcp_path_secret:
            return AuthDecision(False, 404)
        expected = mcp_mount_path(settings)
        # Exact-path credential match; anything else (including /mcp and
        # prefixes/near-misses) is an existence-concealing 404.
        if hmac.compare_digest(request_path.rstrip("/"), expected):
            return AuthDecision(True)
        return AuthDecision(False, 404)

    # bearer mode
    if request_path.rstrip("/") != "/mcp":
        return AuthDecision(False, 404)
    if not settings.mcp_access_token or not authorization_header:
        return AuthDecision(False, 401)
    scheme, _, credential = authorization_header.partition(" ")
    if scheme.lower() == "bearer" and hmac.compare_digest(
        credential.strip(), settings.mcp_access_token
    ):
        return AuthDecision(True)
    return AuthDecision(False, 401)
