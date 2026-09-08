# ADR-001 — Client layer: thin `httpx` client, not Apple's official SDK

**Status:** accepted (2026-09-02)
**Deciders:** project owner
**Related:** PLAN.md §3.4, §4, §10

## Context

Apple publishes an official, MIT-licensed, Apple-maintained Python client for
the Apple Ads Platform API — [`apple-ads-platform`](https://github.com/apple/apple-ads-platform-api-python)
(PyPI 1.109.0, released 2026-08-14). It is generated from Apple's OpenAPI
spec with openapi-generator, ships 374 typed models, and handles the OAuth
token lifecycle. It is a good library for read-write integrations, and we
seriously considered building on it.

This project has two properties that dominate the choice: it must be
**permanently read-only by construction**, and it holds **ad-account
credentials** in a small, single-owner service.

## Decision

Implement the Apple client as a small async `httpx` layer that sits behind the
read-operation registry (`policy/read_operations.json` + `policy/registry.py`),
mirroring the architecture of `unofficial-reddit-ads-mcp`.

Use Apple's SDK repository **only** as the pinned, machine-readable source of
truth for the operation inventory and model shapes:

- `scripts/extract_operations.py` parses `apple_ads_api.py` at the commit in
  `spec/SDK_PIN` into `spec/operations.json` (99 operations).
- `scripts/check_api_drift.py` fails CI when Apple's inventory changes until
  every new/changed operation is classified.
- Model modules are consulted (never imported) when a field shape is unclear.

## Reasons

1. **Read-only must be structural.** The SDK's single `AppleAdsApi` class
   exposes all 99 operations, including every create, update, delete, bulk,
   upload, apply and dismiss. Built on it, "read-only" is a promise our
   wrapper makes, and the invariant test degrades to "we never call these 36
   methods". With the thin client the write code does not exist in the
   process; `tests/test_read_only_invariants.py` enumerates every reachable
   operation and the loader refuses any non-`/query` POST.
2. **Same architecture as the sibling project.** Registry → client → tools →
   analysis, async end to end, is already reviewed, tested and deployed.
3. **Async and composite tools.** The SDK is synchronous (`urllib3`); every
   call inside FastMCP would need a thread hop, and bounded-concurrency
   composite tools become clumsier.
4. **Rate limiting.** The SDK does not act on `RateLimit-*` / `Retry-After`;
   Apple's own docs show reading them manually from `_with_http_info`
   tuples. `apple/http_policy.py` does this centrally either way.
5. **Dependency surface.** Thin client: `mcp`, `httpx`, `pydantic`,
   `pyjwt[crypto]`. The SDK adds `urllib3`, `python-dateutil`,
   `typing-extensions` and 374 generated modules, and requires Python ≥ 3.12.
   This process holds credentials; less surface is better.
6. **Maturity.** At decision time the SDK had a single "initial release"
   commit, 19 days old. Being an early adopter of a generated v1 client in a
   credential-holding service is a risk the project does not need; the API is
   documented well enough to call directly.

## Consequences

- We own ~40 lines of auth (`auth/apple_oauth.py`: ES256 client-secret JWT +
  `client_credentials`) and the request/response shapes of the read
  operations the tools use. Both are covered by tests
  (`tests/test_client_e2e.py` signs real ES256 tokens against a mocked
  transport).
- New Apple endpoints appear as CI drift, not as silently available SDK
  methods — deliberate friction.
- If write tooling is ever wanted, it belongs in a separate repository, and
  that is the moment to reconsider the SDK.

## Alternatives considered

- **Official SDK as runtime dependency** — rejected for the reasons above.
- **Fork an existing community MCP server** — the candidates
  ([AppVisionOS/apple-search-ads-mcp](https://github.com/AppVisionOS/apple-search-ads-mcp),
  [gregtuc/asa-mcp](https://github.com/gregtuc/asa-mcp)) are TypeScript and
  target the deprecated Campaign Management API v5 (sunset 2027-01-26);
  [crevas/Apple-Ads-CLI](https://github.com/crevas/Apple-Ads-CLI) is a Go
  CLI; [ppcprophet/apple-ads-mcp](https://github.com/ppcprophet/apple-ads-mcp)
  routes auth through a hosted token service. None matched the Python,
  self-hosted, structurally read-only requirements. Credited as prior art in
  the README; no code was reused.
