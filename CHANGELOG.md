# Changelog

## 0.2.6 — tunable ceilings

- `MAX_TOOL_CALLS_PER_HOUR` (default 60, hard cap 500) and
  `MAX_SUBREQUESTS_PER_CALL` (default 20, hard cap 50) can now be set in the
  environment; invalid values fail startup. docs/DEPLOY_GCP.md §9.

## 0.2.5 — genre list recorded

- Apple's 15 popularity genres enumerated live and documented; `genre`
  accepts the App Store names Apple merges (`Productivity` →
  `PRODUCTIVITY_UTILITIES`, `News` → `NEW_PUBLICATION`). No schema change.
- `scripts/release.sh`: one-command commit/push/tag/build/deploy using a
  git-ignored `deploy.env` (docs/DEPLOY_GCP.md §8). PLAN.md status updated.

## 0.2.4 — genre discovery

- `get_search_term_popularity(list_genres=true)`: enumerates the genre
  tokens Apple accepts for the given storefronts (`PRODUCTIVITY` and
  `MEDICAL` are rejected live, so the set is not the App Store category
  list and no endpoint publishes it).

## 0.2.3 — genre token

- `get_search_term_popularity`: Apple's genre token drops the conjunction
  (`HEALTH_FITNESS`, not `HEALTH_AND_FITNESS`); the normalizer and docs
  updated. Verified live: impression share with local text filter (394
  rows scanned), geo id resolution with `entity`.

## 0.2.2 — second Phase 2/3 live round

- `get_impression_share` / `get_search_term_popularity`: `search_term_contains`
  is now matched locally — Apple's insights filters accept neither
  `CONTAINS` (silently empty) nor `LIKE` (rejected); `meta.rows_scanned`
  reports the scan size.
- `search_geo`: id resolution requires `entity` (Apple: "Each geoRequest
  must have entity"); ids sent as strings per the SDK model.
- Verified live after 0.2.1: impression share (394 rows/week for one app
  and storefront), recommendations query accepted (no active
  recommendations on the test account), geo name search for Country and
  AdminArea.

## 0.2.1 — Phase 2/3 live-verification fixes

- `get_impression_share`: `promotedObjectId` filter uses `IN` (Apple
  rejects `EQUALS`); text filter uses `LIKE`.
- `get_search_term_popularity`: `genre` normalized to Apple's enum token
  (`Health & Fitness` → `HEALTH_AND_FITNESS`); text filter uses `LIKE`
  (`CONTAINS` silently returned nothing).
- `get_recommendations`: `promotedObjectType` is `APPSTORE_APP` for
  campaign-id queries (`CAMPAIGN` is rejected).
- `search_geo`: `entity` mapped to Apple's CamelCase enum
  (`Locality`, `AdminArea`, …); upper-case values returned empty results.
- `get_keyword_suggestions`: storefronts passed to the phrase query too.
- Compact analysis rows: zero-valued pre-order/redownload splits dropped
  from ranked, keyword and search-term rows; `compare_periods` no longer
  duplicates the account comparison in `summary.totals`;
  `analyze_search_terms` returns `top_exact_keyword_terms` instead of
  repeating the expansion candidates.
- Mock Apple in the e2e suite now enforces the above live rules.
- docs/API_NOTES.md: items 13–16 live-verified; 16a added.

## 0.2.0 — Phases 2 and 3: analysis, diagnostics and Apple insights

- **Analysis tools (Phase 2)**: `compare_periods`, `rank_performance`,
  `analyze_trends`, `analyze_pacing`, `analyze_keywords`,
  `analyze_search_terms`, `get_account_history`. All derived values
  (CPI, CPT, TTR, install rates, deltas, utilization, anomalies) are
  computed locally from Apple's summable totals with the formula returned
  alongside each result; zero denominators yield `null`.
- **Apple-intelligence tools (Phase 3)**: `diagnose_delivery`,
  `check_app_eligibility`, `get_impression_share`,
  `get_search_term_popularity`, `get_keyword_suggestions`,
  `get_recommendations`, `get_target_cpa_suggestion`, `search_apps`,
  `get_app_details`, `search_geo`, `get_supported_languages`.
- Prompts `weekly_performance_review` and `diagnose_performance_drop`;
  resource `apple-ads://api-notes` (the packaged copy of
  docs/API_NOTES.md, CI-checked to stay identical).
- Envelope: the response-size warning now fires only when the size ceiling
  truncated rows, not when a tool's own `limit` did.
- Registry unchanged: every new tool composes already-enabled read
  operations; the read-only invariants suite still passes and no write path
  became reachable.
- Tests: 126 (new `tests/test_analysis.py`; mocked end-to-end coverage for
  all Phase 2/3 tools).

## 0.1.3 — second live round

- `list_keywords`: requires campaign_ids or ad_group_ids; negative keywords
  fetched per ad group (Apple requires `adGroupId`); compact one-line rows;
  `text_contains` and `limit` (default 300) parameters.
- `display_status` filters are applied locally on campaigns and ad groups —
  Apple rejects `displayStatus` as a query field.
- Response ceiling default lowered to 40 KB (`MAX_RESPONSE_BYTES` to
  override): chat clients refuse larger tool results; the envelope now
  truncates with a warning instead.
- README status updated: Phase 1 live-verified.

## 0.1.2 — live-verification fixes

- Reports: keyword and search-term levels now require a `campaignId` filter
  locally (Apple's rule); `keywordId` removed from allowed filters; `fields`
  is applied client-side because sending it upstream strips Apple's
  metadata block.
- `list_keywords` fans out one `campaignId EQUALS` query per campaign —
  Apple rejects `IN` on that endpoint. Single-id filters use EQUALS
  everywhere.
- List tools compact their output by default (noise fields dropped,
  targeting flattened, empty reason arrays removed); `verbose=true` returns
  Apple's raw entities. A 46-campaign account went from ~57 KB to a size
  chat clients render.
- Money normalization handles the `{"value": {amount, currency}}` wrapper
  used by campaign `dailyBudget`.
- docs/API_NOTES.md: nine live findings recorded; TODO-LIVE items 6 and 17
  closed.

## 0.1.1

- Fix: the startup role check ran in its own event loop and the same
  context was reused for serving, so every tool call failed with "Event loop
  is closed" (found on the first live call). The check now uses a throwaway
  context; the server gets a fresh one. Regression test added.
- Recognize `"API Campaign Read Only"` (the API's name for the UI's "API
  Account Read Only"); live findings recorded in docs/API_NOTES.md.

## 0.1.0 (Phase 1 — private MVP)

- Read-only MCP server for the Apple Ads Platform API (App Store campaigns).
- Operation inventory extracted from Apple's official SDK (spec.version 109):
  99 operations classified — 47 enabled reads, 16 Apple Maps reads disabled,
  36 writes denied. Registry loader refuses any enabled POST that is not a
  `/query` (except the reviewed `POST /v1/search/geo` lookup).
- Apple OAuth: ES256 client-secret JWT + `client_credentials`
  (`searchadsorg`), in-memory token cache, startup ACL role check.
- Policy-enforcing async client: `X-AP-Context` from allowlisted accounts
  only, offset pagination with caps, `RateLimit-*` / `Retry-After` handling,
  structured error surfacing.
- Tools (7): `list_ad_accounts`, `list_campaigns`, `list_ad_groups`,
  `list_keywords`, `list_ads`, `get_report`, `get_daily_performance`.
  Resources: `apple-ads://report-fields`, `apple-ads://capabilities`.
- Transports: stdio and stateless Streamable HTTP with bearer or secret-path
  authentication. Dockerfile and Cloud Run guide.
- Tests: read-only invariants (stdlib-only), unit tests, report validation,
  and mocked end-to-end tests with real ES256 signing.
