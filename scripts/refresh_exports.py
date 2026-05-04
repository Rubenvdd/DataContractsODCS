#!/usr/bin/env python3
"""
refresh_exports.py — Regenerate HTML contract documentation.

The HTML exports in dbt/exports/ were generated in Phase 1 and may be
stale after contract changes (slug-style ID rename, schema updates).

This script regenerates them from the current contracts.

Usage:
    uv run python scripts/refresh_exports.py
"""

import subprocess
import sys
from pathlib import Path


EXPORTS = [
    {
        "contract": "contracts/listeners/inbound.odcs.yaml",
        "output": "dbt/exports/listeners-contract.html",
        "label": "Listeners (inbound)",
    },
    {
        "contract": "contracts/content/datacontract.odcs.yaml",
        "output": "dbt/exports/content-contract.html",
        "label": "Content",
    },
    {
        "contract": "contracts/playback/datacontract.odcs.yaml",
        "output": "dbt/exports/playback-contract.html",
        "label": "Playback",
    },
]


def main():
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent

    print("Refreshing HTML contract exports...\n")

    all_ok = True
    for export in EXPORTS:
        contract = project_root / export["contract"]
        output = project_root / export["output"]
        output.parent.mkdir(parents=True, exist_ok=True)

        print(f"  {export['label']}: {contract.name} -> {output.name}")

        result = subprocess.run(
            ["uv", "run", "datacontract", "export", str(contract),
             "--format", "html", "--output", str(output)],
            cwd=project_root,
            capture_output=True,
            text=False,
        )

        if result.returncode == 0:
            print(f"    [PASS] Written to {output.relative_to(project_root)}")
        else:
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            print(f"    [FAIL] {stderr.strip()[:200]}")
            all_ok = False

    print(f"\n{'All exports refreshed.' if all_ok else 'Some exports failed.'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
