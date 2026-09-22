"""Account-structure tools: accounts, campaigns, ad groups, keywords, ads.

Plain async functions (SDK-free, unit-testable); app.py registers them as
MCP tools. Entity names, keyword and search-term text are advertiser data
and treated as untrusted strings — never as instructions.
"""
from __future__ import annotations

from typing import Any

from apple_ads_mcp.apple.normalize import normalize_entity
from apple_ads_mcp.apple.query import entity_query
from apple_ads_mcp.context import AppContext
from apple_ads_mcp.envelope import build_envelope
from apple_ads_mcp.policy.accounts import (
    filter_allowed,
    resolve_account,
    write_capable_roles,
)
from apple_ads_mcp.policy.limits import check_entity_ids

_STATUS_VALUES = ("ENABLED", "PAUSED")

# Fields dropped from list outputs unless verbose=True. Verified live
# (2026-09-22): a 46-campaign account produced ~57 KB with the raw entities,
# past what chat clients render; these fields carried ~40% of it and no
# analytical value (regulationResponses alone was 3.4 KB of NOT_ANSWERED).
_NOISE_FIELDS = frozenset(
    {"adAccountId", "deleted", "regulationResponses", "invoiceDetail", "creationTime",
     "paymentModel", "billingEvent", "automatedKeywordsRequired", "pricingModel"}
)


def _targeting_summary(targeting: dict[str, Any] | None) -> dict[str, Any] | None:
    """Flatten {dim: {include: [...], exclude: [...]}} to {dim: [...], dim_exclude: [...]}."""
    if not isinstance(targeting, dict):
        return targeting
    out: dict[str, Any] = {}
    for dim, spec in targeting.items():
        if isinstance(spec, dict):
            if spec.get("include"):
                out[dim] = spec["include"]
            if spec.get("exclude"):
                out[f"{dim}_exclude"] = spec["exclude"]
        else:
            out[dim] = spec
    return out


def compact(entity: dict[str, Any], verbose: bool) -> dict[str, Any]:
    """Drop noise fields and flatten targeting/bid for chat-sized outputs."""
    if verbose:
        return entity
    out = {k: v for k, v in entity.items() if k not in _NOISE_FIELDS}
    if "targeting" in out:
        out["targeting"] = _targeting_summary(out["targeting"])
    bid_strategy = out.get("bidStrategy")
    if isinstance(bid_strategy, dict):
        flat = dict(bid_strategy)
        if isinstance(flat.get("bid"), dict):
            flat["bid"] = normalize_entity({"bid": flat["bid"]})["bid"]
        out["bidStrategy"] = flat
    for key in ("systemStatusReasons", "systemStatusLimitingReasons", "sharedBudgets", "endTime"):
        if key in out and not out[key]:
            out.pop(key)
    return out


def _status_filters(status: str | None, display_status: str | None) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = []
    if status:
        s = status.upper()
        if s not in _STATUS_VALUES:
            raise ValueError(f"status must be one of {_STATUS_VALUES}")
        filters.append({"field": "status", "operator": "EQUALS", "value": s})
    if display_status:
        filters.append({"field": "displayStatus", "operator": "EQUALS", "value": display_status.upper()})
    return filters


def _coerce_ids(ids: list[str]) -> list[Any]:
    return [int(i) if str(i).isdigit() else i for i in ids]


def _id_filter(field: str, ids: list[str] | None) -> list[dict[str, Any]]:
    """EQUALS for a single id, IN for several (campaigns/ad groups/ads accept IN)."""
    if not ids:
        return []
    values = _coerce_ids(ids)
    if len(values) == 1:
        return [{"field": field, "operator": "EQUALS", "value": values[0]}]
    return [{"field": field, "operator": "IN", "value": values}]


async def list_ad_accounts(ctx: AppContext) -> dict[str, Any]:
    """Allowlisted ad accounts with currency, timezone, status, product features, and roles."""
    budget = ctx.guard("list_ad_accounts", {})
    acl_payload = await ctx.client.request("GET", "/v1/acls", budget=budget, account_id=None)
    acls = ((acl_payload.get("result") or {}).get("acls")) or []
    allowed = filter_allowed(ctx.settings, acls)
    roles_by_account = {str(a["adAccount"]["id"]): list(a.get("roles") or []) for a in allowed}

    rows: list[dict] = []
    warnings: list[str] = list(ctx.settings.warnings)
    for account_id in sorted(ctx.settings.allowed_account_ids):
        if account_id not in roles_by_account:
            warnings.append(
                f"allowlisted account {account_id} is not visible to this API user (absent from GET /v1/acls)"
            )
            continue
        payload = await ctx.client.request(
            "GET", f"/v1/ad-accounts/{account_id}", budget=budget, account_id=account_id
        )
        account = normalize_entity(payload.get("result") or {})
        account["roles"] = roles_by_account[account_id]
        risky = write_capable_roles(account["roles"])
        if risky:
            warnings.append(
                f"account {account_id}: API user holds role(s) {risky} which may permit writes. "
                "This server cannot write, but Apple-side least privilege is recommended: "
                "use the 'API Account Read Only' role."
            )
        features = account.get("productFeatures") or []
        if features and not any("APPSTORE" in str(f).upper() for f in features):
            warnings.append(
                f"account {account_id} productFeatures={features}: v1 tools target App Store campaigns"
            )
        rows.append(account)
    return build_envelope(
        data=rows,
        meta={"rows_returned": len(rows), "truncated": False},
        summary={"allowed_accounts": len(rows)},
        warnings=warnings,
    )


