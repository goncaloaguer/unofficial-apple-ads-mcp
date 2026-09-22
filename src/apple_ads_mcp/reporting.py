"""Report request construction and validation for the five App Store levels.

Encodes Apple's documented constraints (PLAN.md §3.2) so invalid requests fail
locally with an actionable message instead of a 400 upstream:

- granularity windows (HOURLY: start within 7 days, not for ads/searchterms;
  DAILY: start within 90 days and range > 1 day; WEEKLY: start within 365
  days, end >= 14 days ago; MONTHLY: end >= 90 days ago; single day: none)
- groupBy dimensions per level; HOURLY excludes demographic/geo groupBys
- search terms: ORTZ only; options.includeRows unsupported
- EMPTY_METRICS cannot be combined with groupBy

Stdlib-only.
"""
from __future__ import annotations

import datetime as dt
from importlib import resources
from typing import Any

from apple_ads_mcp.apple.query import report_query
from apple_ads_mcp.policy.limits import LimitExceeded, check_date_range

LEVELS = {
    "campaigns": "/v1/reports/apps/campaigns/query",
    "adgroups": "/v1/reports/apps/adgroups/query",
    "ads": "/v1/reports/apps/ads/query",
    "keywords": "/v1/reports/apps/keywords/query",
    "searchterms": "/v1/reports/apps/searchterms/query",
}

GROUP_BY = {
    "campaigns": ("deviceClass", "ageRange", "gender", "countryCode", "adminArea", "locality", "storefront", "countryOrRegion"),
    "adgroups": ("deviceClass", "ageRange", "gender", "countryCode", "adminArea", "locality", "storefront", "countryOrRegion"),
    "keywords": ("deviceClass", "storefront", "countryOrRegion"),
    "searchterms": ("deviceClass", "storefront", "countryOrRegion"),
    "ads": ("storefront", "countryOrRegion"),
}
HOURLY_EXCLUDED_GROUP_BY = frozenset({"ageRange", "gender", "countryCode", "adminArea", "locality"})
GRANULARITIES = ("HOURLY", "DAILY", "WEEKLY", "MONTHLY")
TIME_ZONES = ("UTC", "ORTZ")
INCLUDE_ROWS = ("GRAND_TOTAL", "EMPTY_METRICS")

METRICS = (
    "localSpend", "impressions", "taps", "ttr", "cpt", "cpm",
    "tapInstalls", "tapInstallCPI", "viewInstalls", "totalInstalls",
    "totalNewDownloads", "totalRedownloads", "tapNewDownloads", "tapRedownloads",
    "viewNewDownloads", "viewRedownloads", "totalAvgCPI", "totalInstallRate",
    "tapInstallRate", "tapPreOrdersPlaced", "viewPreOrdersPlaced", "totalPreOrdersPlaced",
)
COST_METRICS = frozenset({"localSpend", "cpt", "cpm", "tapInstallCPI", "totalAvgCPI"})

# Curated filter fields per level. Verified live (2026-09-22): `campaignId`
# and `adGroupId` are accepted; `keywordId` is rejected
# ("Filters contain unsupported fields"); keyword and search-term reports
# REQUIRE a `campaignId` filter ("campaignId filter is required for KEYWORD
# reports when promotedObjectType is APPS"). Other names are unverified and
# will surface Apple's validation message if wrong.
FILTER_FIELDS = {
    "campaigns": ("campaignId", "campaignStatus", "promotedObjectId", "countryOrRegion"),
    "adgroups": ("campaignId", "adGroupId", "adGroupStatus", "countryOrRegion"),
    "ads": ("campaignId", "adGroupId", "adId", "countryOrRegion"),
    "keywords": ("campaignId", "adGroupId", "keywordStatus", "matchType", "countryOrRegion"),
    "searchterms": ("campaignId", "adGroupId", "countryOrRegion"),
}
REQUIRES_CAMPAIGN_FILTER = frozenset({"keywords", "searchterms"})


def parse_date(value: str, name: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value[:10])
    except (TypeError, ValueError) as exc:
        raise LimitExceeded(f"{name} must be an ISO date (YYYY-MM-DD)") from exc


def validate_granularity(
    level: str, granularity: str | None, start: dt.date, end: dt.date, today: dt.date | None = None
) -> None:
    today = today or dt.date.today()
    if granularity is None:
        return
    if granularity not in GRANULARITIES:
        raise LimitExceeded(f"granularity must be one of {GRANULARITIES}")
    age_start = (today - start).days
    age_end = (today - end).days
    span = (end - start).days
    if granularity == "HOURLY":
        if level in ("ads", "searchterms"):
            raise LimitExceeded("HOURLY granularity is not available for ads or searchterms reports")
        if age_start > 7:
            raise LimitExceeded("HOURLY granularity requires the range to start within the last 7 days")
    elif granularity == "DAILY":
        if age_start > 90:
            raise LimitExceeded("DAILY granularity requires the range to start within the last 90 days")
        if span < 1:
            raise LimitExceeded("DAILY granularity requires a range longer than one day; omit granularity for a single day")
    elif granularity == "WEEKLY":
        if age_start > 365:
            raise LimitExceeded("WEEKLY granularity requires the range to start within the last 365 days")
        if age_end < 14:
            raise LimitExceeded("WEEKLY granularity requires the end date to be at least 14 days in the past")
    elif granularity == "MONTHLY":
        if age_end < 90:
            raise LimitExceeded("MONTHLY granularity requires the end date to be at least 90 days in the past")


def build_report_request(
    *,
    level: str,
    start: str,
    end: str,
    time_zone: str | None = None,
    granularity: str | None = None,
    group_by: list[str] | None = None,
    fields: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    include_grand_total: bool = False,
    include_empty_metrics: bool = False,
    page_size: int = 500,
    max_report_days: int = 90,
    today: dt.date | None = None,
) -> tuple[str, dict[str, Any]]:
    """Validate inputs and return (path, body)."""
    level = (level or "").lower()
    if level not in LEVELS:
        raise LimitExceeded(f"level must be one of {sorted(LEVELS)}")
    start_d, end_d = parse_date(start, "start"), parse_date(end, "end")
    if end_d < start_d:
        raise LimitExceeded("end must be on or after start")
    check_date_range((end_d - start_d).days + 1, max_report_days)

    tz = (time_zone or "ORTZ").upper()
    if tz not in TIME_ZONES:
        raise LimitExceeded(f"time_zone must be one of {TIME_ZONES}")
    if level == "searchterms" and tz != "ORTZ":
        raise LimitExceeded("searchterms reports support only the ORTZ time zone")

    gran = granularity.upper() if granularity else None
    validate_granularity(level, gran, start_d, end_d, today)

    allowed_group_by = GROUP_BY[level]
    for dim in group_by or []:
        if dim not in allowed_group_by:
            raise LimitExceeded(f"group_by {dim!r} is not supported at level {level}; allowed: {allowed_group_by}")
        if gran == "HOURLY" and dim in HOURLY_EXCLUDED_GROUP_BY:
            raise LimitExceeded(f"group_by {dim!r} is not available with HOURLY granularity")

    for f in fields or []:
        if f not in METRICS:
            raise LimitExceeded(f"unknown report field {f!r}; see apple-ads://report-fields")

    allowed_filters = FILTER_FIELDS[level]
    for f in filters or []:
        if f.get("field") not in allowed_filters:
            raise LimitExceeded(f"filter field {f.get('field')!r} not allowed at level {level}; allowed: {allowed_filters}")
    if level in REQUIRES_CAMPAIGN_FILTER and not any(f.get("field") == "campaignId" for f in filters or []):
        raise LimitExceeded(
            f"{level} reports require a campaignId filter, e.g. "
            '[{"field": "campaignId", "operator": "EQUALS", "value": <id>}] '
            "(Apple rejects unfiltered keyword/search-term reports for apps)"
        )

    include_rows = []
    if include_grand_total:
        include_rows.append("GRAND_TOTAL")
    if include_empty_metrics:
        if group_by:
            raise LimitExceeded("EMPTY_METRICS cannot be combined with group_by")
        include_rows.append("EMPTY_METRICS")
    if include_rows and level == "searchterms":
        raise LimitExceeded("options.includeRows is not supported for searchterms reports")

    # `fields` is applied client-side after flattening: sending it upstream
    # makes Apple drop the metadata block too (keyword text, match type, bid
    # came back null — verified live 2026-09-22).
    body = report_query(
        start=start_d.isoformat(),
        end=end_d.isoformat(),
        time_zone=tz,
        granularity=gran,
        filters=filters,
        group_by=group_by,
        include_rows=include_rows or None,
        page_size=page_size,
    )
    return LEVELS[level], body


def default_date_range(days: int, today: dt.date | None = None) -> tuple[str, str]:
    """Last N full days ending yesterday (today's metrics are incomplete)."""
    today = today or dt.date.today()
    end = today - dt.timedelta(days=1)
    start = end - dt.timedelta(days=max(days, 1) - 1)
    return start.isoformat(), end.isoformat()


def freshness_warning(end: str, today: dt.date | None = None) -> str | None:
    today = today or dt.date.today()
    try:
        end_d = parse_date(end, "end")
    except LimitExceeded:
        return None
    if (today - end_d).days < 1:
        return "The range includes today; Apple's metrics for the current day are partial and still settling."
    return None


def report_fields_document() -> str:
    return resources.files("apple_ads_mcp").joinpath("reporting_fields.json").read_text()
