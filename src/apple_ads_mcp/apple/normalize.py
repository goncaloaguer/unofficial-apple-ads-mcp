"""Normalization of Apple Ads payloads: money, rates, dates, enums.

Money arrives as ``{"amount": "12.34", "currency": "USD"}`` (amount is a
string with up to two decimals). We expose ``{"amount": float, "currency"}``
and keep provenance in the envelope's derived_metrics where we compute.
Unknown enum values and unknown fields are preserved untouched.
"""
from __future__ import annotations

from typing import Any

MONEY_FIELDS = frozenset(
    {"localSpend", "cpt", "cpm", "tapInstallCPI", "totalAvgCPI", "bid", "dailyBudget",
     "budgetAmount", "defaultBidAmount", "cpaGoal", "targetCpa", "amount"}
)


def money(value: Any) -> dict[str, Any] | None:
    """Normalize a Money object; returns None for null/absent values."""
    if value is None:
        return None
    if isinstance(value, dict) and "amount" in value:
        try:
            amount = float(value["amount"]) if value["amount"] is not None else None
        except (TypeError, ValueError):
            return {"amount": None, "currency": value.get("currency"), "raw": value.get("amount")}
        return {"amount": amount, "currency": value.get("currency")}
    return value


def normalize_metrics(metrics: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten a report metrics object: money → floats, keep everything else."""
    if not metrics:
        return {}
    out: dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, dict) and "amount" in value:
            m = money(value)
            out[key] = m["amount"] if m else None
            if m and m.get("currency") and "currency" not in out:
                out["currency"] = m["currency"]
        else:
            out[key] = value
    return out


def normalize_entity(entity: dict[str, Any]) -> dict[str, Any]:
    """Shallow-normalize an entity (campaign, ad group, keyword…): money fields → floats."""
    out: dict[str, Any] = {}
    for key, value in entity.items():
        if key in MONEY_FIELDS and isinstance(value, dict):
            out[key] = money(value)
        elif isinstance(value, dict) and set(value) <= {"amount", "currency"} and "amount" in value:
            out[key] = money(value)
        else:
            out[key] = value
    return out


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator
