#!/usr/bin/env python3
"""Detect drift between the pinned operation inventory and the reviewed registry.

Usage:
    python3 scripts/check_api_drift.py                # CI mode (pinned inventory)
    python3 scripts/check_api_drift.py /path/to/sdk   # compare against a newer SDK checkout

CI mode fails if the registry was not regenerated after the inventory changed,
if any inventory operation is unclassified, or if the inventory checksum
differs from spec/SHA256SUMS. With an SDK checkout it re-extracts the inventory
and reports added/removed/changed operations that require manual classification
before spec/ and the registry may be updated (in the same commit).

Exit codes: 0 = no drift, 1 = drift found.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

SPEC = ROOT / "spec" / "operations.json"
SUMS = ROOT / "spec" / "SHA256SUMS"
REGISTRY = ROOT / "src" / "apple_ads_mcp" / "policy" / "read_operations.json"


def main() -> int:
    registry = json.loads(REGISTRY.read_text())
    reg_ops = {(e["method"], e["path"]): e for e in registry["operations"]}
    problems: list[str] = []

    spec_sha = hashlib.sha256(SPEC.read_bytes()).hexdigest()
    if registry.get("operations_sha256") != spec_sha:
        problems.append(
            "registry operations_sha256 does not match spec/operations.json — "
            "rerun scripts/generate_registry.py and review the diff"
        )
    recorded = SUMS.read_text().split()[0] if SUMS.exists() else None
    if recorded != spec_sha:
        problems.append("spec/SHA256SUMS does not match spec/operations.json")

    if len(sys.argv) > 1:
        from extract_operations import extract  # type: ignore

        new_ops = {(o["method"], o["path"]): o for o in extract(Path(sys.argv[1]))}
        label = f"SDK checkout {sys.argv[1]}"
    else:
        new_ops = {
            (o["method"], o["path"]): o
            for o in json.loads(SPEC.read_text())["operations"]
        }
        label = "spec/operations.json"

    for key in sorted(set(new_ops) - set(reg_ops)):
        problems.append(f"NEW unclassified operation: {key[0]} {key[1]}")
    for key in sorted(set(reg_ops) - set(new_ops)):
        problems.append(f"REMOVED operation still in registry: {key[0]} {key[1]}")
    for key in sorted(set(reg_ops) & set(new_ops)):
        if new_ops[key]["requires_context"] != reg_ops[key]["requires_context"]:
            problems.append(f"X-AP-Context requirement changed: {key[0]} {key[1]}")

    if problems:
        print(f"DRIFT DETECTED against {label}:")
        for p in problems:
            print(f"  - {p}")
        print(
            "\nClassify every new or changed operation in scripts/generate_registry.py, "
            "re-run scripts/extract_operations.py and scripts/generate_registry.py, "
            "and commit spec/ + the registry together."
        )
        return 1
    print(f"no drift: {len(reg_ops)} operations match {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
