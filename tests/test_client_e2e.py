"""Mocked end-to-end tests: token flow, headers, pagination, retries, tools.

Requires httpx and pyjwt (installed with the package); runs in CI after
``pip install .``. Uses httpx.MockTransport so nothing touches the network.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

try:
    import httpx
    import jwt  # noqa: F401
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("httpx/pyjwt not installed")

from apple_ads_mcp.apple.client import AppleApiError  # noqa: E402
from apple_ads_mcp.auth.apple_oauth import create_client_secret  # noqa: E402
from apple_ads_mcp.config import load_settings  # noqa: E402
from apple_ads_mcp.context import AppContext  # noqa: E402
from apple_ads_mcp.policy.limits import LimitExceeded, SubrequestBudget  # noqa: E402
from apple_ads_mcp.policy.registry import OperationDenied  # noqa: E402
from apple_ads_mcp.tools import insights, reporting_tools, structure  # noqa: E402


def _gen_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()


PEM = _gen_pem()
ENV = {
    "APPLE_ADS_CLIENT_ID": "SEARCHADS.11111111-2222-3333-4444-555555555555",
    "APPLE_ADS_TEAM_ID": "SEARCHADS.66666666-7777-8888-9999-000000000000",
    "APPLE_ADS_KEY_ID": "a273d0d3-4d9e-458c-a173-0db8619ca7d7",
    "APPLE_ADS_PRIVATE_KEY": PEM,
    "ALLOWED_ACCOUNT_IDS": "123456789",
}


class FakeApple:
    """Scriptable Apple API + token endpoint behind httpx.MockTransport."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.fail_first_with: int | None = None
        self.rate_headers = {"RateLimit-Limit": "100", "RateLimit-Remaining": "90", "RateLimit-Reset": "30"}
        self.campaigns = [
            {"id": i, "name": f"Camp {i}", "status": "ENABLED", "displayStatus": "RUNNING",
             "dailyBudget": {"amount": "50.00", "currency": "USD"}, "promotedObjectId": "999"}
            for i in range(1, 8)
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "appleid.apple.com":
            self.token_calls += 1
            body = dict(x.split("=") for x in request.content.decode().split("&"))
            assert body["grant_type"] == "client_credentials" and body["scope"] == "searchadsorg"
            return httpx.Response(200, json={"access_token": f"tok{self.token_calls}", "token_type": "Bearer",
                                             "expires_in": 3600, "scope": "searchadsorg"})
        assert url.host == "api.ads.apple.com"
        if self.fail_first_with is not None:
            status, self.fail_first_with = self.fail_first_with, None
            return httpx.Response(status, headers={**self.rate_headers, "Retry-After": "0"},
                                  json={"error": {"code": "x", "message": "transient"}})
        path = url.path
        if path == "/v1/acls":
            return httpx.Response(200, headers=self.rate_headers, json={"result": {"acls": [
                {"adAccount": {"id": 123456789, "name": "Example App Co.", "orgId": 1}, "roles": ["API Account Read Only"]},
                {"adAccount": {"id": 555, "name": "Other", "orgId": 1}, "roles": ["Admin"]},
            ]}})
        if path == "/v1/ad-accounts/123456789":
            assert request.headers["X-AP-Context"] == "adAccountId=123456789"
            return httpx.Response(200, headers=self.rate_headers, json={"result": {
                "id": 123456789, "name": "Example App Co.", "currency": "USD", "timezone": "Europe/Lisbon",
                "systemStatus": "ACTIVE", "productFeatures": ["APPSTORE_APP_MANUAL"]}})
        if path == "/v1/campaigns/query":
            body = json.loads(request.content)
            offset, size = body["pagination"]["offset"], body["pagination"]["pageSize"]
            page = self.campaigns[offset: offset + size]
            return httpx.Response(200, headers=self.rate_headers, json={
                "result": page, "pagination": {"offset": offset, "pageSize": size, "totalCount": len(self.campaigns)}})
        if path == "/v1/reports/apps/campaigns/query":
            body = json.loads(request.content)
            assert "fetchTotalCount" not in body["pagination"]
            gran = body["timeRange"].get("granularity")
            rows = []
            for c in self.campaigns[:2]:
                metrics = {"localSpend": {"amount": "10.00", "currency": "USD"}, "impressions": 1000, "taps": 50,
                           "totalInstalls": 5, "tapInstalls": 4, "viewInstalls": 1}
                row = {"metadata": {"id": c["id"], "name": c["name"], "displayStatus": "RUNNING"},
                       "totalMetrics": metrics}
                if gran:
                    row["granularMetrics"] = [{**metrics, "date": d} for d in ("2026-08-30", "2026-08-31")]
                rows.append(row)
            return httpx.Response(200, headers=self.rate_headers, json={
                "result": {"rows": rows, "summary": {"grandTotal": {"totalMetrics": {"taps": 100}}}},
                "pagination": {"offset": 0, "pageSize": 500, "totalCount": 2}})
        if path in ("/v1/keywords/query", "/v1/negative-keywords/query"):
            body = json.loads(request.content)
            filters = {f["field"]: f for f in body.get("filters", [])}
            for f in filters.values():
                if f["field"] in ("campaignId", "adGroupId") and f["operator"] != "EQUALS":
                    return httpx.Response(400, headers=self.rate_headers, json={"error": {
                        "code": "VALIDATION_ERROR", "details": [{"code": "INVALID_INPUT",
                        "message": f"{f['field']} condition must use EQUALS operator"}]}})
            if path.endswith("negative-keywords/query") and "adGroupId" not in filters:
                return httpx.Response(400, headers=self.rate_headers, json={"error": {
                    "code": "VALIDATION_ERROR", "details": [{"code": "INVALID_INPUT", "message": "adGroupId condition is required"}]}})
            if "adGroupId" in filters:
                gid = filters["adGroupId"]["value"]
                cid = gid // 100
            else:
                cid = filters["campaignId"]["value"]
                gid = 100 * cid
            neg = path.endswith("negative-keywords/query")
            rows = [{"id": (1000 if neg else 10) * cid + i, "campaignId": cid, "adGroupId": gid, "text": ("neg" if neg else "kw") + str(i) if i else ("neg0" if neg else "adhd"),
                     "matchType": "EXACT", "status": "ENABLED", "adAccountId": 123456789, "deleted": False,
                     **({} if neg else {"bid": {"amount": "2.5", "currency": "USD"}, "displayStatus": "AD_GROUP_ON_HOLD"})}
                    for i in range(2)]
            return httpx.Response(200, headers=self.rate_headers, json={
                "result": rows, "pagination": {"offset": 0, "pageSize": 1000, "totalCount": len(rows)}})
        if path == "/v1/campaigns/1":
            return httpx.Response(200, headers=self.rate_headers, json={"result": self.campaigns[0]})
        if path == "/v1/adgroups/query":
            body = json.loads(request.content)
            cid = next((f["value"] for f in body.get("filters", []) if f["field"] == "campaignId"), 1)
            cid = cid[0] if isinstance(cid, list) else cid
            rows = [{"id": 100 * cid, "campaignId": cid, "name": f"AG{cid}", "status": "ENABLED", "systemStatus": "RUNNING",
                     "displayStatus": "RUNNING", "systemStatusReasons": [], "bidStrategy": {"bid": {"amount": "2", "currency": "USD"}}}]
            return httpx.Response(200, headers=self.rate_headers, json={"result": rows, "pagination": {"offset": 0, "pageSize": 500, "totalCount": 1}})
        if path in ("/v1/reports/apps/keywords/query", "/v1/reports/apps/searchterms/query"):
            body = json.loads(request.content)
            cid = next((f["value"] for f in body.get("filters", []) if f["field"] == "campaignId"), None)
            if cid is None:
                return httpx.Response(400, headers=self.rate_headers, json={"error": {"code": "VALIDATION_ERROR", "details": [
                    {"code": "INVALID_VALUE_FIELD", "message": "campaignId filter is required for KEYWORD reports when promotedObjectType is APPS."}]}})
            m = lambda spend, imp, taps, inst: {"localSpend": {"amount": str(spend), "currency": "USD"}, "impressions": imp, "taps": taps,  # noqa: E731
                                                "tapInstalls": inst, "viewInstalls": 0, "totalInstalls": inst, "totalNewDownloads": inst, "totalRedownloads": 0}
            if path.endswith("keywords/query"):
                rows = [
                    {"metadata": {"id": 11, "text": "adhd", "campaignId": cid, "adGroupId": 100 * cid, "status": "ENABLED", "matchType": "EXACT", "bid": {"amount": "2.5", "currency": "USD"}}, "totalMetrics": m(40, 200, 20, 10), "insights": {"bidRecommendation": {}}},
                    {"metadata": {"id": 12, "text": "adhd test", "campaignId": cid, "adGroupId": 100 * cid, "status": "ENABLED", "matchType": "EXACT", "bid": {"amount": "2", "currency": "USD"}}, "totalMetrics": m(12, 50, 6, 0)},
                    {"metadata": {"id": 13, "text": "focus timer", "campaignId": cid, "adGroupId": 100 * cid, "status": "ENABLED", "matchType": "EXACT", "bid": {"amount": "1", "currency": "USD"}}, "totalMetrics": m(0, 2, 0, 0)},
                ]
            else:
                kw = {"id": 11, "text": "adhd", "matchType": "EXACT", "bid": {"amount": "2.5", "currency": "USD"}}
                rows = [
                    {"metadata": {"searchTermText": "adhd", "searchTermSource": "TARGETED", "campaignId": cid, "adGroupId": 100 * cid, "keyword": kw}, "totalMetrics": m(30, 150, 15, 8)},
                    {"metadata": {"searchTermText": "adhd planner", "searchTermSource": "AUTO", "campaignId": cid, "adGroupId": 100 * cid, "keyword": kw}, "totalMetrics": m(9, 40, 5, 2)},
                    {"metadata": {"searchTermText": "adhd meme", "searchTermSource": "AUTO", "campaignId": cid, "adGroupId": 100 * cid, "keyword": kw}, "totalMetrics": m(6, 30, 4, 0)},
                    {"metadata": {"searchTermText": None, "searchTermSource": "TARGETED", "campaignId": cid, "adGroupId": 100 * cid, "keyword": kw}, "totalMetrics": m(5, 20, 3, 1)},
                ]
            return httpx.Response(200, headers=self.rate_headers, json={"result": {"rows": rows}, "pagination": {"offset": 0, "pageSize": 500, "totalCount": len(rows)}})
        if path == "/v1/change-history/query":
            body = json.loads(request.content)
            assert body["filters"][0]["field"] == "eventTime" and body["options"]["needTotals"] in ("true", "false")
            rows = [{"transactionId": "t1", "eventType": "UPDATE", "eventTime": "2026-08-10T10:00:00Z", "entityType": "Campaign", "count": 1,
                     "metas": [{"entityId": 1, "detailId": "Campaign.1.t1"}], "userType": "CUSTOMER", "modifiedBy": 42},
                    {"transactionId": "t2", "eventType": "CREATE", "eventTime": "2026-08-11T10:00:00Z", "entityType": "Keyword", "count": 3, "metas": [], "userType": "CUSTOMER_API", "modifiedBy": 42}]
            return httpx.Response(200, headers=self.rate_headers, json={"result": rows, "pagination": {"offset": 0, "pageSize": 200, "totalCount": 2}})
        if path == "/v1/change-history/Campaign.1.t1":
            return httpx.Response(200, headers=self.rate_headers, json={"result": {"detailId": "Campaign.1.t1", "details": [{"changes": [{"field": "dailyBudget", "oldValues": ["50"], "newValues": ["100"]}]}]}})
        if path == "/v1/insights/apps/impression-share/query":
            body = json.loads(request.content)
            assert body["timeRange"]["timeZone"] == "UTC" and body["options"]["impressionShareReportType"] in ("FIRST_SLOT", "ALL_SLOTS")
            rows = [{"day": "2026-08-01", "promotedObjectId": "999", "countryOrRegion": "US", "searchTerm": "adhd", "lowImpressionShare": 0.4, "highImpressionShare": 0.4, "rank": 2, "searchPopularity1to5": 5}]
            return httpx.Response(200, headers=self.rate_headers, json={"result": {"rows": rows}, "pagination": {"offset": 0, "pageSize": 1000, "totalCount": 1}})
        if path == "/v1/insights/apps/search-term-popularity/query":
            rows = [{"week": "2026-08-02", "countryOrRegion": "US", "genre": "Health & Fitness", "searchTerm": "adhd", "rankInGenre": 3, "searchPopularity1to100": 80}]
            return httpx.Response(200, headers=self.rate_headers, json={"result": {"rows": rows}, "pagination": {"offset": 0, "pageSize": 1000, "totalCount": 1}})
        if path.startswith("/v1/suggestions/") or path.startswith("/v1/recommendations/"):
            body = json.loads(request.content)
            fields = {f["field"]: f["value"] for f in body["filters"]}
            assert isinstance(fields.get("promotedObjectId"), list)
            if path.endswith("keywords/query"):
                rows = [{"text": "adhd planner", "popularity": 70}, {"text": "focus app", "popularity": 90}]
            elif path.endswith("phrases/query"):
                rows = [{"phrase": "adhd tools", "popularity": 50}]
            elif path.endswith("target-cpas/query") and "suggestions" in path:
                rows = [{"promotedObjectId": "999", "countryOrRegion": "US", "suggestedTargetCPA": {"amount": "3.5", "currency": "USD"}}]
            else:
                rows = [{"id": 5, "campaignId": int(fields["promotedObjectId"][0]), "suggestedDailyBudgetAmount": {"amount": "150", "currency": "USD"}, "expectedInstalls": 40}]
            return httpx.Response(200, headers=self.rate_headers, json={"result": rows, "pagination": {"offset": 0, "pageSize": 100, "totalCount": len(rows)}})
        if path == "/v1/search/apps":
            assert "query" in request.url.params or request.url.params.get("returnOwnedApps") == "true"
            return httpx.Response(200, headers=self.rate_headers, json={"result": [{"adamId": 999, "appName": "Example App"}]})
        if path == "/v1/apps/999":
            return httpx.Response(200, headers=self.rate_headers, json={"result": {"id": 999, "appName": "Example App", "availableStorefronts": ["US", "GB"]}})
        if path == "/v1/product-pages/query":
            return httpx.Response(200, headers=self.rate_headers, json={"result": [], "pagination": {"offset": 0, "pageSize": 100, "totalCount": 0}})
        if path == "/v1/search/geo":
            if request.method == "GET":
                assert request.url.params["supplySource"] == "APPSTORE"
            else:
                assert json.loads(request.content)["supplySource"] == "APPSTORE"
            return httpx.Response(200, headers=self.rate_headers, json={"result": [{"id": 2018, "entity": "ADMIN_AREA", "displayName": "California"}]})
        if path == "/v1/eligibilities/apps/query":
            return httpx.Response(200, headers=self.rate_headers, json={"result": [{"adamId": 999, "countryOrRegion": "US", "state": "ELIGIBLE"}], "pagination": {"offset": 0, "pageSize": 500, "totalCount": 1}})
        if path == "/v1/metadata/apps/supported-languages/query":
            return httpx.Response(200, headers=self.rate_headers, json={"result": [{"code": "en-US"}], "pagination": {"offset": 0, "pageSize": 500, "totalCount": 1}})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = load_settings(ENV)
        self.fake = FakeApple()
        self.ctx = AppContext.create(self.settings)
        transport = httpx.MockTransport(self.fake.handler)
        # Inject the mock transport into both the API client and token fetches.
        self.ctx.client._client = httpx.AsyncClient(base_url=self.settings.api_base_url, transport=transport)
        tokens = self.ctx.client._tokens

        async def fetch():
            async with httpx.AsyncClient(transport=transport) as c:
                resp = await c.post(self.settings.token_url, data={
                    "grant_type": "client_credentials", "client_id": self.settings.apple_client_id,
                    "client_secret": create_client_secret(self.settings), "scope": "searchadsorg"})
                return resp.json()

        tokens._fetch = fetch  # type: ignore[attr-defined]

    async def test_client_secret_is_valid_es256(self):
        import jwt as pyjwt

        secret = create_client_secret(self.settings)
        header = pyjwt.get_unverified_header(secret)
        claims = pyjwt.decode(secret, options={"verify_signature": False}, audience="https://appleid.apple.com")
        self.assertEqual(header["alg"], "ES256")
        self.assertEqual(header["kid"], self.settings.apple_key_id)
        self.assertEqual(claims["iss"], self.settings.apple_team_id)
        self.assertEqual(claims["sub"], self.settings.apple_client_id)
        self.assertLessEqual(claims["exp"] - claims["iat"], 86400 * 180)

    async def test_headers_and_token_reuse(self):
        budget = SubrequestBudget(10)
        await self.ctx.client.request("GET", "/v1/campaigns/1", budget=budget, account_id="123456789")
        await self.ctx.client.request("GET", "/v1/campaigns/1", budget=budget, account_id="123456789")
        self.assertEqual(self.fake.token_calls, 1)
        req = self.fake.requests[-1]
        self.assertEqual(req.headers["Authorization"], "Bearer tok1")
        self.assertEqual(req.headers["X-AP-Context"], "adAccountId=123456789")
        self.assertEqual(self.ctx.client.last_rate_limit.remaining, 90)

    async def test_context_never_from_unallowlisted_account(self):
        with self.assertRaises(PermissionError):
            await self.ctx.client.request("GET", "/v1/campaigns/1", budget=SubrequestBudget(5), account_id="555")
        with self.assertRaises(AppleApiError):
            await self.ctx.client.request("GET", "/v1/campaigns/1", budget=SubrequestBudget(5), account_id=None)

    async def test_denied_operation_never_reaches_network(self):
        with self.assertRaises(OperationDenied):
            await self.ctx.client.request("POST", "/v1/campaigns", budget=SubrequestBudget(5), account_id="123456789", json_body={})
        self.assertEqual(self.fake.requests, [])

    async def test_401_refreshes_token_once(self):
        self.fake.fail_first_with = 401
        await self.ctx.client.request("GET", "/v1/campaigns/1", budget=SubrequestBudget(5), account_id="123456789")
        self.assertEqual(self.fake.token_calls, 2)

    async def test_429_then_success(self):
        self.fake.fail_first_with = 429
        payload = await self.ctx.client.request("GET", "/v1/campaigns/1", budget=SubrequestBudget(5), account_id="123456789")
        self.assertEqual(payload["result"]["id"], 1)

    async def test_400_surfaces_error_code(self):
        self.fake.fail_first_with = 400
        with self.assertRaises(AppleApiError) as cm:
            await self.ctx.client.request("GET", "/v1/campaigns/1", budget=SubrequestBudget(5), account_id="123456789")
        self.assertIn("x; transient", str(cm.exception))

    async def test_offset_pagination_and_caps(self):
        from apple_ads_mcp.apple.query import entity_query

        body = entity_query(page_size=3, fetch_total_count=True)
        rows, meta = await self.ctx.client.paginate("POST", "/v1/campaigns/query", budget=SubrequestBudget(10),
                                                    account_id="123456789", json_body=body)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(meta["pages_fetched"], 3)
        self.assertFalse(meta["truncated"])
        bodies = [json.loads(r.content) for r in self.fake.requests if r.url.path == "/v1/campaigns/query"]
        self.assertEqual([b["pagination"]["offset"] for b in bodies], [0, 3, 6])
        self.assertEqual([b["pagination"]["fetchTotalCount"] for b in bodies], [True, False, False])
        rows, meta = await self.ctx.client.paginate("POST", "/v1/campaigns/query", budget=SubrequestBudget(10),
                                                    account_id="123456789", json_body=body, max_pages=1)
        self.assertEqual(len(rows), 3)
        self.assertTrue(meta["truncated"])

    async def test_subrequest_budget_enforced(self):
        from apple_ads_mcp.apple.query import entity_query

        with self.assertRaises(LimitExceeded):
            await self.ctx.client.paginate("POST", "/v1/campaigns/query", budget=SubrequestBudget(1),
                                           account_id="123456789", json_body=entity_query(page_size=2))


class ToolTests(ClientTests):
    async def test_list_ad_accounts_filters_and_flags_roles(self):
        out = await structure.list_ad_accounts(self.ctx)
        self.assertEqual([a["id"] for a in out["data"]], [123456789])
        self.assertEqual(out["data"][0]["roles"], ["API Account Read Only"])
        self.assertEqual(out["warnings"], [])
        self.assertNotIn("555", json.dumps(out))

    async def test_list_campaigns(self):
        out = await structure.list_campaigns(self.ctx, status="enabled", name_contains="camp")
        self.assertEqual(len(out["data"]), 7)
        self.assertEqual(out["data"][0]["dailyBudget"], {"amount": 50.0, "currency": "USD"})
        body = json.loads(self.fake.requests[-1].content)
        self.assertIn({"field": "name", "operator": "LIKE", "value": "camp", "ignoreCase": True}, body["filters"])
        self.assertEqual(out["summary"]["by_display_status"], {"RUNNING": 7})

    async def test_get_report_flatten_totals(self):
        out = await reporting_tools.get_report(self.ctx, level="campaigns", start="2026-08-01", end="2026-08-31",
                                               include_grand_total=True)
        self.assertEqual(len(out["data"]), 2)
        row = out["data"][0]
        self.assertEqual(row["localSpend"], 10.0)
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["name"], "Camp 1")
        self.assertEqual(out["summary"]["grand_total"], {"taps": 100})
        self.assertEqual(out["meta"]["source"], "Apple Ads Platform API v1")

    async def test_get_daily_performance(self):
        out = await reporting_tools.get_daily_performance(self.ctx, days=3)
        self.assertEqual([d["date"] for d in out["data"]], ["2026-08-30", "2026-08-31"])
        day = out["data"][0]
        self.assertEqual(day["localSpend"], 20.0)  # two campaigns × 10
        self.assertEqual(day["cpt_derived"], 20.0 / 100)
        self.assertEqual(out["summary"]["totals"]["totalInstalls"], 20)

    async def test_list_keywords_fans_out_equals_per_campaign(self):
        out = await structure.list_keywords(self.ctx, campaign_ids=["1", "2"], include_negative=True)
        positives = [k for k in out["data"] if not k.get("negative")]
        negatives = [k for k in out["data"] if k.get("negative")]
        self.assertEqual([k["id"] for k in positives], [10, 11, 20, 21])
        self.assertEqual(positives[0]["bid"], 2.5)
        self.assertEqual(positives[0]["displayStatus"], "AD_GROUP_ON_HOLD")
        self.assertNotIn("adAccountId", positives[0])  # compacted
        self.assertEqual(out["summary"]["currency"], "USD")
        self.assertEqual(out["summary"]["negative_keywords"], 4)
        self.assertEqual({n["adGroupId"] for n in negatives}, {100, 200})
        kw_bodies = [json.loads(r.content) for r in self.fake.requests if r.url.path == "/v1/keywords/query"]
        self.assertEqual([b["filters"][0]["operator"] for b in kw_bodies], ["EQUALS", "EQUALS"])
        neg_bodies = [json.loads(r.content) for r in self.fake.requests if r.url.path == "/v1/negative-keywords/query"]
        self.assertTrue(all(b["filters"][0]["field"] == "adGroupId" for b in neg_bodies))

    async def test_list_keywords_requires_scope_and_limits(self):
        with self.assertRaises(ValueError):
            await structure.list_keywords(self.ctx)
        out = await structure.list_keywords(self.ctx, campaign_ids=["1"], limit=1)
        self.assertEqual(len(out["data"]), 1)
        self.assertTrue(out["meta"]["truncated"])
        self.assertTrue(any("returning the first 1" in w for w in out["warnings"]))

    async def test_display_status_filtered_locally(self):
        self.fake.campaigns[0]["displayStatus"] = "ON_HOLD"
        out = await structure.list_campaigns(self.ctx, display_status="on_hold")
        self.assertEqual([c["id"] for c in out["data"]], [1])
        sent = json.loads(self.fake.requests[-1].content)
        self.assertNotIn("displayStatus", json.dumps(sent.get("filters", [])))

    async def test_list_campaigns_compacts_and_verbose_keeps_raw(self):
        self.fake.campaigns[0]["regulationResponses"] = [{"regulationType": "X", "responseValue": "NOT_ANSWERED"}]
        self.fake.campaigns[0]["targeting"] = {"countryOrRegion": {"include": ["US"]}, "supplyPlacement": {"include": ["APPSTORE_SEARCH_RESULTS"]}}
        out = await structure.list_campaigns(self.ctx)
        c = out["data"][0]
        self.assertNotIn("regulationResponses", c)
        self.assertEqual(c["targeting"], {"countryOrRegion": ["US"], "supplyPlacement": ["APPSTORE_SEARCH_RESULTS"]})
        out = await structure.list_campaigns(self.ctx, verbose=True)
        self.assertIn("regulationResponses", out["data"][0])

    async def test_report_fields_applied_client_side(self):
        out = await reporting_tools.get_report(self.ctx, level="campaigns", start="2026-08-01", end="2026-08-31",
                                               fields=["taps"])
        row = out["data"][0]
        self.assertEqual(row["name"], "Camp 1")  # metadata intact
        self.assertIn("taps", row)
        self.assertNotIn("localSpend", row)
        sent = json.loads(self.fake.requests[-1].content)
        self.assertNotIn("fields", sent)

    async def test_duplicate_call_suppressed(self):
        await structure.list_campaigns(self.ctx)
        with self.assertRaises(LimitExceeded):
            await structure.list_campaigns(self.ctx)


