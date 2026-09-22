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


def _status_filters(status: str | None, display_status: str | None = None) -> list[dict[str, Any]]:
    """Upstream filters. `status` is queryable; `displayStatus` is NOT
    ("Field, displayStatus, is invalid" — live 2026-09-22) and is applied
    locally by `_display_filter`."""
    filters: list[dict[str, Any]] = []
    if status:
        s = status.upper()
        if s not in _STATUS_VALUES:
            raise ValueError(f"status must be one of {_STATUS_VALUES}")
        filters.append({"field": "status", "operator": "EQUALS", "value": s})
    return filters


def _display_filter(rows: list[dict], display_status: str | None) -> list[dict]:
    if not display_status:
        return rows
    wanted = display_status.upper()
    return [r for r in rows if str(r.get("displayStatus", "")).upper() == wanted]


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
    rows = _display_filter(rows, display_status)
    meta["rows_returned"] = len(rows)
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
    rows = _display_filter(rows, display_status)
    meta["rows_returned"] = len(rows)
    rows = [compact(normalize_entity(r), verbose) for r in rows]
    automated = sum(1 for r in rows if r.get("automatedKeywordsOptIn"))
    return build_envelope(
        data=rows,
        meta=meta,
        account_id=account,
        summary={"ad_groups": len(rows), "search_match_enabled": automated},
        max_response_bytes=ctx.settings.max_response_bytes,
    )


def _compact_keyword(k: dict[str, Any], negative: bool = False) -> dict[str, Any]:
    """One chat-sized row per keyword (live: raw rows were ~270 B each; 284
    keywords in one campaign exceeded what chat clients render)."""
    bid = k.get("bid")
    row: dict[str, Any] = {
        "id": k.get("id"),
        "adGroupId": k.get("adGroupId"),
        "text": k.get("text"),
        "matchType": k.get("matchType"),
        "status": k.get("status"),
    }
    if negative:
        row["negative"] = True
        if k.get("adGroupId") is None:
            row["campaignId"] = k.get("campaignId")
    else:
        row["bid"] = bid.get("amount") if isinstance(bid, dict) else bid
        display = k.get("displayStatus")
        if display and display not in ("RUNNING", "PAUSED"):
            row["displayStatus"] = display  # e.g. AD_GROUP_ON_HOLD
    return row


async def list_keywords(
    ctx: AppContext,
    account_id: str | None = None,
    campaign_ids: list[str] | None = None,
    ad_group_ids: list[str] | None = None,
    status: str | None = None,
    include_negative: bool = False,
    text_contains: str | None = None,
    limit: int = 300,
) -> dict[str, Any]:
    """Keywords (bid, match type, status); optionally negative keywords too.

    Live-verified constraints (2026-09-22): ``/keywords/query`` rejects
    ``campaignId IN [...]`` (EQUALS only) and ``/negative-keywords/query``
    requires an ``adGroupId`` condition. So positive keywords are fetched
    with one EQUALS query per campaign (or per ad group), and negatives with
    one query per ad group — ad groups are resolved from the campaigns first
    when only campaign_ids are given.
    """
    account = resolve_account(ctx.settings, account_id)
    check_entity_ids(campaign_ids, ctx.settings.max_entity_ids)
    check_entity_ids(ad_group_ids, ctx.settings.max_entity_ids)
    if not campaign_ids and not ad_group_ids:
        raise ValueError("list_keywords needs campaign_ids or ad_group_ids (Apple rejects account-wide keyword queries)")
    budget = ctx.guard(
        "list_keywords",
        {"account_id": account, "campaign_ids": campaign_ids, "ad_group_ids": ad_group_ids,
         "status": status, "include_negative": include_negative, "text_contains": text_contains, "limit": limit},
    )
    page = min(1000, ctx.settings.max_page_size)
    limit = max(1, min(limit, ctx.settings.max_report_rows))
    base_filters = _status_filters(status)
    if text_contains:
        base_filters = base_filters + [{"field": "text", "operator": "LIKE", "value": text_contains, "ignoreCase": True}]
    warnings: list[str] = []
    meta: dict[str, Any] = {"pages_fetched": 0, "truncated": False, "source": "Apple Ads Platform API v1"}

    async def query(path: str, scope: list[dict[str, Any]]) -> list[dict]:
        body = entity_query(filters=scope, page_size=page, fetch_total_count=True)
        part, part_meta = await ctx.client.paginate("POST", path, budget=budget, account_id=account, json_body=body)
        meta["pages_fetched"] += part_meta.get("pages_fetched", 0)
        meta["truncated"] = meta["truncated"] or bool(part_meta.get("truncated"))
        return part

    # Positive keywords
    rows: list[dict] = []
    if campaign_ids:
        for cid in campaign_ids:
            rows.extend(await query("/v1/keywords/query", base_filters + _id_filter("campaignId", [cid])))
        if ad_group_ids:
            wanted = {str(i) for i in ad_group_ids}
            rows = [r for r in rows if str(r.get("adGroupId")) in wanted]
    else:
        for gid in ad_group_ids or []:
            rows.extend(await query("/v1/keywords/query", base_filters + _id_filter("adGroupId", [gid])))

    # Negative keywords: one query per ad group
    negatives: list[dict] = []
    if include_negative:
        group_ids = [str(i) for i in ad_group_ids] if ad_group_ids else sorted({str(r.get("adGroupId")) for r in rows if r.get("adGroupId")})
        if not group_ids and campaign_ids:
            for cid in campaign_ids:
                groups = await query("/v1/adgroups/query", _id_filter("campaignId", [cid]))
                group_ids += [str(g["id"]) for g in groups]
        remaining = ctx.settings.max_subrequests_per_call - budget.used
        if len(group_ids) > remaining:
            warnings.append(
                f"negative keywords fetched for {remaining} of {len(group_ids)} ad groups (subrequest ceiling); "
                "narrow with ad_group_ids for the rest"
            )
            group_ids = group_ids[:remaining]
        for gid in group_ids:
            negatives.extend(await query("/v1/negative-keywords/query", _status_filters(status) + _id_filter("adGroupId", [gid])))
        warnings.append("campaign-level negative keywords are not returned by the per-ad-group query (Apple requires adGroupId); TODO-LIVE")

    rows = [normalize_entity(r) for r in rows]
    data = [_compact_keyword(r) for r in rows] + [_compact_keyword(n, negative=True) for n in negatives]
    if len(data) > limit:
        warnings.append(f"{len(data)} keywords match; returning the first {limit}. Narrow with ad_group_ids, text_contains or status, or raise limit.")
        data = data[:limit]
        meta["truncated"] = True
    currency = next((r["bid"].get("currency") for r in rows if isinstance(r.get("bid"), dict)), None)
    by_match: dict[str, int] = {}
    for r in rows:
        key = str(r.get("matchType") or "UNKNOWN")
        by_match[key] = by_match.get(key, 0) + 1
    by_group: dict[str, int] = {}
    for r in rows:
        key = str(r.get("adGroupId"))
        by_group[key] = by_group.get(key, 0) + 1
    meta["rows_returned"] = len(data)
    return build_envelope(
        data=data,
        meta=meta,
        account_id=account,
        summary={"keywords": len(rows), "negative_keywords": len(negatives) if include_negative else None,
                 "currency": currency, "by_match_type": by_match, "by_ad_group": by_group},
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
