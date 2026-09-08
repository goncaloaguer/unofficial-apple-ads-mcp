"""Read-only invariants (PLAN.md §14.2). Stdlib-only: runs before any
dependency is installed so a supply-chain issue can never mask a regression.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from apple_ads_mcp.policy import registry  # noqa: E402

SPEC = json.loads((ROOT / "spec" / "operations.json").read_text())
REGISTRY = json.loads((ROOT / "src" / "apple_ads_mcp" / "policy" / "read_operations.json").read_text())
SRC = ROOT / "src" / "apple_ads_mcp"

WRITE_METHODS = {"PUT", "PATCH", "DELETE"}
MUTATING = ("/apply", "/dismiss", "/bulk-", "/upload")


class RegistryInvariants(unittest.TestCase):
    def test_every_spec_operation_is_classified(self):
        spec_keys = {(o["method"], o["path"]) for o in SPEC["operations"]}
        reg_keys = {(o["method"], o["path"]) for o in REGISTRY["operations"]}
        self.assertEqual(spec_keys, reg_keys)
        self.assertEqual(len(SPEC["operations"]), 99)

    def test_counts(self):
        self.assertEqual(REGISTRY["counts"], {"enabled": 47, "disabled": 16, "denied": 36})

    def test_no_write_method_enabled_or_disabled(self):
        for op in REGISTRY["operations"]:
            if op["classification"] != "denied":
                self.assertNotIn(op["method"], WRITE_METHODS, op)

    def test_every_non_denied_post_is_query_or_geo_lookup(self):
        for op in REGISTRY["operations"]:
            if op["classification"] != "denied" and op["method"] == "POST":
                self.assertTrue(
                    op["path"].endswith("/query") or op["path"] == "/v1/search/geo", op
                )

    def test_mutating_markers_denied(self):
        for op in REGISTRY["operations"]:
            if any(m in op["path"] for m in MUTATING):
                self.assertEqual(op["classification"], "denied", op)

    def test_every_create_update_delete_denied(self):
        creates = {
            "/v1/ad-accounts", "/v1/campaigns", "/v1/adgroups", "/v1/ads", "/v1/keywords",
            "/v1/negative-keywords", "/v1/creatives", "/v1/shared-budgets", "/v1/location-groups",
        }
        for op in REGISTRY["operations"]:
            if op["method"] == "POST" and op["path"] in creates:
                self.assertEqual(op["classification"], "denied", op)
        denied = {(o["method"], o["path"]) for o in REGISTRY["operations"] if o["classification"] == "denied"}
        for must in [
            ("POST", "/v1/recommendations/daily-budgets/apply"),
            ("POST", "/v1/recommendations/target-cpas/dismiss"),
            ("POST", "/v1/keywords/bulk-create"),
            ("POST", "/v1/assets/upload"),
            ("PUT", "/v1/campaigns/{id}"),
            ("DELETE", "/v1/keywords/{id}"),
        ]:
            self.assertIn(must, denied)

    def test_loaded_registry_matches_enabled_entries(self):
        loaded = {(o.method, o.path_template) for o in registry.load_registry()}
        enabled = {(o["method"], o["path"]) for o in REGISTRY["operations"] if o["classification"] == "enabled"}
        self.assertEqual(loaded, enabled)
        self.assertEqual(len(loaded), 47)


class LoaderStructuralRule(unittest.TestCase):
    def _raw_with(self, method, path):
        return {"operations": [{"operation_id": "x", "method": method, "path": path, "classification": "enabled"}]}

    def test_tampered_create_refused(self):
        with self.assertRaises(registry.OperationDenied):
            registry._build(self._raw_with("POST", "/v1/campaigns"))

    def test_tampered_put_refused(self):
        with self.assertRaises(registry.OperationDenied):
            registry._build(self._raw_with("PUT", "/v1/campaigns/{id}"))

    def test_tampered_apply_refused_even_with_query_suffix_lookalike(self):
        with self.assertRaises(registry.OperationDenied):
            registry._build(self._raw_with("POST", "/v1/recommendations/daily-budgets/apply"))
        with self.assertRaises(registry.OperationDenied):
            registry._build(self._raw_with("POST", "/v1/keywords/bulk-create/query"))

    def test_query_and_geo_accepted(self):
        self.assertEqual(len(registry._build(self._raw_with("POST", "/v1/campaigns/query"))), 1)
        self.assertEqual(len(registry._build(self._raw_with("POST", "/v1/search/geo"))), 1)
        self.assertEqual(len(registry._build(self._raw_with("GET", "/v1/campaigns/{id}"))), 1)


class Authorize(unittest.TestCase):
    def test_denied_paths_raise(self):
        for method, path in [
            ("POST", "/v1/campaigns"),
            ("PUT", "/v1/campaigns/123"),
            ("DELETE", "/v1/keywords/5"),
            ("POST", "/v1/keywords/bulk-create"),
            ("POST", "/v1/recommendations/daily-budgets/apply"),
            ("POST", "/v1/business-brands/query"),  # disabled (Maps)
            ("GET", "/v1/campaigns/123/../../anything"),
            ("POST", "/v1/campaigns/query?x=1"),
        ]:
            with self.assertRaises(registry.OperationDenied, msg=f"{method} {path}"):
                registry.authorize(method, path)

    def test_enabled_paths_match(self):
        self.assertEqual(registry.authorize("POST", "/v1/campaigns/query").operation_id, "campaigns_query_post")
        self.assertEqual(registry.authorize("GET", "/v1/campaigns/123").operation_id, "campaigns_id_get")
        self.assertEqual(registry.authorize("GET", "/v1/change-history/Campaign.1.2").operation_id, "get_change_details")
        self.assertEqual(registry.authorize("get", "/v1/me/").operation_id, "get_me")
        self.assertEqual(registry.authorize("GET", "/v1/me").requires_context, "none")
        self.assertEqual(registry.authorize("POST", "/v1/reports/apps/keywords/query").requires_context, "required")


class SourceScan(unittest.TestCase):
    """No denied path literal may appear in application code outside the registry."""

    def test_no_denied_path_literals_in_source(self):
        denied_paths = {
            o["path"] for o in REGISTRY["operations"] if o["classification"] == "denied"
        }
        # Paths that are pure prefixes of enabled reads (e.g. /v1/campaigns) are
        # allowed to appear only as part of a longer enabled path literal.
        enabled_paths = {o["path"] for o in REGISTRY["operations"] if o["classification"] != "denied"}
        offenders = []
        for py in SRC.rglob("*.py"):
            text = py.read_text()
            for literal in re.findall(r'"(/v1/[^"]+)"', text) + re.findall(r"'(/v1/[^']+)'", text):
                if literal in denied_paths and literal not in enabled_paths:
                    offenders.append((py.name, literal))
        self.assertEqual(offenders, [])

    def test_no_mutating_markers_in_source(self):
        for py in SRC.rglob("*.py"):
            if py.name in ("registry.py",):
                continue
            text = py.read_text()
            for marker in ("/bulk-", "/upload", "/apply", "/dismiss"):
                self.assertNotIn(f'"{marker}', text, f"{py.name} references {marker}")

    def test_client_only_issues_get_and_post(self):
        text = (SRC / "apple" / "client.py").read_text()
        for verb in ('"PUT"', '"DELETE"', '"PATCH"', "'PUT'", "'DELETE'", "'PATCH'"):
            self.assertNotIn(verb, text)


class OAuthInvariants(unittest.TestCase):
    def test_scope_constant(self):
        from apple_ads_mcp.config import REQUIRED_APPLE_SCOPE

        self.assertEqual(REQUIRED_APPLE_SCOPE, "searchadsorg")

    def test_token_response_must_have_exact_scope(self):
        from apple_ads_mcp.auth.apple_oauth import OAuthError, parse_token_response

        ok = {"access_token": "t", "token_type": "Bearer", "expires_in": 3600, "scope": "searchadsorg"}
        self.assertEqual(parse_token_response(ok)[0], "t")
        with self.assertRaises(OAuthError):
            parse_token_response({**ok, "scope": "searchadsorg other"})
        with self.assertRaises(OAuthError):
            parse_token_response({**ok, "token_type": "MAC"})
        with self.assertRaises(OAuthError):
            parse_token_response({**ok, "expires_in": 0})


if __name__ == "__main__":
    unittest.main()