class AnalysisToolTests(ClientTests):
    async def test_compare_periods_account_and_campaigns(self):
        from apple_ads_mcp.tools import analysis_tools

        out = await analysis_tools.compare_periods(self.ctx, "2026-08-01", "2026-08-07", "2026-08-08", "2026-08-14")
        row = {c["metric"]: c for c in out["data"][0]["comparison"]}
        self.assertEqual(row["localSpend"]["period_a"], 20.0)
        self.assertEqual(row["localSpend"]["delta"], 0.0)
        self.assertEqual(row["cpi"]["period_b"], 2.0)
        out = await analysis_tools.compare_periods(self.ctx, "2026-08-01", "2026-08-07", "2026-08-08", "2026-08-14", level="campaigns")
        self.assertEqual({d["id"] for d in out["data"]}, {1, 2})

    async def test_rank_by_cost_metric_ascending(self):
        from apple_ads_mcp.tools import analysis_tools

        out = await analysis_tools.rank_performance(self.ctx, "2026-08-01", "2026-08-07", level="keywords", metric="cpi", campaign_ids=["1"])
        self.assertEqual([r["id"] for r in out["data"]], [11])  # only the keyword with installs has a cpi
        out = await analysis_tools.rank_performance(self.ctx, "2026-08-01", "2026-08-07", level="keywords", metric="localSpend", campaign_ids=["1"], min_spend=1)
        self.assertEqual([r["id"] for r in out["data"]], [12, 11])  # ascending cost
        with self.assertRaises(LimitExceeded):
            await analysis_tools.rank_performance(self.ctx, "2026-08-01", "2026-08-07", level="keywords", metric="cpi")

    async def test_trends_and_pacing(self):
        from apple_ads_mcp.tools import analysis_tools

        out = await analysis_tools.analyze_trends(self.ctx, "2026-08-25", "2026-09-01", metric="taps")
        self.assertEqual([p["date"] for p in out["data"][0]["series"]], ["2026-08-30", "2026-08-31"])
        self.assertEqual(out["data"][0]["series"][0]["taps"], 100)
        out = await analysis_tools.analyze_pacing(self.ctx, "2026-08-01", "2026-08-10")
        row = next(d for d in out["data"] if d["id"] == 1)
        self.assertEqual(row["expected_spend"], 500.0)  # 50 x 10 days
        self.assertEqual(row["flag"], "under_delivering")

    async def test_analyze_keywords_flags(self):
        from apple_ads_mcp.tools import analysis_tools

        out = await analysis_tools.analyze_keywords(self.ctx, ["1"], "2026-08-01", "2026-08-07")
        by_id = {k["id"]: k for k in out["data"]}
        self.assertIn("spend_no_installs", by_id[12]["flags"])
        self.assertIn("impression_starved", by_id[13]["flags"])
        self.assertEqual(by_id[11]["cpi"], 4.0)
        self.assertEqual(by_id[11]["cpt_vs_bid"], 0.8)
        self.assertEqual(out["summary"]["best_cpi"][0]["id"], 11)

    async def test_analyze_search_terms_classification(self):
        from apple_ads_mcp.tools import analysis_tools

        out = await analysis_tools.analyze_search_terms(self.ctx, ["1"], "2026-08-01", "2026-08-07")
        exp = [t["searchTermText"] for t in out["data"]["expansion_candidates"]]
        neg = [t["searchTermText"] for t in out["data"]["negative_candidates"]]
        self.assertEqual(exp, ["adhd planner"])  # has installs and is not an exact keyword
        self.assertEqual(neg, ["adhd meme"])
        self.assertEqual(out["summary"]["low_volume_aggregate"]["installs"], 1)
        self.assertTrue(next(t for t in out["data"]["top_terms_by_installs"] if t["searchTermText"] == "adhd")["is_exact_keyword"])

    async def test_account_history_with_details(self):
        from apple_ads_mcp.tools import analysis_tools

        out = await analysis_tools.get_account_history(self.ctx, "2026-08-01", "2026-08-31", event_types=["UPDATE"], include_details=True)
        self.assertEqual(len(out["data"]["changes"]), 1)
        self.assertEqual(out["data"]["details"][0]["details"][0]["changes"][0]["field"], "dailyBudget")
        with self.assertRaises(LimitExceeded):
            await analysis_tools.get_account_history(self.ctx, "2026-01-01", "2026-08-31")


class InsightToolTests(ClientTests):
    async def test_diagnose_delivery(self):
        self.fake.campaigns[1].update({"systemStatus": "NOT_RUNNING", "displayStatus": "ON_HOLD", "systemStatusReasons": ["PAYMENT_MODEL_NOT_SET"]})
        out = await insights.diagnose_delivery(self.ctx, campaign_ids=["1", "2"])
        issues = {(f["level"], f["id"]): f for f in out["data"]}
        self.assertIn(("campaign", 2), issues)
        self.assertEqual(issues[("campaign", 2)]["reasons"], ["PAYMENT_MODEL_NOT_SET"])
        self.assertEqual(out["summary"]["account_status"], "ACTIVE")

    async def test_impression_share_windows(self):
        out = await insights.get_impression_share(self.ctx, "999", "2026-08-01", "2026-08-07")
        self.assertEqual(out["data"][0]["searchTerm"], "adhd")
        with self.assertRaises(LimitExceeded):
            await insights.get_impression_share(self.ctx, "999", "2026-07-01", "2026-08-07")
        with self.assertRaises(LimitExceeded):  # not a Sunday
            await insights.get_impression_share(self.ctx, "999", "2026-08-03", "2026-08-16", granularity="WEEKLY_SUN_SAT")
        await insights.get_impression_share(self.ctx, "999", "2026-08-02", "2026-08-15", granularity="WEEKLY_SUN_SAT")

    async def test_popularity_suggestions_recommendations(self):
        out = await insights.get_search_term_popularity(self.ctx, ["us"], "2026-08-02", "2026-08-08")
        self.assertEqual(out["data"][0]["rankInGenre"], 3)
        out = await insights.get_keyword_suggestions(self.ctx, "999", terms=["adhd"], countries=["US"])
        self.assertEqual([k["text"] for k in out["data"]["keywords"]], ["focus app", "adhd planner"])
        self.assertEqual(out["data"]["phrases"][0]["phrase"], "adhd tools")
        out = await insights.get_recommendations(self.ctx, ["1"])
        self.assertEqual(out["data"]["daily_budget"][0]["suggestedDailyBudgetAmount"], 150.0)
        out = await insights.get_target_cpa_suggestion(self.ctx, "999", ["US"])
        self.assertEqual(out["data"][0]["suggestedTargetCPA"], 3.5)

    async def test_lookups(self):
        out = await insights.search_apps(self.ctx, query="example")
        self.assertEqual(out["data"][0]["adamId"], 999)
        with self.assertRaises(LimitExceeded):
            await insights.search_apps(self.ctx, query="ab")
        out = await insights.get_app_details(self.ctx, "999")
        self.assertEqual(out["summary"]["storefronts"], 2)
        out = await insights.search_geo(self.ctx, ids=["2018"])
        self.assertEqual(out["data"][0]["displayName"], "California")
        out = await insights.search_geo(self.ctx, query="Calif", entity="admin_area", country_code="us")
        self.assertEqual(len(out["data"]), 1)
        out = await insights.check_app_eligibility(self.ctx, "999", ["US"])
        self.assertEqual(out["summary"]["by_state"], {"ELIGIBLE": 1})
        out = await insights.get_supported_languages(self.ctx, "US")
        self.assertEqual(out["data"][0]["code"], "en-US")

    async def test_recommendation_endpoints_never_apply(self):
        await insights.get_recommendations(self.ctx, ["1", "2"])
        for r in self.fake.requests:
            self.assertNotIn("/apply", r.url.path)
            self.assertNotIn("/dismiss", r.url.path)


