# Apple Ads Platform API — notes from building and running this server

Facts here are either (D) from Apple's documentation, (S) from Apple's
official SDK models (spec.version 109), or (L) verified live against an
advertiser account. Items marked **TODO-LIVE** are Phase 1 acceptance items
(PLAN.md §3.3) still to be confirmed against a real account.

1. **Base URL and headers (D).** `https://api.ads.apple.com/v1/…`;
   `Authorization: Bearer …` everywhere; `X-AP-Context: adAccountId=<id>`
   on ad-account-scoped calls. Not needed for `/me`, `/acls`, `/orgs/{id}`,
   `/advertiser-resources`; optional for `shared-budgets` (S).
   **TODO-LIVE:** the SDK sends `adAccountId=<id>;` with a trailing
   semicolon, the docs without — confirm both are accepted.
2. **Token (D).** `POST https://appleid.apple.com/auth/oauth2/token`,
   `grant_type=client_credentials`, `scope=searchadsorg`, 3600 s TTL. The
   client secret is an ES256 JWT (`iss=teamId`, `sub=clientId`,
   `aud=https://appleid.apple.com`, `kid=keyId`, `exp` ≤ 180 days).
3. **Roles (L).** `GET /v1/acls` returns `roles: [string]` per account —
   an open list, not an enum. The API's names differ from the UI's: a user
   shown as "API Account Manager" in the UI comes back as
   `"API Campaign Manager"` (verified live 2026-09-22). The read-only role
   is therefore expected as `"API Campaign Read Only"`; the UI's "API
   Account Read Only" is kept in the allowlist as well. Also observed: the
   org ID and the primary ad account ID were the same number, and a legacy
   "Search Ads Basic" account appears as a separate ad account.
4. **Four query-body families (S).** Entity `/query` uses
   `QueryFilter{field, operator, value, ignoreCase}` +
   `QueryPagination{offset, pageSize, fetchTotalCount}`. Reports/insights use
   `Filter{field, operator, value}` + `RequestPagination{offset, pageSize}`
   (no `fetchTotalCount`). Recommendations/suggestions use
   `FilterCondition{…, value: [..]}` (always an array) + `pageSize ≤ 1000`.
   Change history uses `AuditFilter` + `options.needTotals` as the *string*
   `"true"|"false"`.
5. **Entity query operators (S).** `EQUALS, NOT_EQUALS, IN, NOT_IN, LIKE,
   NOT_LIKE, STARTS_WITH, ENDS_WITH, GREATER_THAN[_OR_EQUAL_TO],
   LESS_THAN[_OR_EQUAL_TO], BETWEEN, IS_NULL, IS_NOT_NULL, CONTAINS_ANY,
   CONTAINS_ALL, NOT_CONTAINS_ANY, NOT_CONTAINS_ALL` — no `CONTAINS`
   ("name contains" = `LIKE` + `ignoreCase`). Report filters do have
   `CONTAINS`.
6. **Report filters (L, 2026-09-22).** `campaignId` and `adGroupId` are
   accepted (EQUALS verified). `keywordId` is rejected with
   `INVALID_FIELD_ATTRIBUTE: Filters contain unsupported fields keywordId`.
   Keyword and search-term reports **require** a `campaignId` filter:
   `INVALID_VALUE_FIELD: campaignId filter is required for KEYWORD reports
   when promotedObjectType is APPS`. Other names in
   `reporting.FILTER_FIELDS` remain unverified.
6a. **Report `fields` strips metadata (L).** Sending `fields: [...]` makes
   Apple omit metadata too (keyword `text`, `matchType`, `bid` came back
   null). This server no longer sends `fields` upstream; it prunes metrics
   client-side after flattening.
6b. **Entity query operators are per-endpoint (L).** `POST /v1/keywords/query`
   rejects `campaignId IN [...]` (`INVALID_INPUT: campaignId condition must
   use EQUALS operator`) while `/v1/adgroups/query` and `/v1/ads/query`
   accept IN. `list_keywords` fans out one EQUALS query per campaign.
6b'. **More per-endpoint rules (L).** `/negative-keywords/query` requires an
   `adGroupId` condition (`INVALID_INPUT: adGroupId condition is required`);
   `list_keywords` resolves ad groups first and queries each. Whether
   campaign-level negative keywords are reachable at all this way is
   **TODO-LIVE**. `displayStatus` is not a queryable entity field
   (`Field, displayStatus, is invalid`) — filtered locally. `status EQUALS`
   and `name LIKE` (ignoreCase) work. Keyword `displayStatus` can be
   `AD_GROUP_ON_HOLD` (inherited from the parent).
