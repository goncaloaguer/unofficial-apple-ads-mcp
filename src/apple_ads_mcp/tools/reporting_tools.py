"""Reporting tools: get_report and get_daily_performance."""
from __future__ import annotations

from typing import Any

from apple_ads_mcp import reporting
from apple_ads_mcp.apple.normalize import normalize_metrics, safe_div
from apple_ads_mcp.context import AppContext
from apple_ads_mcp.envelope import build_envelope
from apple_ads_mcp.policy.accounts import resolve_account

# Metadata keys kept per level (names per the SDK's *Reporting* models); the
# groupBy dimension keys are always kept when present.
_GROUP_DIMS = ("countryOrRegion", "deviceClass", "gender", "ageRange", "locality", "countryCode", "adminArea", "storefront")
_METADATA_KEEP = {
    "campaigns": ("id", "name", "status", "displayStatus", "systemStatus", "promotedObjectId", "dailyBudget", "bidStrategy", "startTime", "endTime"),
    "adgroups": ("id", "name", "campaignId", "status", "displayStatus", "systemStatus", "automatedKeywordsOptIn", "bidStrategy", "pricingModel"),
    "ads": ("id", "name", "campaignId", "adGroupId", "status", "displayStatus", "systemStatus", "creative"),
    "keywords": ("id", "text", "campaignId", "adGroupId", "status", "displayStatus", "matchType", "bid"),
    "searchterms": ("searchTermText", "searchTermSource", "campaignId", "adGroupId", "keyword"),
}


def flatten_rows(level: str, raw_rows: list[dict], granular: bool) -> list[dict[str, Any]]:
    """Flatten Apple's {totalMetrics, granularMetrics, metadata} rows.

    Without granularity: one row per entity (totals + metadata). With
    granularity: one row per entity per period (each carries ``date``),
    metadata repeated. Unknown metadata keys are kept under ``metadata_extra``
    only when nothing matched the curated keep-list (defensive against shape
    drift), never dropped silently.
    """
    keep = _METADATA_KEEP.get(level, ()) + _GROUP_DIMS
    out: list[dict[str, Any]] = []
    for raw in raw_rows:
        meta_raw = raw.get("metadata") or {}
        meta = {k: meta_raw.get(k) for k in keep if k in meta_raw}
        if not meta and meta_raw:
            meta = {"metadata_extra": meta_raw}
        for money_key in ("bid", "dailyBudget"):
            if isinstance(meta.get(money_key), dict):
                meta[money_key] = normalize_metrics({money_key: meta[money_key]}).get(money_key)
        kw = meta.get("keyword")
        if isinstance(kw, dict):  # search-term rows: compact the matched keyword
            meta["keyword"] = {
                "id": kw.get("id"), "text": kw.get("text"), "matchType": kw.get("matchType"),
                "bid": normalize_metrics({"bid": kw.get("bid")}).get("bid") if isinstance(kw.get("bid"), dict) else kw.get("bid"),
            }
        if raw.get("insights"):
            meta["insights"] = raw["insights"]
        periods = raw.get("granularMetrics") if granular else None
        if periods:
            for p in periods:
                out.append({**meta, **normalize_metrics(p)})
        else:
            out.append({**meta, **normalize_metrics(raw.get("totalMetrics"))})
    return out


async def get_report(
    ctx: AppContext,
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
) -> dict[str, Any]:
    account = resolve_account(ctx.settings, account_id)
    path, body = reporting.build_report_request(
        level=level,
        start=start,
        end=end,
        time_zone=time_zone,
        granularity=granularity,
        group_by=group_by,
        fields=fields,
        filters=filters,
        include_grand_total=include_grand_total,
        include_empty_metrics=include_empty_metrics,
        page_size=min(500, ctx.settings.max_page_size),
        max_report_days=ctx.settings.max_report_days,
    )
    budget = ctx.guard("get_report", {"account_id": account, "path": path, **body})
    raw_rows, meta = await ctx.client.paginate(
        "POST", path, budget=budget, account_id=account, json_body=body, result_key="rows"
    )
    rows = flatten_rows(level.lower(), raw_rows, granular=bool(granularity))
    meta["rows_returned"] = len(rows)
    meta["entities"] = len(raw_rows)
    summary: dict[str, Any] = {
        "level": level.lower(),
        "time_range": body["timeRange"],
        "group_by": body.get("groupBy", []),
    }
    grand = (meta.pop("summary", None) or {}).get("grandTotal") if include_grand_total else None
    if grand:
        summary["grand_total"] = normalize_metrics(grand.get("totalMetrics") or grand)
    warnings: list[str] = []
    if not rows:
        warnings.append(
            "No rows returned: no delivery in this range (check displayStatus with "
            "list_campaigns), or the range/granularity combination yields no periods."
        )
    if fw := reporting.freshness_warning(end):
        warnings.append(fw)
    return build_envelope(
        data=rows,
        meta=meta,
        account_id=account,
        summary=summary,
        warnings=warnings,
        derived_metrics=[{"money": "Money.amount strings converted to floats in the account currency"}],
        max_response_bytes=ctx.settings.max_response_bytes,
    )


_KPI_SUM = ("localSpend", "impressions", "taps", "tapInstalls", "viewInstalls", "totalInstalls",
            "totalNewDownloads", "totalRedownloads")


async def get_daily_performance(
    ctx: AppContext,
    days: int = 7,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Account KPIs by day for the last N full days (default 7), summed across campaigns."""
    days = max(2, min(days, ctx.settings.max_report_days))  # DAILY needs a range > 1 day
    start, end = reporting.default_date_range(days)
    result = await get_report(
        ctx, level="campaigns", start=start, end=end, account_id=account_id, granularity="DAILY",
    )
    by_day: dict[str, dict[str, float]] = {}
    for row in result["data"]:
        day = str(row.get("date") or "")
        bucket = by_day.setdefault(day, {})
        for key in _KPI_SUM:
            value = row.get(key)
            if isinstance(value, (int, float)):
                bucket[key] = round(bucket.get(key, 0) + value, 2)
    daily = []
    for day in sorted(by_day):
        b = by_day[day]
        b["ttr_derived"] = safe_div(b.get("taps"), b.get("impressions"))
        b["cpt_derived"] = safe_div(b.get("localSpend"), b.get("taps"))
        b["cpi_derived"] = safe_div(b.get("localSpend"), b.get("totalInstalls"))
        daily.append({"date": day, **b})
    totals: dict[str, float | None] = {}
    for key in _KPI_SUM:
        totals[key] = round(sum(d.get(key, 0) for d in daily), 2)
    totals["ttr_derived"] = safe_div(totals.get("taps"), totals.get("impressions"))
    totals["cpt_derived"] = safe_div(totals.get("localSpend"), totals.get("taps"))
    totals["cpi_derived"] = safe_div(totals.get("localSpend"), totals.get("totalInstalls"))
    result["data"] = daily
    result["summary"] = {"days": days, "start": start, "end": end, "totals": totals}
    result["meta"]["rows_returned"] = len(daily)
    result["derived_metrics"] = [
        {"ttr_derived": "sum(taps)/sum(impressions) per day / period"},
        {"cpt_derived": "sum(localSpend)/sum(taps), account currency"},
        {"cpi_derived": "sum(localSpend)/sum(totalInstalls), account currency"},
    ]
    return result
