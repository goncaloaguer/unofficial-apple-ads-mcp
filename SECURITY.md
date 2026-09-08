# Security policy

## Reporting a vulnerability

Open a GitHub Security Advisory ("Report a vulnerability" on the Security
tab) rather than a public issue. Please do not include real advertiser data,
tokens, private keys, or account identifiers in reports.

## Design guarantees this project makes

- Every upstream call must match a version-controlled allowlist of read
  operations (`src/apple_ads_mcp/policy/read_operations.json`); write
  methods and create-via-POST are unreachable by construction, the loader
  refuses any enabled POST that is not a `/query` (or the reviewed
  `POST /v1/search/geo` lookup), and CI fails if that changes.
- Apple has no read-only OAuth scope; the server documents and checks for
  the read-only API role (`APPLE_ADS_REQUIRE_READONLY_ROLE=true` refuses to
  start otherwise), and cannot write regardless of role.
- Mandatory ad-account allowlist; the `X-AP-Context` header is derived only
  from an allowlisted account, never from tool input.
- Only `api.ads.apple.com` and `appleid.apple.com` are contacted; redirects
  are disabled; proxy environment variables are ignored.
- No database, no persistent cache, no payload logging. Report rows, entity
  names, search terms, tokens and key material never appear in logs or
  error messages.
- Rate/loop safeguards bound tool calls, upstream requests, pages, rows,
  response size, and runtime.

## Operator responsibilities

Secrets live in your environment (Secret Manager on Cloud Run). Rotate the
MCP credential and the Apple key pair if they may have leaked
(`docs/AUTHENTICATION.md` → Rotation). Do not share one deployment across
unrelated advertisers.
