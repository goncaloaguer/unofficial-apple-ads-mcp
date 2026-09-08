#!/usr/bin/env python3
"""Extract the Apple Ads Platform API operation inventory from Apple's SDK.

Apple does not publish the OpenAPI document for the Platform API separately,
but its official client library (apple/apple-ads-platform-api-python) is
generated from it with openapi-generator. Every operation appears there as a
``_<operationId>_serialize`` method carrying ``method=`` and
``resource_path=``. This script turns that into ``spec/operations.json`` — the
machine-readable inventory the read-operation registry is classified from.

Usage:
    python3 scripts/extract_operations.py /path/to/apple-ads-platform-api-python

The checkout's commit and ``spec.version`` are recorded in ``spec/SDK_PIN`` so
CI can re-extract deterministically (see scripts/check_api_drift.py).
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "spec" / "operations.json"
PIN = ROOT / "spec" / "SDK_PIN"
SUMS = ROOT / "spec" / "SHA256SUMS"

_FUNC_SPLIT = re.compile(r"\n    def _")
_METHOD = re.compile(r"method='([A-Z]+)'")
_PATH = re.compile(r"resource_path='([^']+)'")
_CONTEXT = re.compile(r"x_ap_context:\s*(Optional\[)?StrictStr")
_CONTEXT_REQUIRED = re.compile(r"x_ap_context:\s*Annotated\[StrictStr")


def extract(sdk_root: Path) -> list[dict]:
    api_file = sdk_root / "apple_ads_platform" / "api" / "apple_ads_api.py"
    src = api_file.read_text()
    ops: list[dict] = []
    for chunk in _FUNC_SPLIT.split(src)[1:]:
        name = chunk.split("(", 1)[0]
        if not name.endswith("_serialize"):
            continue
        op_id = name[: -len("_serialize")]
        m, p = _METHOD.search(chunk), _PATH.search(chunk)
        if not (m and p):
            continue
        # Determine X-AP-Context requirement from the public method signature.
        public = re.search(
            rf"\n    def {re.escape(op_id)}\((.*?)\)\s*->", src, re.DOTALL
        )
        sig = public.group(1) if public else ""
        if "x_ap_context" not in sig:
            requires_context = "none"
        elif re.search(r"x_ap_context:\s*Optional\[", sig):
            requires_context = "optional"
        else:
            requires_context = "required"
        ops.append(
            {
                "operation_id": op_id,
                "method": m.group(1),
                "path": "/v1" + p.group(1),
                "requires_context": requires_context,
            }
        )
    ops.sort(key=lambda o: (o["path"], o["method"]))
    return ops


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    sdk_root = Path(sys.argv[1]).resolve()
    ops = extract(sdk_root)
    commit = subprocess.run(
        ["git", "-C", str(sdk_root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    spec_version = (sdk_root / "spec.version").read_text().strip()
    payload = {
        "source": "apple/apple-ads-platform-api-python",
        "sdk_commit": commit,
        "spec_version": spec_version,
        "count": len(ops),
        "operations": ops,
    }
    OUT.write_text(json.dumps(payload, indent=1) + "\n")
    PIN.write_text(f"commit={commit}\nspec.version={spec_version}\n")
    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
    SUMS.write_text(f"{digest}  operations.json\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(ops)} operations (spec.version {spec_version})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
