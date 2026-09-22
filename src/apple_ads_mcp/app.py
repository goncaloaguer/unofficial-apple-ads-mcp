"""MCP server wiring: FastMCP tools/resources + stdio and HTTP transports.

The MCP SDK is imported only here so every policy/logic module stays
unit-testable without it.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from apple_ads_mcp import reporting
from apple_ads_mcp.auth.mcp_bearer import check_request, mcp_mount_path
from apple_ads_mcp.config import Settings, load_settings
from apple_ads_mcp.context import AppContext
from apple_ads_mcp.tools import reporting_tools, structure

SERVER_NAME = "apple-ads-insights"
INSTRUCTIONS = (
    "Read-only analysis server for one advertiser's Apple Ads (App Store) "
    "account(s). It cannot modify campaigns, budgets, bids or keywords. Start "
    "with list_ad_accounts, then list_campaigns / list_ad_groups / "
    "list_keywords / list_ads for structure and get_report / "
    "get_daily_performance for metrics (levels: campaigns, adgroups, ads, "
    "keywords, searchterms). Apple reports have no conversion or revenue "
    "metrics; installs are the deepest in-platform outcome. Unofficial "
    "community project; not affiliated with Apple Inc."
)


def build_server(ctx: AppContext):
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        host=ctx.settings.host,
        port=ctx.settings.port,
    )

    @mcp.tool()
    async def list_ad_accounts() -> dict:
        """List the ad accounts this deployment is allowed to analyze (currency, timezone, status, roles)."""
        return await structure.list_ad_accounts(ctx)

    @mcp.tool()
    async def list_campaigns(
        account_id: str | None = None,
        status: str | None = None,
        display_status: str | None = None,
        name_contains: str | None = None,
        promoted_object_id: str | None = None,
        verbose: bool = False,
    ) -> dict:
        """List campaigns with daily budget, bid strategy, targeting, and status.

        status: ENABLED | PAUSED (advertiser intent). display_status: RUNNING |
        PAUSED | ON_HOLD | LIMITED | PROCESSING | DELETED (system-computed).
        systemStatusReasons / systemStatusLimitingReasons explain NOT_RUNNING
        and LIMITED states. promoted_object_id is the app's adamId. Output is
        compacted (noise fields dropped, targeting flattened); verbose=true
        returns Apple's raw entities.
        """
        return await structure.list_campaigns(
            ctx, account_id, status, display_status, name_contains, promoted_object_id, verbose
        )

    @mcp.tool()
    async def list_ad_groups(
        account_id: str | None = None,
        campaign_ids: list[str] | None = None,
        status: str | None = None,
        display_status: str | None = None,
        verbose: bool = False,
    ) -> dict:
        """List ad groups with bid strategy, Search Match (automatedKeywordsOptIn), targeting, schedule, status."""
        return await structure.list_ad_groups(ctx, account_id, campaign_ids, status, display_status, verbose)

    @mcp.tool()
    async def list_keywords(
        account_id: str | None = None,
        campaign_ids: list[str] | None = None,
        ad_group_ids: list[str] | None = None,
        status: str | None = None,
        include_negative: bool = False,
    ) -> dict:
        """List keywords (bid, matchType EXACT/BROAD, status); include_negative adds negative keywords."""
        return await structure.list_keywords(
            ctx, account_id, campaign_ids, ad_group_ids, status, include_negative
        )

    @mcp.tool()
    async def list_ads(
        account_id: str | None = None,
        campaign_ids: list[str] | None = None,
        ad_group_ids: list[str] | None = None,
        status: str | None = None,
        include_creatives: bool = True,
        verbose: bool = False,
    ) -> dict:
        """List ads with status/reasons, joined with creative type, destination and eligibility.

        Note: campaigns using the default App Store product page have no ad
        entities at all (verified live); an empty list is normal for them.
        """
        return await structure.list_ads(
            ctx, account_id, campaign_ids, ad_group_ids, status, include_creatives, verbose
        )

    @mcp.tool()
    async def get_report(
        level: str,
        start: str,
        end: str,
        account_id: str | None = None,
        time_zone: str | None = None,
        granularity: str | None = None,
        group_by: list[str] | None = None,
        fields: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        include_grand_total: bool = False,
        include_empty_metrics: bool = False,
    ) -> dict:
        """Flexible App Store performance report (dates YYYY-MM-DD).

        level: campaigns | adgroups | ads | keywords | searchterms.
        time_zone: ORTZ (org time zone, default) | UTC — searchterms require ORTZ.
        granularity: omit for period totals; HOURLY (start within 7 days; not
        ads/searchterms), DAILY (start within 90 days, >1 day), WEEKLY (end
        >=14 days ago), MONTHLY (end >=90 days ago).
        group_by: campaigns/adgroups: deviceClass, ageRange, gender,
        countryCode, adminArea, locality, storefront, countryOrRegion;
        keywords/searchterms: deviceClass, storefront, countryOrRegion; ads:
        storefront, countryOrRegion.
        fields: subset of metrics (see apple-ads://report-fields); omit for all.
        filters: [{field, operator, value}] on campaignId / adGroupId, e.g.
        {"field":"campaignId","operator":"EQUALS","value":123}. keywords and
        searchterms levels REQUIRE a campaignId filter (Apple rule). Rows
        whose searchTermText is null are Apple's low-volume aggregate.
        Metrics: localSpend, impressions, taps, ttr, cpt, cpm, tapInstalls,
        viewInstalls, totalInstalls, new downloads vs redownloads, CPI, install
        rates, pre-orders. No conversion/revenue metrics exist in Apple Ads.
        """
        return await reporting_tools.get_report(
            ctx,
            level=level,
            start=start,
            end=end,
            account_id=account_id,
            time_zone=time_zone,
            granularity=granularity,
            group_by=group_by,
            fields=fields,
            filters=filters,
            include_grand_total=include_grand_total,
            include_empty_metrics=include_empty_metrics,
        )

    @mcp.tool()
    async def get_daily_performance(days: int = 7, account_id: str | None = None) -> dict:
        """Account KPIs by day for the last N full days (default 7): spend, impressions, taps, installs, TTR, CPT, CPI."""
        return await reporting_tools.get_daily_performance(ctx, days, account_id)

    @mcp.resource("apple-ads://report-fields")
    def report_fields() -> str:
        """Report metrics, groupBy dimensions, granularity windows and time-zone rules."""
        return reporting.report_fields_document()

    @mcp.resource("apple-ads://capabilities")
    def capabilities() -> str:
        """What this server can and deliberately cannot do."""
        from apple_ads_mcp.policy.registry import load_registry

        return json.dumps(
            {
                "read_only": True,
                "apple_scope": "searchadsorg",
                "writes": "not implemented, not configurable",
                "enabled_operations": len(load_registry()),
                "scope": "App Store campaigns (Apple Maps read endpoints classified but disabled)",
                "accounts": sorted(ctx.settings.allowed_account_ids),
                "limits": {
                    "tool_calls_per_hour": ctx.settings.max_tool_calls_per_hour,
                    "subrequests_per_call": ctx.settings.max_subrequests_per_call,
                    "max_report_days": ctx.settings.max_report_days,
                    "max_rows": ctx.settings.max_report_rows,
                },
                "disclaimer": "Unofficial community project; not affiliated with, endorsed, or supported by Apple Inc.",
            },
            indent=1,
        )

    return mcp


def build_http_app(settings: Settings, mcp):
    """Streamable-HTTP ASGI app wrapped with the auth policy."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Mount, Route

    inner = mcp.streamable_http_app()

    async def health(request):
        return JSONResponse({"status": "ok"})

    class AuthMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return
            path = scope.get("path", "")
            # /healthz is intercepted by Google Frontend on run.app domains — hence /health.
            if path == "/health":
                response = JSONResponse({"status": "ok"})
                await response(scope, receive, send)
                return
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            decision = check_request(settings, path, headers.get("authorization"))
            if not decision.allowed:
                response = Response(status_code=decision.status)
                await response(scope, receive, send)
                return
            scope = dict(scope)
            scope["path"] = "/mcp"
            await self.app(scope, receive, send)

    app = Starlette(
        routes=[Route("/health", health), Mount("/", app=inner)],
        middleware=[],
        lifespan=lambda a: inner.router.lifespan_context(a),
    )
    return AuthMiddleware(app)


async def startup_role_check(ctx: AppContext) -> list[str]:
    """PLAN.md §2.1: warn (or refuse) when the API user holds write-capable roles."""
    from apple_ads_mcp.policy.accounts import filter_allowed, write_capable_roles
    from apple_ads_mcp.policy.limits import SubrequestBudget

    warnings: list[str] = []
    try:
        payload = await ctx.client.request(
            "GET", "/v1/acls", budget=SubrequestBudget(2), account_id=None
        )
    except Exception as exc:  # network/credential problems surface on first tool call instead
        return [f"startup ACL check skipped: {exc.__class__.__name__}: {exc}"]
    acls = ((payload.get("result") or {}).get("acls")) or []
    seen = set()
    for entry in filter_allowed(ctx.settings, acls):
        account_id = str(entry["adAccount"]["id"])
        seen.add(account_id)
        risky = write_capable_roles(list(entry.get("roles") or []))
        if risky:
            warnings.append(
                f"account {account_id}: API user roles {risky} may permit writes. This server "
                "cannot write, but use the 'API Account Read Only' role for Apple-side least privilege."
            )
    for missing in sorted(ctx.settings.allowed_account_ids - seen):
        warnings.append(f"allowlisted account {missing} is not visible to this API user")
    return warnings


async def run_startup_check(settings: Settings, probe: AppContext | None = None) -> list[str]:
    """Run the role check on a throwaway context and close it.

    The check runs under its own ``asyncio.run`` loop before the server's
    loop exists. Anything created inside it (httpx client, asyncio locks)
    is bound to that loop, so the serving context must be a *fresh*
    ``AppContext`` — reusing the probe context raised "Event loop is closed"
    on the first tool call (live, 2026-09-22).
    """
    probe = probe or AppContext.create(settings)
    try:
        return await startup_role_check(probe)
    finally:
        await probe.client.aclose()


def main() -> None:
    import asyncio

    settings = load_settings()
    for warning in settings.warnings:
        print(f"[config warning] {warning}", file=sys.stderr)

    role_warnings = asyncio.run(run_startup_check(settings))
    for warning in role_warnings:
        print(f"[startup warning] {warning}", file=sys.stderr)
    if settings.require_readonly_role and any("may permit writes" in w for w in role_warnings):
        print("[startup] APPLE_ADS_REQUIRE_READONLY_ROLE=true and a write-capable role was found; refusing to start", file=sys.stderr)
        raise SystemExit(3)

    # Fresh context for the serving loop — never reuse the probe (see run_startup_check).
    ctx = AppContext.create(settings)
    mcp = build_server(ctx)
    if settings.transport == "stdio":
        mcp.run()
        return

    import uvicorn

    app = build_http_app(settings, mcp)
    endpoint = mcp_mount_path(settings)
    print(f"[startup] MCP endpoint active at {endpoint!r} (auth mode: {settings.mcp_auth_mode})", file=sys.stderr)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