class StartupSequenceTests(unittest.TestCase):
    """main() runs the role check in one asyncio.run loop and serves in another.

    Regression for the live 2026-09-22 failure: reusing the probe context
    across loops raised "Event loop is closed" on the first tool call.
    """

    def _ctx_with_fake(self, settings, fake):

        ctx = AppContext.create(settings)
        transport = httpx.MockTransport(fake.handler)
        ctx.client._client = httpx.AsyncClient(base_url=settings.api_base_url, transport=transport)

        async def fetch():
            async with httpx.AsyncClient(transport=transport) as c:
                resp = await c.post(settings.token_url, data={
                    "grant_type": "client_credentials", "client_id": settings.apple_client_id,
                    "client_secret": create_client_secret(settings), "scope": "searchadsorg"})
                return resp.json()

        ctx.client._tokens._fetch = fetch  # type: ignore[attr-defined]
        return ctx

    def test_probe_then_fresh_context_across_loops(self):
        import asyncio

        from apple_ads_mcp.app import run_startup_check

        settings = load_settings(ENV)
        fake = FakeApple()
        probe = self._ctx_with_fake(settings, fake)
        warnings = asyncio.run(run_startup_check(settings, probe))
        self.assertEqual(warnings, [])  # fake ACL grants the read-only role
        self.assertTrue(probe.client._client.is_closed)

        serving = self._ctx_with_fake(settings, fake)  # what main() builds after the probe
        out = asyncio.run(structure.list_ad_accounts(serving))
        self.assertEqual([a["id"] for a in out["data"]], [123456789])

    def test_probe_warns_on_write_capable_role(self):
        import asyncio

        from apple_ads_mcp.app import run_startup_check

        settings = load_settings(ENV)
        fake = FakeApple()
        original = fake.handler

        def handler(request):
            resp = original(request)
            if request.url.path == "/v1/acls":
                body = resp.json()
                body["result"]["acls"][0]["roles"] = ["API Campaign Manager"]
                return httpx.Response(200, headers=fake.rate_headers, json=body)
            return resp

        fake.handler = handler
        probe = self._ctx_with_fake(settings, fake)
        warnings = asyncio.run(run_startup_check(settings, probe))
        self.assertEqual(len(warnings), 1)
        self.assertIn("API Campaign Manager", warnings[0])


if __name__ == "__main__":
    unittest.main()
