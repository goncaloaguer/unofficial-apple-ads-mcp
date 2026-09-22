"""Startup configuration. Fails fast on invalid or unsafe settings.

All secrets arrive via environment variables (locally from the shell; on
Cloud Run from Secret Manager references). Nothing here can enable write
access to Apple Ads.

Apple authentication (PLAN.md §9.1): an ES256 private key plus the
clientId/teamId/keyId shown in the Ads UI after uploading the public key.
The key arrives either inline (APPLE_ADS_PRIVATE_KEY, PEM text) or as a path
(APPLE_ADS_PRIVATE_KEY_PATH) — exactly one.

Remote MCP authentication uses exactly one mode (PLAN.md §9.2):
- MCP_AUTH_MODE=bearer (default): static bearer token on /mcp.
- MCP_AUTH_MODE=secret_path: high-entropy path credential /<secret>/mcp for
  MCP clients that cannot attach headers. /mcp returns 404 in this mode.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_ACCOUNT_ID_RE = re.compile(r"^[0-9]{1,20}$")
_SEARCHADS_ID_RE = re.compile(r"^SEARCHADS\.[A-Za-z0-9\-]{8,}$")
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9\-]{8,}$")
_PATH_SECRET_RE = re.compile(r"^[A-Za-z0-9_\-]{43,}$")  # >=32 random bytes, urlsafe b64
_PEM_RE = re.compile(r"-----BEGIN (EC |)PRIVATE KEY-----")

REQUIRED_APPLE_SCOPE = "searchadsorg"


class ConfigError(ValueError):
    """Raised when configuration is missing or unsafe."""


@dataclass(frozen=True)
class Settings:
    # Apple OAuth (secrets)
    apple_client_id: str
    apple_team_id: str
    apple_key_id: str
    apple_private_key_pem: str
    # Account isolation
    allowed_account_ids: frozenset[str]
    default_account_id: str | None
    require_readonly_role: bool
    # MCP remote auth — exactly one active mode
    mcp_auth_mode: str  # "bearer" | "secret_path" (http transport only)
    mcp_access_token: str | None
    mcp_path_secret: str | None
    transport: str  # "stdio" | "http"
    host: str = "0.0.0.0"
    port: int = 8080
    # Safety ceilings (PLAN.md §7) — operator may lower, not raise via MCP
    max_tool_calls_per_hour: int = 60
    max_subrequests_per_call: int = 20
    max_concurrent_subrequests: int = 4
    tool_deadline_seconds: int = 90
    max_report_rows: int = 1000
    max_pages: int = 10
    max_page_size: int = 1000
    max_report_days: int = 90
    max_entity_ids: int = 100
    # Chat clients stop rendering tool results well below Reddit's 2 MiB
    # ceiling (a 57 KB campaign list was refused live, 2026-09-22); the
    # envelope halves `data` with a warning past this size. MAX_RESPONSE_BYTES
    # overrides it for header-capable clients that handle large payloads.
    max_response_bytes: int = 40_000
    api_base_url: str = "https://api.ads.apple.com"  # registry paths carry the /v1 prefix
    token_url: str = "https://appleid.apple.com/auth/oauth2/token"
    user_agent: str = "apple-ads-insights-mcp/0.2.5 (+https://github.com/goncaloaguer/unofficial-apple-ads-mcp)"
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


def _read_private_key(problems: list[str]) -> str:
    inline = os.environ.get("APPLE_ADS_PRIVATE_KEY")
    path = _get("APPLE_ADS_PRIVATE_KEY_PATH")
    if inline and path:
        problems.append("set only one of APPLE_ADS_PRIVATE_KEY and APPLE_ADS_PRIVATE_KEY_PATH")
        return ""
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                inline = fh.read()
        except OSError as exc:
            problems.append(f"APPLE_ADS_PRIVATE_KEY_PATH unreadable: {exc.__class__.__name__}")
            return ""
    if not inline:
        problems.append("APPLE_ADS_PRIVATE_KEY (PEM text) or APPLE_ADS_PRIVATE_KEY_PATH is required")
        return ""
    # Secret Manager / shells sometimes deliver literal "\n"; normalize.
    pem = inline.replace("\\n", "\n").strip() + "\n"
    if not _PEM_RE.search(pem):
        problems.append(
            "APPLE_ADS_PRIVATE_KEY must be a PEM-encoded EC private key "
            "(-----BEGIN EC PRIVATE KEY----- or -----BEGIN PRIVATE KEY-----)"
        )
    return pem


def load_settings(env: dict[str, str] | None = None) -> Settings:
    if env is not None:
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            return load_settings(None)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    problems: list[str] = []
    warnings: list[str] = []

    client_id = _get("APPLE_ADS_CLIENT_ID")
    team_id = _get("APPLE_ADS_TEAM_ID")
    key_id = _get("APPLE_ADS_KEY_ID")
    for name, value, pattern in (
        ("APPLE_ADS_CLIENT_ID", client_id, _SEARCHADS_ID_RE),
        ("APPLE_ADS_TEAM_ID", team_id, _SEARCHADS_ID_RE),
        ("APPLE_ADS_KEY_ID", key_id, _KEY_ID_RE),
    ):
        if not value:
            problems.append(f"{name} is required")
        elif not pattern.match(value):
            problems.append(f"{name} does not look like the value shown in Apple Ads → Account Settings → API")
    pem = _read_private_key(problems)

    raw_allowed = _get("ALLOWED_ACCOUNT_IDS") or ""
    allowed = frozenset(a.strip() for a in raw_allowed.split(",") if a.strip())
    if not allowed:
        problems.append(
            "ALLOWED_ACCOUNT_IDS is required (comma-separated numeric ad account "
            "IDs from GET /v1/acls); account discovery never grants access implicitly"
        )
    else:
        bad = [a for a in sorted(allowed) if not _ACCOUNT_ID_RE.match(a)]
        if bad:
            problems.append(f"ALLOWED_ACCOUNT_IDS entries must be numeric ad account IDs: {bad}")

    default_account = _get("DEFAULT_ACCOUNT_ID")
    if default_account and default_account not in allowed:
        problems.append("DEFAULT_ACCOUNT_ID must be present in ALLOWED_ACCOUNT_IDS")
    if not default_account and len(allowed) == 1:
        default_account = next(iter(allowed))

    require_ro = (_get("APPLE_ADS_REQUIRE_READONLY_ROLE") or "false").lower() in ("1", "true", "yes")

    transport = (_get("MCP_TRANSPORT") or "stdio").lower()
    if transport not in ("stdio", "http"):
        problems.append("MCP_TRANSPORT must be 'stdio' or 'http'")

    auth_mode = (_get("MCP_AUTH_MODE") or "bearer").lower()
    access_token = _get("MCP_ACCESS_TOKEN")
    path_secret = _get("MCP_PATH_SECRET")

    if transport == "http":
        if auth_mode not in ("bearer", "secret_path"):
            problems.append("MCP_AUTH_MODE must be 'bearer' or 'secret_path'")
        elif access_token and path_secret:
            problems.append(
                "MCP_ACCESS_TOKEN and MCP_PATH_SECRET are both set; the auth "
                "modes are mutually exclusive — configure exactly one credential"
            )
        elif auth_mode == "bearer":
            if not access_token:
                problems.append("bearer mode requires MCP_ACCESS_TOKEN")
            elif len(access_token) < 32:
                problems.append("MCP_ACCESS_TOKEN must be at least 32 characters")
            path_secret = None
        elif auth_mode == "secret_path":
            if not path_secret:
                problems.append("secret_path mode requires MCP_PATH_SECRET")
            elif not _PATH_SECRET_RE.match(path_secret):
                problems.append(
                    "MCP_PATH_SECRET must be a URL-safe value of at least 32 "
                    "random bytes (e.g. `python3 -c \"import secrets; "
                    "print(secrets.token_urlsafe(32))\"`); do not choose a "
                    "memorable path"
                )
            access_token = None
            warnings.append(
                "secret_path compatibility mode active: the URL itself is the "
                "credential. It can appear in MCP client settings, browser "
                "history, and infrastructure request logs. Rotate it "
                "periodically and prefer bearer mode where your client "
                "supports headers."
            )
    else:
        access_token = None
        path_secret = None

    if problems:
        raise ConfigError("; ".join(problems))

    return Settings(
        apple_client_id=client_id or "",
        apple_team_id=team_id or "",
        apple_key_id=key_id or "",
        apple_private_key_pem=pem,
        allowed_account_ids=allowed,
        default_account_id=default_account,
        require_readonly_role=require_ro,
        mcp_auth_mode=auth_mode,
        mcp_access_token=access_token,
        mcp_path_secret=path_secret,
        transport=transport,
        port=int(_get("PORT") or "8080"),
        max_response_bytes=int(_get("MAX_RESPONSE_BYTES") or 40_000),
        warnings=tuple(warnings),
    )
