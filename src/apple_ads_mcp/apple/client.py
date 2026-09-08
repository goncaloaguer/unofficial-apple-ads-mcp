"""Policy-enforcing Apple Ads Platform API client.

Every request passes through the read-operation registry, the account guard
(``X-AP-Context`` is derived only from an allowlisted account resolved by the
tool layer), the per-call subrequest budget, and the rate-limit policy.
"""
from __future__ import annotations

import asyncio
from typing import Any

from apple_ads_mcp.apple import http_policy
from apple_ads_mcp.apple.query import set_offset
from apple_ads_mcp.auth.apple_oauth import TokenManager
from apple_ads_mcp.config import Settings
from apple_ads_mcp.policy import registry
from apple_ads_mcp.policy.limits import SubrequestBudget

SOURCE = "Apple Ads Platform API v1"


class AppleApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class AppleClient:
    def __init__(self, settings: Settings, tokens: TokenManager) -> None:
        self._settings = settings
        self._tokens = tokens
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._last_rate: http_policy.RateLimitState | None = None
        self._consecutive_429 = 0

    async def _http(self):
        import httpx

        async with self._lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._settings.api_base_url,
                    timeout=30,
                    follow_redirects=False,
                    trust_env=False,
                    headers={"User-Agent": self._settings.user_agent, "Accept": "application/json"},
                )
        return self._client

    @property
    def last_rate_limit(self) -> http_policy.RateLimitState | None:
        return self._last_rate

    async def request(
        self,
        method: str,
        path: str,
        *,
        budget: SubrequestBudget,
        account_id: str | None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One policy-checked upstream request with retries.

        ``account_id`` must already be allowlist-resolved by the caller; it is
        the sole source of the X-AP-Context header. Operations whose registry
        entry says the header is required refuse to run without it.
        """
        op = registry.authorize(method, path)
        if op.requires_context == "required" and not account_id:
            raise AppleApiError(0, f"{op.operation_id} requires an ad account context")
        if account_id is not None and account_id not in self._settings.allowed_account_ids:
            # Belt and braces: tools resolve accounts, but never trust a caller.
            raise PermissionError(f"account {account_id!r} is not allowlisted")
        budget.spend()
        client = await self._http()

        attempt = 0
        while True:
            attempt += 1
            if self._last_rate is not None:
                delay = http_policy.proactive_delay(self._last_rate)
                if delay:
                    await asyncio.sleep(delay)
            token = await self._tokens.get_token()
            headers = {"Authorization": f"Bearer {token}"}
            if account_id is not None and op.requires_context != "none":
                headers["X-AP-Context"] = f"adAccountId={account_id}"
            resp = await client.request(method, path, params=params, json=json_body, headers=headers)
            self._last_rate = http_policy.parse_rate_limit(dict(resp.headers))

            if 200 <= resp.status_code < 300:
                self._consecutive_429 = 0
                try:
                    return resp.json()
                except ValueError as exc:
                    raise AppleApiError(resp.status_code, "non-JSON response body") from exc
            if resp.status_code == 401 and attempt == 1:
                self._tokens.invalidate()
                continue
            if http_policy.should_retry(resp.status_code, attempt):
                if resp.status_code == 429:
                    self._consecutive_429 += 1
                    delay = http_policy.retry_delay(self._last_rate, self._consecutive_429)
                else:
                    delay = http_policy.backoff_delay(attempt)
                await asyncio.sleep(delay)
                continue
            detail = http_policy.summarize_error_body(resp.text or "")
            message = f"Apple Ads API returned HTTP {resp.status_code} for {method} {op.path_template}"
            if detail:
                message += f" — {detail}"
            raise AppleApiError(resp.status_code, message)

    async def paginate(
        self,
        method: str,
        path: str,
        *,
        budget: SubrequestBudget,
        account_id: str | None,
        json_body: dict[str, Any],
        max_pages: int | None = None,
        max_rows: int | None = None,
        result_key: str | None = None,
    ) -> tuple[list[dict], dict[str, Any]]:
        """Offset-paginate a ``/query`` endpoint with caps.

        The full body is re-sent on every page with only ``offset`` changed
        (and total-count flags switched off after the first page). Returns
        (rows, meta). ``result_key`` selects a nested list inside ``result``
        (e.g. ``rows`` for reports); otherwise ``result`` itself must be a list.
        """
        settings = self._settings
        max_pages = max_pages or settings.max_pages
        max_rows = max_rows or settings.max_report_rows
        page_size = int((json_body.get("pagination") or {}).get("pageSize") or 100)

        rows: list[dict] = []
        pages = 0
        truncated = False
        total_count: int | None = None
        offset = int((json_body.get("pagination") or {}).get("offset") or 0)
        summary: dict[str, Any] | None = None

        while True:
            body = set_offset(json_body, offset, first_page=(pages == 0))
            payload = await self.request(method, path, budget=budget, account_id=account_id, json_body=body)
            pages += 1
            result = payload.get("result")
            if result_key and isinstance(result, dict):
                page_rows = result.get(result_key) or []
                if summary is None and isinstance(result.get("summary"), dict):
                    summary = result["summary"]
            elif isinstance(result, list):
                page_rows = result
            elif isinstance(result, dict):
                page_rows = [result]
            else:
                page_rows = []
            rows.extend(page_rows)

            pagination = payload.get("pagination") or {}
            if total_count is None and isinstance(pagination.get("totalCount"), int) and pagination["totalCount"] > 0:
                total_count = pagination["totalCount"]

            if len(page_rows) < page_size:
                break  # short page: no more data
            offset += len(page_rows)
            if total_count is not None and offset >= total_count:
                break
            if len(rows) >= max_rows or pages >= max_pages:
                truncated = True
                break

        if len(rows) > max_rows:
            rows = rows[:max_rows]
            truncated = True

        meta = {
            "pages_fetched": pages,
            "rows_returned": len(rows),
            "total_count": total_count,
            "truncated": truncated,
            "next_page_available": truncated,
            "source": SOURCE,
        }
        if summary is not None:
            meta["summary"] = summary
        return rows, meta

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
