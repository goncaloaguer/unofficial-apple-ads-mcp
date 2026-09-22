"""Deterministic analysis helpers (stdlib-only)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from apple_ads_mcp import analysis  # noqa: E402

ROWS = [
    {"id": 1, "date": "2026-08-01", "localSpend": 10.0, "impressions": 1000, "taps": 50, "totalInstalls": 5, "tapInstalls": 4, "totalNewDownloads": 4, "ttr": 0.05},
    {"id": 1, "date": "2026-08-02", "localSpend": 30.0, "impressions": 1000, "taps": 50, "totalInstalls": 15, "tapInstalls": 10, "totalNewDownloads": 10, "ttr": 0.05},
]


class AnalysisTests(unittest.TestCase):
    def test_aggregate_ignores_apple_rates(self):
        totals = analysis.aggregate(ROWS)
        self.assertEqual(totals["localSpend"], 40.0)
        self.assertNotIn("ttr", totals)

    def test_rates_and_zero_denominators(self):
        rates = analysis.derive_rates(analysis.aggregate(ROWS))
        self.assertEqual(rates["cpt"], 0.4)
        self.assertEqual(rates["cpi"], 2.0)
        self.assertEqual(rates["ttr"], 0.05)
        self.assertEqual(rates["newDownloadShare"], 0.7)
        empty = analysis.derive_rates({"localSpend": 5.0})
        self.assertIsNone(empty["cpi"])

    def test_compare(self):
        out = {c["metric"]: c for c in analysis.compare({"localSpend": 10.0, "cpi": None}, {"localSpend": 15.0, "cpi": 2.0})}
        self.assertEqual(out["localSpend"]["pct_change"], 0.5)
        self.assertNotIn("delta", out["cpi"])

    def test_rank_direction_and_min_spend(self):
        rows = [{"id": 1, "cpi": 3.0, "localSpend": 30}, {"id": 2, "cpi": 1.0, "localSpend": 1}, {"id": 3, "cpi": 2.0, "localSpend": 20}]
        self.assertEqual([r["id"] for r in analysis.rank(rows, "cpi")], [2, 3, 1])
        self.assertEqual([r["id"] for r in analysis.rank(rows, "cpi", min_spend=5)], [3, 1])
        self.assertEqual([r["id"] for r in analysis.rank(rows, "localSpend")], [2, 3, 1])  # cost metric: ascending by default
        self.assertEqual([r["id"] for r in analysis.rank(rows, "localSpend", ascending=False)], [1, 3, 2])

    def test_trend_anomaly(self):
        rows = [{"date": f"2026-08-{d:02d}", "taps": 10} for d in range(1, 9)] + [{"date": "2026-08-09", "taps": 40}]
        rows[3]["taps"] = 11
        out = analysis.trend_series(rows, "taps", window=7)
        self.assertEqual(len(out["anomalies"]), 1)
        self.assertEqual(out["anomalies"][0]["date"], "2026-08-09")
        self.assertEqual(out["change_pct"], 3.0)

    def test_pacing_flags(self):
        self.assertEqual(analysis.pacing(spend=95, daily_budget=10, days=10, status="ENABLED")["flag"], "budget_capped")
        self.assertEqual(analysis.pacing(spend=20, daily_budget=10, days=10, status="ENABLED")["flag"], "under_delivering")
        self.assertEqual(analysis.pacing(spend=70, daily_budget=10, days=10, status="ENABLED")["flag"], "on_pace")
        self.assertEqual(analysis.pacing(spend=70, daily_budget=10, days=10, status="PAUSED")["flag"], "not_enabled")
        self.assertEqual(analysis.pacing(spend=70, daily_budget=None, days=10, status="ENABLED")["flag"], "no_daily_budget")


if __name__ == "__main__":
    unittest.main()
