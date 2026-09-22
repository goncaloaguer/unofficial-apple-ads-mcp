# Changelog

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
