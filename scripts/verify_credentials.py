#!/usr/bin/env python3
"""Verify Apple Ads API credentials without starting the MCP server.

Reads the same environment variables as the server (APPLE_ADS_CLIENT_ID,
APPLE_ADS_TEAM_ID, APPLE_ADS_KEY_ID, APPLE_ADS_PRIVATE_KEY or
APPLE_ADS_PRIVATE_KEY_PATH), requests an access token, and calls
``GET /v1/me`` and ``GET /v1/acls``. Prints your org ID and the ad accounts
this API user can see with their roles — nothing else. Use the printed
account IDs for ALLOWED_ACCOUNT_IDS.

Usage:
    set -a; source .env; set +a      # or export the variables another way
    python3 scripts/verify_credentials.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apple_ads_mcp.config import ConfigError, load_settings  # noqa: E402


async def main() -> int:
    import httpx

    from apple_ads_mcp.auth.apple_oauth import OAuthError, TokenManager
    from apple_ads_mcp.policy.accounts import write_capable_roles

    os.environ.setdefault("ALLOWED_ACCOUNT_IDS", "0")  # not needed for discovery
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    tokens = TokenManager(settings)
    try:
        token = await tokens.get_token()
    except OAuthError as exc:
        print(f"token request failed: {exc}", file=sys.stderr)
        return 1
    print("access token: OK (scope searchadsorg, 1h TTL)")
    headers = {"Authorization": f"Bearer {token}", "User-Agent": settings.user_agent}
    async with httpx.AsyncClient(base_url=settings.api_base_url, timeout=30, trust_env=False) as client:
        me = await client.get("/v1/me", headers=headers)
        if me.status_code != 200:
            print(f"GET /v1/me failed: HTTP {me.status_code}", file=sys.stderr)
            return 1
        result = me.json().get("result") or {}
        print(f"user id: {result.get('userId')}  org id: {result.get('orgId')}")
        acls = await client.get("/v1/acls", headers=headers)
        if acls.status_code != 200:
            print(f"GET /v1/acls failed: HTTP {acls.status_code}", file=sys.stderr)
            return 1
        entries = ((acls.json().get("result") or {}).get("acls")) or []
        if not entries:
            print("no ad accounts visible to this API user")
            return 0
        print("ad accounts visible to this API user:")
        for entry in entries:
            account = entry.get("adAccount") or {}
            roles = list(entry.get("roles") or [])
            flag = "" if not write_capable_roles(roles) else "   <- write-capable role; prefer 'API Account Read Only'"
            print(f"  id={account.get('id')}  name={account.get('name')!r}  roles={roles}{flag}")
        print("\nUse the id value(s) above for ALLOWED_ACCOUNT_IDS.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
