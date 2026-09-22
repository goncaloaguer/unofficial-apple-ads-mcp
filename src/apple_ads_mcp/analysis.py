"""Deterministic analysis: aggregation, rates, comparison, ranking, trends, pacing.

Pure stdlib functions over flattened report rows (see tools/reporting_tools.
flatten_rows). Every derived value's formula is returned alongside it
(PLAN.md §2.5); zero denominators yield None, never infinity.
"""
from __future__ import annotations

from statistics import mean, pstdev
from typing import Any

SUMMABLE = (
    "localSpend", "impressions", "taps", "tapInstalls", "viewInstalls", "totalInstalls",
    "totalNewDownloads", "totalRedownloads", "tapNewDownloads", "tapRedownloads",
    "viewNewDownloads", "viewRedownloads", "tapPreOrdersPlaced", "viewPreOrdersPlaced",
    "totalPreOrdersPlaced",
)
# Apple-provided rates/averages: never summed, recomputed from totals instead.
NON_SUMMABLE = frozenset({"ttr", "cpt", "cpm", "tapInstallCPI", "totalAvgCPI", "totalInstallRate", "tapInstallRate"})
COST_METRICS = frozenset({"localSpend", "cpt", "cpm", "tapInstallCPI", "totalAvgCPI", "cpi", "cpt_derived", "cpi_derived"})

RATE_FORMULAS = {
    "ttr": "sum(taps)/sum(impressions)",
    "cpt": "sum(localSpend)/sum(taps)",
    "cpm": "sum(localSpend)*1000/sum(impressions)",
    "cpi": "sum(localSpend)/sum(totalInstalls)",
    "tapInstallCPI": "sum(localSpend)/sum(tapInstalls)",
    "totalInstallRate": "sum(totalInstalls)/sum(taps)",
    "tapInstallRate": "sum(tapInstalls)/sum(taps)",
    "newDownloadShare": "sum(totalNewDownloads)/sum(totalInstalls)",
}


# Split metrics that add little in ranked/mined rows; dropped when zero (compact_row).
SPARSE_WHEN_ZERO = frozenset({
    "tapPreOrdersPlaced", "viewPreOrdersPlaced", "totalPreOrdersPlaced",
    "tapNewDownloads", "tapRedownloads", "viewNewDownloads", "viewRedownloads",
    "viewInstalls", "totalRedownloads",
})


def compact_row(row: dict) -> dict:
    """Drop sparse split metrics that are zero/None; totals and rates always stay."""
    return {k: v for k, v in row.items() if not (k in SPARSE_WHEN_ZERO and not v)}


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def aggregate(rows: list[dict]) -> dict[str, float]:
    """Sum summable metrics across rows."""
    totals: dict[str, float] = {}
    for row in rows:
        for key in SUMMABLE:
            value = row.get(key)
            if isinstance(value, (int, float)):
                totals[key] = round(totals.get(key, 0) + value, 4)
    return totals


def derive_rates(totals: dict[str, float]) -> dict[str, float | None]:
    """Standard rates from aggregated totals (formulas in RATE_FORMULAS)."""
    spend = totals.get("localSpend")
    imps = totals.get("impressions")
    taps = totals.get("taps")
    installs = totals.get("totalInstalls")
    tap_installs = totals.get("tapInstalls")

    def r(value: float | None, digits: int = 4) -> float | None:
        return round(value, digits) if value is not None else None

    return {
        "ttr": r(safe_div(taps, imps), 6),
        "cpt": r(safe_div(spend, taps)),
        "cpm": r(safe_div((spend or 0) * 1000 if spend is not None else None, imps)),
        "cpi": r(safe_div(spend, installs)),
        "tapInstallCPI": r(safe_div(spend, tap_installs)),
        "totalInstallRate": r(safe_div(installs, taps)),
        "tapInstallRate": r(safe_div(tap_installs, taps)),
        "newDownloadShare": r(safe_div(totals.get("totalNewDownloads"), installs)),
    }


def summarize(rows: list[dict]) -> dict[str, Any]:
    totals = aggregate(rows)
    return {**totals, **derive_rates(totals)}


def group_rows(rows: list[dict], key_fields: tuple[str, ...]) -> dict[tuple, list[dict]]:
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = tuple(row.get(k) for k in key_fields)
        groups.setdefault(key, []).append(row)
    return groups


def compare(period_a: dict[str, Any], period_b: dict[str, Any]) -> list[dict[str, Any]]:
    """Compare metric dicts: absolute and percentage deltas (b relative to a)."""
    out: list[dict[str, Any]] = []
    for key in sorted(set(period_a) | set(period_b)):
        a, b = period_a.get(key), period_b.get(key)
        entry: dict[str, Any] = {"metric": key, "period_a": a, "period_b": b}
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            entry["delta"] = round(b - a, 4)
            entry["pct_change"] = round((b - a) / abs(a), 4) if a else None
        out.append(entry)
    return out


def rank(rows: list[dict], metric: str, *, top_n: int = 20, min_spend: float = 0.0, ascending: bool | None = None) -> list[dict]:
    """Rank rows by a metric; cost metrics rank ascending (cheapest first) by default."""
    if ascending is None:
        ascending = metric in COST_METRICS
    eligible = [
        r for r in rows
        if isinstance(r.get(metric), (int, float)) and (r.get("localSpend") or 0) >= min_spend
    ]
    return sorted(eligible, key=lambda r: r[metric], reverse=not ascending)[:top_n]


def trend_series(rows: list[dict], metric: str, time_key: str = "date", window: int = 7) -> dict[str, Any]:
    """Ordered series + moving average + anomaly flags.

    Anomaly rule: |value - moving_avg| > 2 * stdev of the prior window
    (requires >= window prior points).
    """
    ordered = sorted((r for r in rows if isinstance(r.get(metric), (int, float))), key=lambda r: str(r.get(time_key, "")))
    values = [float(r[metric]) for r in ordered]
    series, anomalies = [], []
    for i, row in enumerate(ordered):
        point: dict[str, Any] = {time_key: row.get(time_key), metric: values[i]}
        prior = values[max(0, i - window): i]
        if len(prior) >= window:
            avg, sd = mean(prior), pstdev(prior)
            point[f"moving_avg_{window}"] = round(avg, 4)
            if sd > 0 and abs(values[i] - avg) > 2 * sd:
                point["anomaly"] = True
                anomalies.append({time_key: row.get(time_key), "value": values[i], "expected": round(avg, 4),
                                  "rule": f"|value - {window}-period mean| > 2 * stdev"})
        series.append(point)
    first, last = (values[0], values[-1]) if values else (None, None)
    return {
        "series": series,
        "anomalies": anomalies,
        "first": first,
        "last": last,
        "change_pct": round((last - first) / abs(first), 4) if first else None,
    }


def pacing(*, spend: float, daily_budget: float | None, days: int, status: str | None) -> dict[str, Any]:
    """Spend vs dailyBudget × days. Apple may exceed dailyBudget on a given day
    while averaging to it over a month, so utilization > 1 is possible."""
    out: dict[str, Any] = {"spend": spend, "daily_budget": daily_budget, "days": days}
    if daily_budget:
        expected = daily_budget * days
        out["expected_spend"] = round(expected, 2)
        out["utilization"] = round(spend / expected, 4)
        if status and status != "ENABLED":
            out["flag"] = "not_enabled"
        elif out["utilization"] >= 0.9:
            out["flag"] = "budget_capped"
        elif out["utilization"] <= 0.5:
            out["flag"] = "under_delivering"
        else:
            out["flag"] = "on_pace"
    else:
        out["flag"] = "no_daily_budget"
    return out
