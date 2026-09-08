#!/usr/bin/env python3
"""Generate the read-operation registry from the extracted operation inventory.

Every operation in spec/operations.json is explicitly classified. The output
(src/apple_ads_mcp/policy/read_operations.json) is the single source of truth
for which upstream Apple Ads Platform API operations this server may ever call.

Classification policy (PLAN.md §2.2 / §4.1):
- PUT, PATCH, DELETE → denied, always.
- Any path containing /apply, /dismiss, /bulk-, /upload → denied, always.
- POST is denied unless the path ends in /query or is one of the reviewed
  READ_POST_EXCEPTIONS (currently only POST /v1/search/geo, a lookup by IDs).
- Remaining GET/POST operations are read-eligible. Those under Apple Maps
  (BUSINESS_BRAND) prefixes are `disabled` (read-only, but outside v1 tool
  scope); everything else is `enabled`.

The generated file is version-controlled; humans review every change.
Run: python3 scripts/generate_registry.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "spec" / "operations.json"
OUT_PATH = ROOT / "src" / "apple_ads_mcp" / "policy" / "read_operations.json"

# POST operations without a /query suffix that are semantically reads.
READ_POST_EXCEPTIONS: dict[str, str] = {
    "/v1/search/geo": "Geo lookup by a list of IDs; request body carries IDs only, no state change",
}

# Substrings that mark a mutating action regardless of method.
MUTATING_MARKERS = ("/apply", "/dismiss", "/bulk-", "/upload")

# Apple Maps (promotedObjectType BUSINESS_BRAND) surface — read-only, disabled in v1.
MAPS_PREFIXES = (
    "/v1/business-brands",
    "/v1/business-categories",
    "/v1/locations",
    "/v1/location-groups",
    "/v1/assets",
    "/v1/rejection-reasons/business-brands",
    "/v1/reports/business-brands",
)
MAPS_REASON = "Apple Maps (BUSINESS_BRAND) surface: read-only but outside v1 tool scope"

RATE_GROUPS = (
    ("/v1/reports/", "reports"),
    ("/v1/insights/", "insights"),
    ("/v1/change-history", "audit"),
    ("/v1/recommendations/", "recommendations"),
    ("/v1/suggestions/", "recommendations"),
    ("/v1/search/", "search"),
)

SENSITIVITY = (
    ("/v1/me", "identity"),
    ("/v1/acls", "identity"),
    ("/v1/orgs/", "identity"),
    ("/v1/advertiser-resources", "identity"),
    ("/v1/change-history", "audit"),
    ("/v1/reports/", "metrics"),
    ("/v1/insights/", "metrics"),
)


def classify(method: str, path: str) -> tuple[str, str]:
    if method in ("PUT", "PATCH", "DELETE"):
        return "denied", f"{method} is a write method"
    if any(marker in path for marker in MUTATING_MARKERS):
        return "denied", "mutating POST action (apply/dismiss/bulk/upload)"
    if method == "POST" and not path.endswith("/query"):
        if path in READ_POST_EXCEPTIONS:
            return "enabled", READ_POST_EXCEPTIONS[path]
        return "denied", "POST without /query suffix creates an entity"
    if method not in ("GET", "POST"):
        return "denied", f"unexpected method {method}"
    if path.startswith(MAPS_PREFIXES):
        return "disabled", MAPS_REASON
    return "enabled", ""


def rate_group(path: str) -> str:
    for prefix, group in RATE_GROUPS:
        if path.startswith(prefix):
            return group
    return "entity"


def sensitivity(path: str) -> str:
    for prefix, cls in SENSITIVITY:
        if path.startswith(prefix):
            return cls
    return "standard"


def main() -> int:
    spec = json.loads(SPEC_PATH.read_text())
    spec_sha = hashlib.sha256(SPEC_PATH.read_bytes()).hexdigest()
    entries = []
    counts = {"enabled": 0, "disabled": 0, "denied": 0}
    for op in spec["operations"]:
        method, path = op["method"], op["path"]
        cls, reason = classify(method, path)
        counts[cls] += 1
        entries.append(
            {
                "operation_id": op["operation_id"],
                "method": method,
                "path": path,
                "classification": cls,
                "reason": reason,
                "requires_context": op["requires_context"],
                "rate_group": rate_group(path),
                "paginated": path.endswith("/query"),
                "sensitivity": sensitivity(path),
            }
        )
    registry = {
        "spec_version": spec["spec_version"],
        "sdk_commit": spec["sdk_commit"],
        "operations_sha256": spec_sha,
        "counts": counts,
        "operations": entries,
    }
    OUT_PATH.write_text(json.dumps(registry, indent=1) + "\n")
    print(f"registry written: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
