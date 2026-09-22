"""Unit tests: config, MCP auth, limits, http policy, query builders, normalization.
Stdlib-only (no network, no MCP SDK)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from apple_ads_mcp.apple import http_policy, normalize, query  # noqa: E402
from apple_ads_mcp.auth.mcp_bearer import check_request, mcp_mount_path  # noqa: E402
from apple_ads_mcp.config import ConfigError, load_settings  # noqa: E402
from apple_ads_mcp.policy.accounts import AccountNotAllowed, resolve_account, write_capable_roles  # noqa: E402
from apple_ads_mcp.policy.limits import (  # noqa: E402
    DuplicateSuppressor,
    LimitExceeded,
    RollingWindowLimiter,
    SubrequestBudget,
)

FAKE_PEM = (  # synthetic placeholder; config only checks the PEM envelope
    "-----BEGIN EC PRIVATE KEY-----\nSYNTHETIC/PLACEHOLDER/NOT/A/REAL/KEY\n-----END EC PRIVATE KEY-----\n"
)
BASE_ENV = {
    "APPLE_ADS_CLIENT_ID": "SEARCHADS.aeb3ef5f-0c5a-4f2a-99c8-fca83f25a9",
    "APPLE_ADS_TEAM_ID": "SEARCHADS.hgw3ef3p-0w7a-8a2n-77c8-scv83f25a7",
    "APPLE_ADS_KEY_ID": "a273d0d3-4d9e-458c-a173-0db8619ca7d7",
    "APPLE_ADS_PRIVATE_KEY": FAKE_PEM,
    "ALLOWED_ACCOUNT_IDS": "123456789",
}


def settings(**overrides):
    return load_settings({**BASE_ENV, **overrides})


class ConfigTests(unittest.TestCase):
    def test_minimal_stdio(self):
        s = settings()
        self.assertEqual(s.transport, "stdio")
        self.assertEqual(s.default_account_id, "123456789")
        self.assertTrue(s.apple_private_key_pem.startswith("-----BEGIN EC PRIVATE KEY-----"))

    def test_literal_backslash_n_pem_is_normalized(self):
        s = settings(APPLE_ADS_PRIVATE_KEY=FAKE_PEM.replace("\n", "\\n"))
        self.assertIn("\n", s.apple_private_key_pem)

    def test_missing_secrets(self):
        with self.assertRaises(ConfigError):
            load_settings({"ALLOWED_ACCOUNT_IDS": "1"})

    def test_bad_ids(self):
        with self.assertRaises(ConfigError):
            settings(APPLE_ADS_CLIENT_ID="not-an-id")
        with self.assertRaises(ConfigError):
            settings(ALLOWED_ACCOUNT_IDS="a2_abc")
        with self.assertRaises(ConfigError):
            settings(DEFAULT_ACCOUNT_ID="999")

    def test_both_key_sources_rejected(self):
        with self.assertRaises(ConfigError):
            settings(APPLE_ADS_PRIVATE_KEY_PATH="/nonexistent")

    def test_http_bearer_requires_token(self):
        with self.assertRaises(ConfigError):
            settings(MCP_TRANSPORT="http")
        s = settings(MCP_TRANSPORT="http", MCP_ACCESS_TOKEN="x" * 40)
        self.assertEqual(s.mcp_auth_mode, "bearer")
        self.assertIsNone(s.mcp_path_secret)

    def test_http_modes_mutually_exclusive(self):
        with self.assertRaises(ConfigError):
            settings(MCP_TRANSPORT="http", MCP_ACCESS_TOKEN="x" * 40, MCP_PATH_SECRET="y" * 43)

    def test_secret_path_entropy(self):
        with self.assertRaises(ConfigError):
            settings(MCP_TRANSPORT="http", MCP_AUTH_MODE="secret_path", MCP_PATH_SECRET="short")
        s = settings(MCP_TRANSPORT="http", MCP_AUTH_MODE="secret_path", MCP_PATH_SECRET="A" * 43)
        self.assertTrue(s.warnings)

    def test_readonly_role_flag(self):
        self.assertFalse(settings().require_readonly_role)
        self.assertTrue(settings(APPLE_ADS_REQUIRE_READONLY_ROLE="true").require_readonly_role)


class McpAuthTests(unittest.TestCase):
    def test_bearer(self):
        s = settings(MCP_TRANSPORT="http", MCP_ACCESS_TOKEN="t" * 40)
        self.assertEqual(mcp_mount_path(s), "/mcp")
        self.assertTrue(check_request(s, "/mcp", "Bearer " + "t" * 40).allowed)
        self.assertEqual(check_request(s, "/mcp", "Bearer wrong").status, 401)
        self.assertEqual(check_request(s, "/mcp", None).status, 401)
        self.assertEqual(check_request(s, "/other", "Bearer " + "t" * 40).status, 404)

    def test_secret_path(self):
        secret = "S" * 43
        s = settings(MCP_TRANSPORT="http", MCP_AUTH_MODE="secret_path", MCP_PATH_SECRET=secret)
        self.assertTrue(check_request(s, f"/{secret}/mcp", None).allowed)
        self.assertEqual(check_request(s, "/mcp", None).status, 404)
        self.assertEqual(check_request(s, f"/{secret[:-1]}/mcp", None).status, 404)
        self.assertEqual(check_request(s, f"/{secret}/mcp/extra", None).status, 404)


class AccountTests(unittest.TestCase):
    def test_resolve(self):
        s = settings(ALLOWED_ACCOUNT_IDS="1,2", DEFAULT_ACCOUNT_ID="2")
        self.assertEqual(resolve_account(s, None), "2")
        self.assertEqual(resolve_account(s, 1), "1")
        with self.assertRaises(AccountNotAllowed):
            resolve_account(s, "3")
        s2 = settings(ALLOWED_ACCOUNT_IDS="1,2")
        with self.assertRaises(AccountNotAllowed):
            resolve_account(s2, None)

    def test_roles(self):
        self.assertEqual(write_capable_roles(["API Account Read Only"]), [])
        self.assertEqual(write_capable_roles(["Admin", "API Account Read Only"]), ["Admin"])
        self.assertEqual(write_capable_roles(["Something New"]), ["Something New"])


class LimitTests(unittest.TestCase):
    def test_rolling(self):
        lim = RollingWindowLimiter(2, window_seconds=10)
        lim.acquire(now=0)
        lim.acquire(now=1)
        with self.assertRaises(LimitExceeded):
            lim.acquire(now=2)
        lim.acquire(now=11)

    def test_budget_and_duplicates(self):
        b = SubrequestBudget(2)
        b.spend()
        b.spend()
        with self.assertRaises(LimitExceeded):
            b.spend()
        d = DuplicateSuppressor(window_seconds=2)
        key = DuplicateSuppressor.key("t", {"a": 1})
        self.assertFalse(d.is_rapid_duplicate(key, now=0))
        self.assertTrue(d.is_rapid_duplicate(key, now=1))
        self.assertFalse(d.is_rapid_duplicate(key, now=5))


class HttpPolicyTests(unittest.TestCase):
    def test_rate_limit_parse(self):
        st = http_policy.parse_rate_limit(
            {"RateLimit-Limit": "100", "ratelimit-remaining": "2", "RateLimit-Reset": "37", "Retry-After": "5"}
        )
        self.assertEqual((st.limit, st.remaining, st.reset_seconds, st.retry_after), (100, 2, 37, 5))
        self.assertEqual(http_policy.proactive_delay(st), 16.0)  # capped at MAX_BACKOFF
        self.assertEqual(http_policy.retry_delay(st, 1), 5.0)
        empty = http_policy.parse_rate_limit({})
        self.assertEqual(http_policy.proactive_delay(empty), 0.0)
        self.assertEqual(http_policy.retry_delay(empty, 1), 2.0)
        self.assertEqual(http_policy.retry_delay(empty, 2), 4.0)
        self.assertEqual(http_policy.retry_delay(empty, 9), 16.0)

    def test_error_summary(self):
        body = '{"error":{"code":"VALIDATION_ERROR","message":"Validation errors found","details":[{"code":"DUPLICATE_NAME","message":"AdGroup name already exists"}]}}'
        self.assertEqual(
            http_policy.summarize_error_body(body),
            "VALIDATION_ERROR; Validation errors found; DUPLICATE_NAME: AdGroup name already exists",
        )
        self.assertIsNone(http_policy.summarize_error_body("not json"))
        self.assertIsNone(http_policy.summarize_error_body('{"result": {"secret": 1}}'))

    def test_retry_classes(self):
        self.assertTrue(http_policy.should_retry(429, 1))
        self.assertTrue(http_policy.should_retry(503, 3))
        self.assertFalse(http_policy.should_retry(503, 4))
        for status in (400, 401, 403, 404):
            self.assertFalse(http_policy.should_retry(status, 1))


class QueryBuilderTests(unittest.TestCase):
    def test_entity_query(self):
        body = query.entity_query(
            filters=[{"field": "name", "operator": "like", "value": "brand", "ignoreCase": True}],
            sorting=[{"field": "name"}], page_size=50, fetch_total_count=True,
        )
        self.assertEqual(body["pagination"], {"offset": 0, "pageSize": 50, "fetchTotalCount": True})
        self.assertEqual(body["filters"][0], {"field": "name", "operator": "LIKE", "value": "brand", "ignoreCase": True})
        self.assertEqual(body["sorting"], [{"field": "name", "order": "ASC"}])
        with self.assertRaises(ValueError):
            query.entity_query(filters=[{"field": "x", "operator": "CONTAINS", "value": 1}])

    def test_report_query_has_no_fetch_total_count(self):
        body = query.report_query(start="2026-08-01", end="2026-08-07", granularity="DAILY", group_by=["storefront"], include_rows=["GRAND_TOTAL"])
        self.assertEqual(body["timeRange"], {"start": "2026-08-01", "end": "2026-08-07", "timeZone": "ORTZ", "granularity": "DAILY"})
        self.assertNotIn("fetchTotalCount", body["pagination"])
        self.assertEqual(body["options"], {"includeRows": ["GRAND_TOTAL"]})

    def test_recommendation_values_are_arrays(self):
        body = query.recommendation_query(filters=[{"field": "promotedObjectId", "operator": "EQUALS", "value": 5}], page_size=5000)
        self.assertEqual(body["filters"][0]["value"], [5])
        self.assertEqual(body["pagination"]["pageSize"], 1000)

    def test_audit_query(self):
        body = query.audit_query(event_time_from="2026-08-01T00:00:00Z", event_time_to="2026-08-31T23:59:59Z", metadata="latest")
        self.assertEqual(body["filters"][0]["operator"], "BETWEEN")
        self.assertEqual(body["options"]["needTotals"], "true")
        second = query.set_offset(body, 100, first_page=False)
        self.assertEqual(second["options"]["needTotals"], "false")
        self.assertEqual(body["options"]["needTotals"], "true")  # original untouched

    def test_set_offset_entity(self):
        body = query.entity_query(fetch_total_count=True)
        page2 = query.set_offset(body, 100, first_page=False)
        self.assertEqual(page2["pagination"], {"offset": 100, "pageSize": 100, "fetchTotalCount": False})
        self.assertTrue(body["pagination"]["fetchTotalCount"])


class NormalizeTests(unittest.TestCase):
    def test_money(self):
        self.assertEqual(normalize.money({"amount": "12.34", "currency": "USD"}), {"amount": 12.34, "currency": "USD"})
        self.assertIsNone(normalize.money(None))
        self.assertEqual(normalize.money({"amount": "x", "currency": "USD"})["amount"], None)

    def test_value_wrapped_money(self):
        # Campaign dailyBudget arrives as {"value": {amount, currency}} (live).
        self.assertEqual(normalize.money({"value": {"amount": "100", "currency": "GBP"}}), {"amount": 100.0, "currency": "GBP"})
        e = normalize.normalize_entity({"dailyBudget": {"value": {"amount": "100", "currency": "GBP"}}, "name": "x"})
        self.assertEqual(e["dailyBudget"], {"amount": 100.0, "currency": "GBP"})

    def test_metrics(self):
        m = normalize.normalize_metrics({"localSpend": {"amount": "5.00", "currency": "EUR"}, "taps": 3, "ttr": 0.1})
        self.assertEqual(m, {"localSpend": 5.0, "currency": "EUR", "taps": 3, "ttr": 0.1})

    def test_safe_div(self):
        self.assertIsNone(normalize.safe_div(1, 0))
        self.assertIsNone(normalize.safe_div(None, 2))
        self.assertEqual(normalize.safe_div(1, 4), 0.25)


if __name__ == "__main__":
    unittest.main()
