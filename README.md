# Apple Ads Insights MCP

**Read-only, analysis-first [MCP](https://modelcontextprotocol.io) server for the Apple Ads Platform API.**
Ask your AI assistant about your App Store campaigns — structure, keywords,
search terms, reports — with a server that *cannot* modify anything.

Works with **any MCP client**: Claude, ChatGPT, Cursor, VS Code, Windsurf,
Gemini CLI, and anything else that speaks MCP — see
[docs/CONNECT.md](docs/CONNECT.md). Host it anywhere a container runs; a
step-by-step free-tier Google Cloud Run guide is included.

> **Unofficial community project.** Not affiliated with, endorsed, certified,
> or supported by Apple Inc. Apple, Apple Ads and App Store are trademarks of
> Apple Inc. You are responsible for your own compliance with the
> [Apple Ads Terms of Service](https://ads.apple.com/terms-of-service).

> **Status: 0.2.x — all 25 tools live-verified** against a real advertiser
> account on Cloud Run. Apple behaviours that differ from the docs (operator
> quirks, enum casing, genre tokens) are recorded in
> [docs/API_NOTES.md](docs/API_NOTES.md). Plan and rationale in
> [PLAN.md](PLAN.md).

## Why this exists

- **Permanently read-only.** Every outgoing API call must match a
  version-controlled allowlist derived from Apple's own SDK inventory (all
  99 operations are classified; 47 reads enabled, 36 writes denied, CI fails
  if a write becomes reachable). Apple's API uses POST for both reads
  (`/query`) and creates, so the loader refuses any enabled POST that is not
  a `/query`. There is no write mode to misconfigure.
- **Single advertiser per deployment.** You deploy it in *your own* Google
  Cloud project (or run it locally); your credentials never touch anyone
  else's infrastructure. A mandatory account allowlist blocks cross-account
  access, and the `X-AP-Context` header is derived only from it.
- **Built for analysis.** Reports at campaign, ad group, ad, keyword and
  search-term level with Apple's groupBy dimensions and granularities, plus
  safety ceilings so an AI client loop can't burn your rate limits or your
  wallet.
- **Personal-use cost profile.** Scale-to-zero Cloud Run, max 1 instance;
  normal personal use lands at ~$0/month.
- **Targets the current API.** Built for the Apple Ads Platform API
  (`api.ads.apple.com/v1`), not the Campaign Management API v5 that Apple
  sunsets on January 26, 2027.

## Tools (25)

**Structure**: `list_ad_accounts` · `list_campaigns` · `list_ad_groups` ·
`list_keywords` · `list_ads`

**Reporting**: `get_report` (levels: campaigns, adgroups, ads, keywords,
searchterms) · `get_daily_performance`

**Analysis** (computed locally, formulas returned with every derived
value): `compare_periods` · `rank_performance` · `analyze_trends` ·
`analyze_pacing` · `analyze_keywords` · `analyze_search_terms` ·
`get_account_history`

**Apple intelligence & diagnostics**: `diagnose_delivery` ·
`check_app_eligibility` · `get_impression_share` ·
`get_search_term_popularity` · `get_keyword_suggestions` ·
`get_recommendations` · `get_target_cpa_suggestion` · `search_apps` ·
`get_app_details` · `search_geo` · `get_supported_languages`

Prompts: `weekly_performance_review`, `diagnose_performance_drop`.
Resources: `apple-ads://report-fields`, `apple-ads://capabilities`,
`apple-ads://api-notes`.

Note: Apple Ads reporting has no conversion, trial or revenue metrics —
installs are the deepest in-platform outcome. Join with your MMP or
subscription data outside this server.

## Setup overview

1. **Create API credentials** — invite an API user with the **API Account
   Read Only** role, generate an EC key pair, upload the public key, and note
   the `clientId` / `teamId` / `keyId` Apple shows
   ([docs/AUTHENTICATION.md](docs/AUTHENTICATION.md), ~10 minutes).
2. **Find your ad account ID(s)** with `python3 scripts/verify_credentials.py`.
3. **Run it** (either way):
   - **Locally (stdio)** for desktop MCP clients:

     ```bash
     pip install .
     cp .env.example .env   # fill in values, then: set -a; source .env; set +a
     apple-ads-mcp
     ```

   - **Hosted** — any container platform works (the image is a plain
     Dockerfile). A complete free-tier walkthrough for **Google Cloud Run**
     is in [docs/DEPLOY_GCP.md](docs/DEPLOY_GCP.md) (~15 min). Hosted mode is
     required for chat apps like claude.ai and ChatGPT.

4. **Connect your AI tool** — per-client instructions in
   [docs/CONNECT.md](docs/CONNECT.md).

## Remote authentication (pick exactly one)

| Mode | Use when | How |
|---|---|---|
| `bearer` (default) | Your MCP client can send headers (Claude Code, Cursor, VS Code, Gemini CLI) | `Authorization: Bearer <MCP_ACCESS_TOKEN>` on `/mcp` |
| `secret_path` | Client only accepts a URL (claude.ai and ChatGPT custom connectors) | Endpoint served at `/<MCP_PATH_SECRET>/mcp`; `/mcp` returns 404 |

Secret-path mode treats the URL as the credential: it can appear in client
settings, browser history, and infrastructure logs. Generate it with
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, rotate it
periodically, and prefer bearer mode when possible.

## Safety & privacy properties

- Exact-match operation allowlist; structural rule that an enabled POST must
  be a `/query`; mutating paths (`/apply`, `/dismiss`, `/bulk-`, `/upload`)
  refused at load time even if the registry file is tampered with.
- Apple has no read-only OAuth scope, so the server checks the API user's
  roles at startup and warns on write-capable ones
  (`APPLE_ADS_REQUIRE_READONLY_ROLE=true` refuses to start). It cannot write
  either way.
- Mandatory `ALLOWED_ACCOUNT_IDS`; every tool call re-checks the account.
- Rate/loop safeguards: 60 tool calls per rolling hour, 20 upstream requests
  per call, duplicate-call suppression, 90-day report ceiling, bounded rows,
  pages, and response size; `RateLimit-*` headers honored.
- No database, no persistent cache, no payload logging. Report rows, entity
  names, search terms and key material never appear in logs.

**Data disclosure note:** this server returns your advertising metrics to the
MCP client you connect — typically a hosted AI assistant. Review your AI
provider's data handling and your own obligations under Apple's terms before
connecting a production account. Do not share one deployment across
unrelated advertisers, and do not use returned data to train models without
the necessary permissions.

## Why not Apple's official SDK?

Apple publishes an official Python client (`apple-ads-platform`). We use its
repository as the pinned source of truth for the operation inventory, but
not as a runtime dependency: it exposes every write operation, is
synchronous, and adds hundreds of generated modules to a credential-holding
service. The full reasoning is in
[docs/ADR-001-client-layer.md](docs/ADR-001-client-layer.md).

## Development

```bash
pip install -e ".[dev]"
python3 -m unittest discover tests       # invariants, units, mocked end-to-end
python3 scripts/check_api_drift.py       # inventory/registry drift gate
```

The operation inventory lives in `spec/operations.json`, extracted from
Apple's SDK at the commit in `spec/SDK_PIN`. Any inventory update requires
classifying changed operations in `scripts/generate_registry.py` and
regenerating the registry in the same commit — CI enforces this.

## Acknowledgments

No code was reused from other projects, but this server stands on prior art
worth crediting:

- [goncaloaguer/unofficial-reddit-ads-mcp](https://github.com/goncaloaguer/unofficial-reddit-ads-mcp)
  — the sibling project whose architecture, safety model and documentation
  this repository mirrors.
- [apple/apple-ads-platform-api-python](https://github.com/apple/apple-ads-platform-api-python)
  (MIT) — Apple's official client; its generated inventory is our drift oracle.
- [AppVisionOS/apple-search-ads-mcp](https://github.com/AppVisionOS/apple-search-ads-mcp),
  [gregtuc/asa-mcp](https://github.com/gregtuc/asa-mcp) and
  [crevas/Apple-Ads-CLI](https://github.com/crevas/Apple-Ads-CLI) — earlier
  open-source Apple Ads MCP/CLI tools (v5-era); their tool naming informed
  this design.
- Built with the official
  [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk).

## Dependencies

Four direct runtime dependencies, declared in `pyproject.toml`: `mcp`
(pinned `>=1.9,<2`; SDK 2.0 is API-incompatible), `httpx`, `pydantic`, and
`pyjwt[crypto]` (ES256 signing via `cryptography`). The small footprint is
deliberate — this server handles ad-account credentials, so every dependency
is attack surface.

## License

MIT — see [LICENSE](LICENSE).