6c. **Search-term privacy aggregate (L).** Rows with `searchTermText: null`
   are Apple's low-volume bucket (terms below the reporting threshold,
   aggregated per keyword). They still carry spend/taps/installs.
6d. **Ads may not exist (L).** Campaigns using the default App Store product
   page have no ad entities; `ads/query` returns an empty list for them.
   Legacy 2021-era ads appeared with `CREATIVE_SET_INVALID` /
   `ASSET_DELETED` reasons.
7. **Report rows (S).** `{totalMetrics, granularMetrics?[], metadata}`;
   `granularMetrics` only when `granularity` is set, each carrying `date`.
   Keyword rows add `insights.bidRecommendation`. Search-term metadata:
   `searchTermText, searchTermSource, keyword{id,text,matchType,bid},
   adGroupId, campaignId`. Campaign/ad-group metadata is the entity itself
   (`id`, `name`, `status`, `displayStatus`, …) plus groupBy dimension values.
8. **Money (S).** `{amount: "12.34", currency: "USD"}` — `amount` is a
   string with up to two decimals. Normalized to floats here.
9. **Granularity windows (D).** HOURLY: start within 7 days, not for
   ads/searchterms, excludes demographic/geo groupBys (S). DAILY: start
   within 90 days, range > 1 day. WEEKLY: start within 365 days, end ≥ 14
   days ago. MONTHLY: end ≥ 90 days ago. Single day: omit granularity.
10. **Search terms (D/S).** `ORTZ` only; `options.includeRows` unsupported.
11. **`EMPTY_METRICS` (D)** cannot be combined with `groupBy`.
12. **Rate limits (D).** `RateLimit-Limit/Remaining/Reset` on every
    response; `Retry-After` on 429 (`rate_limit_exceeded`). **TODO-LIVE:**
    actual quota values per endpoint family.
13. **Change history (D/S).** `eventTime` filter required (max 6 months).
    Docs list `eventType` as filterable; the SDK's `AuditFilter` does not —
    filter client-side. `detailId` = `EntityType.entityId.txnId`.
    **TODO-LIVE:** whether `metas[]` carries `detailId` with
    `options.metadata=latest`.
14. **Insights (D/S).** Impression share needs a `promotedObjectId` filter;
    DAILY ≤ 30 days or WEEKLY_SUN_SAT ≤ 4 weeks starting on a Sunday; UTC.
    Search-term popularity: WEEKLY_SUN_SAT (65-week rolling retention,
    generated Mondays 07:00 UTC) or MONTHLY (15 months, refreshed on the 5th).
15. **Suggestions/recommendations (S).** Budget/Target-CPA recommendations
    filter on `promotedObjectId` = *campaign* ID; target-CPA *suggestion*
    filters on app ID + `promotedObjectType=APPSTORE_APP` and is limited to
    5 QPS; phrase/category suggestions need `queryType: SUGGESTION|SEARCH`.
16. **Geo search (S).** Both `GET` and `POST /v1/search/geo` require
    `supplySource` (`APPSTORE|MAPS`).
17. **`productFeatures` / `supplyPlacement` values (L).** Verified live:
    `productFeatures: ["APPSTORE_APP_MANUAL"]` and
    `targeting.supplyPlacement.include: ["APPSTORE_SEARCH_RESULTS"]`, exactly
    as the docs name them. Ad-group targeting seen: `deviceClass.include:
    ["IPHONE"]`, `adminArea.include: [numeric geo IDs]`.
17a. **Money wrapping differs by field (L).** Campaign `dailyBudget` is
    `{"value": {"amount": "100", "currency": "GBP"}}` (wrapped), ad-group
    `bidStrategy.bid` and keyword `bid` are bare `{amount, currency}`. Both
    are normalized to `{amount: float, currency}`.
17b. **Payload size (L).** 46 campaigns as raw entities ≈ 57 KB, more than
    chat clients render. List tools now compact by default (drop
    `regulationResponses`, `adAccountId`, `deleted`, timestamps, billing
    fields; flatten `targeting`); `verbose=true` returns the raw entities.
18. **`/healthz` (L, inherited).** Intercepted by Google Frontend on
    `run.app` domains — the health endpoint is `/health`.
19. **MCP SDK (L, inherited).** `mcp` 2.x moved `mcp.server.fastmcp`; pin
    `>=1.9,<2`.
