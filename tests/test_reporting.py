"""Report request validation: granularity windows, groupBy, time zones, options."""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from apple_ads_mcp import reporting  # noqa: E402
from apple_ads_mcp.policy.limits import LimitExceeded  # noqa: E402

TODAY = dt.date(2026, 9, 2)


def build(**kw):
    defaults = dict(level="campaigns", start="2026-08-01", end="2026-08-31", today=TODAY)
    defaults.update(kw)
    return reporting.build_report_request(**defaults)


class GranularityWindows(unittest.TestCase):
    def test_none_is_fine_for_single_day(self):
        path, body = build(start="2026-08-31", end="2026-08-31")
        self.assertEqual(path, "/v1/reports/apps/campaigns/query")
        self.assertNotIn("granularity", body["timeRange"])

    def test_hourly(self):
        build(granularity="HOURLY", start="2026-08-30", end="2026-09-01")
        with self.assertRaises(LimitExceeded):
            build(granularity="HOURLY", start="2026-08-20", end="2026-09-01")
        with self.assertRaises(LimitExceeded):
            build(level="ads", granularity="HOURLY", start="2026-08-30", end="2026-09-01")
        with self.assertRaises(LimitExceeded):
            build(level="searchterms", granularity="HOURLY", start="2026-08-30", end="2026-09-01")

    def test_daily(self):
        build(granularity="DAILY")
        with self.assertRaises(LimitExceeded):
            build(granularity="DAILY", start="2026-05-01", end="2026-05-31")  # start > 90 days ago
        with self.assertRaises(LimitExceeded):
            build(granularity="DAILY", start="2026-08-31", end="2026-08-31")  # single day

    def test_weekly(self):
        build(granularity="WEEKLY", start="2026-06-01", end="2026-08-15")
        with self.assertRaises(LimitExceeded):
            build(granularity="WEEKLY", start="2026-08-01", end="2026-08-31")  # end < 14 days ago
        with self.assertRaises(LimitExceeded):
            build(granularity="WEEKLY", start="2025-06-01", end="2025-08-01", max_report_days=400)

    def test_monthly(self):
        build(granularity="MONTHLY", start="2026-03-01", end="2026-05-31", max_report_days=120)
        with self.assertRaises(LimitExceeded):
            build(granularity="MONTHLY", start="2026-07-01", end="2026-08-31")

    def test_unknown(self):
        with self.assertRaises(LimitExceeded):
            build(granularity="YEARLY")


class GroupByAndOptions(unittest.TestCase):
    def test_group_by_per_level(self):
        build(group_by=["storefront", "gender"])
        with self.assertRaises(LimitExceeded):
            build(level="keywords", group_by=["gender"])
        with self.assertRaises(LimitExceeded):
            build(level="ads", group_by=["deviceClass"])

    def test_hourly_excludes_demographics(self):
        with self.assertRaises(LimitExceeded):
            build(granularity="HOURLY", start="2026-08-30", end="2026-09-01", group_by=["gender"])
        build(granularity="HOURLY", start="2026-08-30", end="2026-09-01", group_by=["storefront"])

    def test_searchterms_ortz_only_and_no_include_rows(self):
        with self.assertRaises(LimitExceeded):
            build(level="searchterms", time_zone="UTC")
        with self.assertRaises(LimitExceeded):
            build(level="searchterms", include_grand_total=True)
        path, body = build(level="searchterms")
        self.assertEqual(body["timeRange"]["timeZone"], "ORTZ")

    def test_empty_metrics_not_with_group_by(self):
        with self.assertRaises(LimitExceeded):
            build(include_empty_metrics=True, group_by=["storefront"])
        _, body = build(include_empty_metrics=True, include_grand_total=True)
        self.assertEqual(body["options"]["includeRows"], ["GRAND_TOTAL", "EMPTY_METRICS"])

    def test_fields_and_filters_validated(self):
        with self.assertRaises(LimitExceeded):
            build(fields=["revenue"])
        with self.assertRaises(LimitExceeded):
            build(filters=[{"field": "name", "operator": "EQUALS", "value": "x"}])
        _, body = build(fields=["localSpend", "taps"], filters=[{"field": "campaignId", "operator": "IN", "value": [1, 2]}])
        self.assertEqual(body["fields"], ["localSpend", "taps"])
        self.assertEqual(body["filters"], [{"field": "campaignId", "operator": "IN", "value": [1, 2]}])

    def test_range_ceiling_and_order(self):
        with self.assertRaises(LimitExceeded):
            build(start="2026-01-01", end="2026-08-31")
        with self.assertRaises(LimitExceeded):
            build(start="2026-08-31", end="2026-08-01")
        with self.assertRaises(LimitExceeded):
            build(level="brands")


class Helpers(unittest.TestCase):
    def test_default_range(self):
        self.assertEqual(reporting.default_date_range(7, today=TODAY), ("2026-08-26", "2026-09-01"))

    def test_freshness(self):
        self.assertIsNotNone(reporting.freshness_warning("2026-09-02", today=TODAY))
        self.assertIsNone(reporting.freshness_warning("2026-09-01", today=TODAY))

    def test_fields_document_is_json(self):
        import json

        doc = json.loads(reporting.report_fields_document())
        self.assertEqual(set(doc["metrics"]), set(reporting.METRICS))


if __name__ == "__main__":
    unittest.main()
