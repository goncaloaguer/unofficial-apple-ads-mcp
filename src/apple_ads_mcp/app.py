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
from apple_ads_mcp.tools import analysis_tools, insights, reporting_tools, structure

SERVER_NAME = "apple-ads-insights"
INSTRUCTIONS = (
    "Read-only analysis server for one advertiser's Apple Ads (App Store) "
    "account(s). It cannot modify campaigns, budgets, bids or keywords. Start "
    "with list_ad_accounts, then list_campaigns / list_ad_groups / "
    "list_keywords / list_ads for structure and get_report / "
    "get_daily_performance for metrics (levels: campaigns, adgroups, ads, "
    "keywords, searchterms). Analysis tools (compare_periods, rank_performance, "
    "analyze_trends, analyze_pacing, analyze_keywords, analyze_search_terms, "
    "get_account_history) compute deterministically server-side and return "
    "formulas. Insight tools (diagnose_delivery, get_impression_share, "
    "get_search_term_popularity, get_keyword_suggestions, get_recommendations, "
    "get_target_cpa_suggestion, search_apps, get_app_details, search_geo, "
    "check_app_eligibility) expose Apple's own signals read-only. Keyword and "
    "search-term levels need campaign_ids. Apple reports have no conversion "
    "or revenue metrics; installs are the deepest in-platform outcome. "
    "Unofficial community project; not affiliated with Apple Inc."
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
        text_contains: str | None = None,
        limit: int = 300,
    ) -> dict:
        """List keywords (bid, matchType EXACT/BROAD, status) for given campaigns or ad groups.

        campaign_ids or ad_group_ids is required (Apple rejects account-wide
        keyword queries). include_negative adds ad-group negative keywords.
        text_contains narrows by keyword text; limit caps rows (default 300)
        — campaigns often hold hundreds of keywords, so narrow by ad group.
        """
        return await structure.list_keywords(
            ctx, account_id, campaign_ids, ad_group_ids, status, include_negative, text_contains, limit
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

    # ------------------------------------------------------------ Phase 2

    @mcp.tool()
    async def compare_periods(
        period_a_start: str,
        period_a_end: str,
        period_b_start: str,
        period_b_end: str,
        level: str = "account",
        campaign_ids: list[str] | None = None,
        account_id: str | None = None,
    ) -> dict:
        """Compare two date ranges: absolute and % deltas (period_b relative to period_a).

        level: account | campaigns | adgroups | keywords (keywords needs campaign_ids).
        Metrics: spend, impressions, taps, installs (tap/view/total), new vs
        redownloads, plus derived ttr, cpt, cpm, cpi, install rates.
        """
        return await analysis_tools.compare_periods(ctx, period_a_start, period_a_end, period_b_start, period_b_end, level, campaign_ids, account_id)

    @mcp.tool()
    async def rank_performance(
        start: str,
        end: str,
        level: str = "campaigns",
        metric: str = "cpi",
        top_n: int = 10,
        min_spend: float = 0.0,
        group_by: str | None = None,
        campaign_ids: list[str] | None = None,
        account_id: str | None = None,
    ) -> dict:
        """Rank campaigns/adgroups/ads/keywords/searchterms (or a group_by dimension
        such as storefront, countryOrRegion, deviceClass) by a metric.

        Cost metrics (cpi, cpt, cpm, localSpend) rank ascending; others
        descending. min_spend filters noise. keywords/searchterms need campaign_ids.
        """
        return await analysis_tools.rank_performance(ctx, start, end, level, metric, top_n, min_spend, group_by, campaign_ids, account_id)

    @mcp.tool()
    async def analyze_trends(
        start: str,
        end: str,
        metric: str = "localSpend",
        grain: str = "day",
        campaign_ids: list[str] | None = None,
        per_campaign: bool = False,
        account_id: str | None = None,
    ) -> dict:
        """Time series of a metric (day | week | hour) with moving average and anomaly flags.

        Account-level by default; per_campaign=true returns one series per campaign.
        Apple windows apply: hour needs start within 7 days, day within 90, week ends >=14 days ago.
        """
        return await analysis_tools.analyze_trends(ctx, start, end, metric, grain, campaign_ids, per_campaign, account_id)

    @mcp.tool()
    async def analyze_pacing(
        start: str,
        end: str,
        campaign_ids: list[str] | None = None,
        account_id: str | None = None,
    ) -> dict:
        """Spend vs dailyBudget x days per campaign: utilization and flags (budget_capped, under_delivering, on_pace, not_enabled)."""
        return await analysis_tools.analyze_pacing(ctx, start, end, campaign_ids, account_id)

    @mcp.tool()
    async def analyze_keywords(
        campaign_ids: list[str],
        start: str,
        end: str,
        ad_group_ids: list[str] | None = None,
        min_impressions: int = 10,
        zero_install_spend: float = 5.0,
        top_n: int = 25,
        account_id: str | None = None,
    ) -> dict:
        """Keyword efficiency for given campaigns: spend, taps, installs, CPI, TTR, cpt_vs_bid and flags
        (impression_starved, spend_no_installs, low_install_rate, paying_near_bid). Summary lists best-CPI keywords and zero-install spenders."""
        return await analysis_tools.analyze_keywords(ctx, campaign_ids, start, end, ad_group_ids, min_impressions, zero_install_spend, top_n, account_id)

    @mcp.tool()
    async def analyze_search_terms(
        campaign_ids: list[str],
        start: str,
        end: str,
        min_taps: int = 3,
        top_n: int = 25,
        account_id: str | None = None,
    ) -> dict:
        """Search-term mining for given campaigns: expansion_candidates (terms with installs that
        are not exact keywords yet), negative_candidates (taps but no installs), top terms, and
        Apple's low-volume aggregate bucket kept separate."""
        return await analysis_tools.analyze_search_terms(ctx, campaign_ids, start, end, min_taps, top_n, account_id)

    @mcp.tool()
    async def get_account_history(
        start: str,
        end: str,
        entity_types: list[str] | None = None,
        event_types: list[str] | None = None,
        campaign_ids: list[str] | None = None,
        include_details: bool = False,
        max_details: int = 10,
        account_id: str | None = None,
    ) -> dict:
        """Change history (who changed what, when) for a range up to 6 months.

        entity_types: Campaign, AdGroup, Keyword, NegativeKeyword, Ad, Creative, AdAccount.
        event_types: CREATE, UPDATE, DELETE (filtered locally). include_details fetches
        field-level before/after for up to max_details changes. Use to correlate
        config changes with performance shifts (correlation, not causation).
        """
        return await analysis_tools.get_account_history(ctx, start, end, entity_types, event_types, campaign_ids, include_details, max_details, account_id)

    # ------------------------------------------------------------ Phase 3

    @mcp.tool()
    async def diagnose_delivery(
        campaign_ids: list[str] | None = None,
        lookback_days: int = 3,
        account_id: str | None = None,
    ) -> dict:
        """Why isn't it serving? Account/campaign/ad-group system statuses with reasons, plus
        RUNNING campaigns with no impressions in the last N days."""
        return await insights.diagnose_delivery(ctx, campaign_ids, lookback_days, account_id)

    @mcp.tool()
    async def check_app_eligibility(adam_id: str, countries: list[str] | None = None, account_id: str | None = None) -> dict:
        """Whether an app (adamId) can run App Store ads, per country/placement/device."""
        return await insights.check_app_eligibility(ctx, adam_id, countries, account_id)

    @mcp.tool()
    async def get_impression_share(
        adam_id: str,
        start: str,
        end: str,
        granularity: str = "DAILY",
        report_type: str = "ALL_SLOTS",
        countries: list[str] | None = None,
        search_term_contains: str | None = None,
        limit: int = 200,
        account_id: str | None = None,
    ) -> dict:
        """Apple impression share by search term for your app (adamId): share bracket, rank, popularity.

        DAILY up to 30 days, or WEEKLY_SUN_SAT up to 4 weeks starting on a Sunday. UTC.
        report_type FIRST_SLOT (top position) or ALL_SLOTS.
        """
        return await insights.get_impression_share(ctx, adam_id, start, end, granularity, report_type, countries, search_term_contains, limit, account_id)

    @mcp.tool()
    async def get_search_term_popularity(
        countries: list[str],
        start: str,
        end: str,
        granularity: str = "WEEKLY_SUN_SAT",
        genre: str | None = None,
        search_term_contains: str | None = None,
        limit: int = 100,
        account_id: str | None = None,
    ) -> dict:
        """Apple's most-searched terms by storefront and genre (WEEKLY_SUN_SAT or MONTHLY), with rank and 1-100 popularity.

        genre: Apple genre token, e.g. HEALTH_FITNESS, PRODUCTIVITY, SOCIAL_NETWORKING, ENTERTAINMENT ('Health &
        Fitness' is normalized; the conjunction is dropped). Omit for all genres. search_term_contains is matched
        locally over up to 500 terms per genre (pass a genre to keep the scan small)."""
        return await insights.get_search_term_popularity(ctx, countries, start, end, granularity, genre, search_term_contains, limit, account_id)

    @mcp.tool()
    async def get_keyword_suggestions(
        adam_id: str,
        terms: list[str] | None = None,
        countries: list[str] | None = None,
        limit: int = 100,
        account_id: str | None = None,
    ) -> dict:
        """Apple's keyword and phrase suggestions for an app (adamId), optionally seeded with terms and storefronts."""
        return await insights.get_keyword_suggestions(ctx, adam_id, terms, countries, limit, account_id)

    @mcp.tool()
    async def get_recommendations(campaign_ids: list[str], kind: str = "both", account_id: str | None = None) -> dict:
        """Apple's daily-budget and Target CPA recommendations for campaigns (read-only; cannot apply/dismiss). kind: both | daily_budget | target_cpa."""
        return await insights.get_recommendations(ctx, campaign_ids, kind, account_id)

    @mcp.tool()
    async def get_target_cpa_suggestion(adam_id: str, countries: list[str] | None = None, account_id: str | None = None) -> dict:
        """Apple's suggested Target CPA for a new Max Conversions campaign for an app, per country."""
        return await insights.get_target_cpa_suggestion(ctx, adam_id, countries, account_id)

    @mcp.tool()
    async def search_apps(
        query: str | None = None,
        return_owned_apps: bool = False,
        storefronts: list[str] | None = None,
        limit: int = 20,
        account_id: str | None = None,
    ) -> dict:
        """Search the App Store by app/developer name (>=3 chars), or list apps this org owns (return_owned_apps=true)."""
        return await insights.search_apps(ctx, query, return_owned_apps, storefronts, limit, account_id)

    @mcp.tool()
    async def get_app_details(adam_id: str, account_id: str | None = None) -> dict:
        """App metadata (name, developer, genres, storefronts) and its custom product pages."""
        return await insights.get_app_details(ctx, adam_id, account_id)

    @mcp.tool()
    async def search_geo(
        query: str | None = None,
        entity: str | None = None,
        country_code: str | None = None,
        ids: list[str] | None = None,
        limit: int = 50,
        account_id: str | None = None,
    ) -> dict:
        """Geo targeting lookup: search by name (entity: COUNTRY | ADMIN_AREA | LOCALITY | POSTAL_CODE, optional
        country_code; localities exist only for some storefronts) or resolve numeric geo IDs seen in ad-group
        targeting (ids=[...] plus the matching entity, e.g. adminArea ids -> ADMIN_AREA)."""
        return await insights.search_geo(ctx, query, entity, country_code, ids, limit, account_id)

    @mcp.tool()
    async def get_supported_languages(country_code: str | None = None, account_id: str | None = None) -> dict:
        """Supported app languages/locales, optionally for one country."""
        return await insights.get_supported_languages(ctx, country_code, account_id)

    @mcp.resource("apple-ads://report-fields")
    def report_fields() -> str:
        """Report metrics, groupBy dimensions, granularity windows and time-zone rules."""
        return reporting.report_fields_document()

    @mcp.resource("apple-ads://api-notes")
    def api_notes() -> str:
        """Live-verified Apple API behaviours that differ from the docs (docs/API_NOTES.md)."""
        from importlib import resources as ilres

        try:
            return ilres.files("apple_ads_mcp").joinpath("API_NOTES.md").read_text()
        except (FileNotFoundError, OSError):
            return "See docs/API_NOTES.md in the repository."

    @mcp.prompt()
    def weekly_performance_review(days: int = 7) -> str:
        """Weekly Apple Ads review: this week vs last, movers, pacing, keyword and search-term actions."""
        return (
            f"Review Apple Ads for the last {days} full days. 1) list_ad_accounts, then get_daily_performance(days={days}). "
            f"2) compare_periods for the last {days} days vs the {days} before at level=campaigns. "
            "3) rank_performance by cpi and by totalInstalls for the period with min_spend > 0. "
            "4) analyze_pacing for the period. 5) For the top-spend campaigns, analyze_search_terms and analyze_keywords. "
            "Report observations first, then recommendations, clearly separated; state which numbers are Apple-reported and which are derived; "
            "note that installs, not trials or revenue, are the deepest metric available here."
        )

    @mcp.prompt()
    def diagnose_performance_drop(metric: str = "totalInstalls") -> str:
        """Investigate a drop in a metric: trend, changes, delivery, keywords."""
        return (
            f"Investigate a drop in {metric}. 1) analyze_trends for the last 30 days by day to locate when it started. "
            "2) get_account_history for the window around the change (entity_types Campaign, AdGroup, Keyword). "
            "3) diagnose_delivery. 4) compare_periods before/after at level=campaigns, then keywords for affected campaigns. "
            "Present evidence before interpretation; correlation with account changes is not proof of cause."
        )

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
