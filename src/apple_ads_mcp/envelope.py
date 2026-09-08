"""Common response envelope (PLAN.md §7.2)."""
from __future__ import annotations

import json
from typing import Any


def build_envelope(
    *,
    data: Any,
    meta: dict[str, Any],
    account_id: str | None = None,
    summary: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    derived_metrics: list[dict[str, str]] | None = None,
    max_response_bytes: int | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "data": data,
        "summary": summary or {},
        "meta": {"account_id": account_id, "source": "Apple Ads Platform API v1", **meta},
        "warnings": warnings or [],
        "derived_metrics": derived_metrics or [],
    }
    if max_response_bytes:
        size = len(json.dumps(envelope, default=str).encode())
        while size > max_response_bytes and isinstance(data, list) and data:
            keep = max(1, len(data) // 2)
            data = data[:keep]
            envelope["data"] = data
            envelope["meta"]["truncated"] = True
            envelope["meta"]["rows_returned"] = len(data)
            size = len(json.dumps(envelope, default=str).encode())
        if envelope["meta"].get("truncated"):
            envelope["warnings"].append(
                "response truncated to fit the size ceiling; narrow the "
                "request (fewer fields, shorter range, filters) for complete "
                "results"
            )
    return envelope