async def list_campaigns(
    ctx: AppContext,
    account_id: str | None = None,
    status: str | None = None,
    display_status: str | None = None,
    name_contains: str | None = None,
    promoted_object_id: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Campaigns with budget, bid strategy, targeting, status trio and reasons."""
    account = resolve_account(ctx.settings, account_id)
    filters = _status_filters(status, display_status)
    if name_contains:
        filters.append({"field": "name", "operator": "LIKE", "value": name_contains, "ignoreCase": True})
    if promoted_object_id:
        filters.append({"field": "promotedObjectId", "operator": "EQUALS", "value": str(promoted_object_id)})
    budget = ctx.guard(
        "list_campaigns",
        {"account_id": account, "status": status, "display_status": display_status,
         "name_contains": name_contains, "promoted_object_id": promoted_object_id, "verbose": verbose},
    )
    body = entity_query(filters=filters, page_size=min(500, ctx.settings.max_page_size), fetch_total_count=True)
    rows, meta = await ctx.client.paginate(
        "POST", "/v1/campaigns/query", budget=budget, account_id=account, json_body=body
    )
    rows = [compact(normalize_entity(r), verbose) for r in rows]
    by_display: dict[str, int] = {}
    for r in rows:
        key = str(r.get("displayStatus") or "UNKNOWN")
        by_display[key] = by_display.get(key, 0) + 1
    return build_envelope(
        data=rows,
        meta=meta,
        account_id=account,
        summary={"campaigns": len(rows), "by_display_status": by_display},
        max_response_bytes=ctx.settings.max_response_bytes,
    )


async def list_ad_groups(
    ctx: AppContext,
    account_id: str | None = None,
    campaign_ids: list[str] | None = None,
    status: str | None = None,
    display_status: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    """Ad groups with bid strategy, automation, targeting dimensions, schedule, status."""
    account = resolve_account(ctx.settings, account_id)
    check_entity_ids(campaign_ids, ctx.settings.max_entity_ids)
    filters = _status_filters(status, display_status) + _id_filter("campaignId", campaign_ids)
    budget = ctx.guard(
        "list_ad_groups",
        {"account_id": account, "campaign_ids": campaign_ids, "status": status,
         "display_status": display_status, "verbose": verbose},
    )
    body = entity_query(filters=filters, page_size=min(500, ctx.settings.max_page_size), fetch_total_count=True)
    rows, meta = await ctx.client.paginate(
        "POST", "/v1/adgroups/query", budget=budget, account_id=account, json_body=body
    )
    rows = [compact(normalize_entity(r), verbose) for r in rows]
    automated = sum(1 for r in rows if r.get("automatedKeywordsOptIn"))
    return build_envelope(
        data=rows,
        meta=meta,
        account_id=account,
        summary={"ad_groups": len(rows), "search_match_enabled": automated},
        max_response_bytes=ctx.settings.max_response_bytes,
    )


async def list_keywords(
    ctx: AppContext,
    account_id: str | None = None,
    campaign_ids: list[str] | None = None,
    ad_group_ids: list[str] | None = None,
    status: str | None = None,
    include_negative: bool = False,
) -> dict[str, Any]:
    """Keywords (bid, match type, status); optionally negative keywords too.

    Verified live (2026-09-22): the keywords query rejects ``campaignId IN
    [...]`` ("campaignId condition must use EQUALS operator"), so this fans
    out one EQUALS query per campaign and filters ad groups client-side.
    """
    account = resolve_account(ctx.settings, account_id)
    check_entity_ids(campaign_ids, ctx.settings.max_entity_ids)
    check_entity_ids(ad_group_ids, ctx.settings.max_entity_ids)
    budget = ctx.guard(
        "list_keywords",
        {"account_id": account, "campaign_ids": campaign_ids, "ad_group_ids": ad_group_ids,
         "status": status, "include_negative": include_negative},
    )
    page = min(1000, ctx.settings.max_page_size)
    base_filters = _status_filters(status, None)
    wanted_ad_groups = {str(i) for i in ad_group_ids} if ad_group_ids else None

    async def fetch(path: str) -> tuple[list[dict], dict[str, Any], list[str]]:
        rows: list[dict] = []
        meta: dict[str, Any] = {"pages_fetched": 0, "truncated": False}
        warnings: list[str] = []
        scopes: list[list[dict[str, Any]]]
        if campaign_ids:
            scopes = [base_filters + _id_filter("campaignId", [cid]) for cid in campaign_ids]
        elif ad_group_ids:
            scopes = [base_filters + _id_filter("adGroupId", [gid]) for gid in ad_group_ids]
        else:
            scopes = [base_filters]
        for scope in scopes:
            body = entity_query(filters=scope, page_size=page, fetch_total_count=True)
            part, part_meta = await ctx.client.paginate(
                "POST", path, budget=budget, account_id=account, json_body=body
            )
            rows.extend(part)
            meta["pages_fetched"] += part_meta.get("pages_fetched", 0)
            meta["truncated"] = meta["truncated"] or bool(part_meta.get("truncated"))
        if wanted_ad_groups is not None and campaign_ids:
            rows = [r for r in rows if str(r.get("adGroupId")) in wanted_ad_groups]
        if meta["truncated"]:
            warnings.append(f"{path.rsplit('/', 2)[1]} list truncated; narrow by campaign_ids/ad_group_ids")
        meta["rows_returned"] = len(rows)
        meta["source"] = "Apple Ads Platform API v1"
        return rows, meta, warnings

    rows, meta, warnings = await fetch("/v1/keywords/query")
    rows = [compact(normalize_entity(r), False) for r in rows]
    negatives: list[dict] = []
    if include_negative:
        neg_rows, _, neg_warnings = await fetch("/v1/negative-keywords/query")
        negatives = [compact(normalize_entity(r), False) for r in neg_rows]
        warnings += neg_warnings
    by_match: dict[str, int] = {}
    for r in rows:
        key = str(r.get("matchType") or "UNKNOWN")
        by_match[key] = by_match.get(key, 0) + 1
    data: dict[str, Any] = {"keywords": rows}
    if include_negative:
        data["negative_keywords"] = negatives
    return build_envelope(
        data=data,
        meta=meta,
        account_id=account,
        summary={"keywords": len(rows), "by_match_type": by_match,
                 "negative_keywords": len(negatives) if include_negative else None},
        warnings=warnings,
        max_response_bytes=ctx.settings.max_response_bytes,
    )


async def list_ads(
    ctx: AppContext,
    account_id: str | None = None,
    campaign_ids: list[str] | None = None,
    ad_group_ids: list[str] | None = None,
    status: str | None = None,
    include_creatives: bool = True,
    verbose: bool = False,
) -> dict[str, Any]:
    """Ads with status trio and reasons, joined with their creatives (type, destination, eligibility)."""
    account = resolve_account(ctx.settings, account_id)
    check_entity_ids(campaign_ids, ctx.settings.max_entity_ids)
    check_entity_ids(ad_group_ids, ctx.settings.max_entity_ids)
    filters = _status_filters(status, None) + _id_filter("campaignId", campaign_ids) + _id_filter("adGroupId", ad_group_ids)
    budget = ctx.guard(
        "list_ads",
        {"account_id": account, "campaign_ids": campaign_ids, "ad_group_ids": ad_group_ids,
         "status": status, "include_creatives": include_creatives, "verbose": verbose},
    )
    body = entity_query(filters=filters, page_size=min(500, ctx.settings.max_page_size), fetch_total_count=True)
    rows, meta = await ctx.client.paginate(
        "POST", "/v1/ads/query", budget=budget, account_id=account, json_body=body
    )
    rows = [compact(normalize_entity(r), verbose) for r in rows]
    warnings: list[str] = []
    if include_creatives and rows:
        creative_ids = sorted({r.get("creativeId") for r in rows if r.get("creativeId") is not None})
        creatives: dict[Any, dict] = {}
        for i in range(0, len(creative_ids), 100):
            chunk = creative_ids[i : i + 100]
            c_body = entity_query(filters=[{"field": "id", "operator": "IN", "value": chunk}], page_size=100)
            try:
                c_rows, _ = await ctx.client.paginate(
                    "POST", "/v1/creatives/query", budget=budget, account_id=account, json_body=c_body, max_pages=1
                )
            except Exception as exc:  # partial-result policy: keep ads, warn
                warnings.append(f"creative lookup failed for {len(chunk)} creative IDs: {exc.__class__.__name__}")
                continue
            for c in c_rows:
                creatives[c.get("id")] = {
                    k: c.get(k) for k in ("id", "name", "creativeType", "destination", "systemStatus", "systemStatusReasons", "eligibility")
                }
        for r in rows:
            r["creative"] = creatives.get(r.get("creativeId"))
    not_running = [r for r in rows if r.get("systemStatus") == "NOT_RUNNING"]
    return build_envelope(
        data=rows,
        meta=meta,
        account_id=account,
        summary={"ads": len(rows), "not_running": len(not_running)},
        warnings=warnings,
        max_response_bytes=ctx.settings.max_response_bytes,
    )
