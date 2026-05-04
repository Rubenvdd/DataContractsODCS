#!/usr/bin/env python3
"""
check_breaking_changes.py -- Melodify Breaking Change Detection

Compares two ODCS v3.1.0 contracts using the datacontract-cli changelog API,
classifies each difference as BREAKING or NON-BREAKING, and exits with
code 1 if any breaking changes are found.

Usage:
  uv run python scripts/check_breaking_changes.py <old_contract> <new_contract>

Exit codes:
  0 -- no breaking changes
  1 -- breaking changes detected (or script error)
"""

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import yaml
from datacontract.data_contract import DataContract
from datacontract.model.changelog import ChangelogResult, ChangelogType


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Change:
    schema_name: str
    field_name: str
    message: str
    breaking: bool


# ---------------------------------------------------------------------------
# Change classification
# ---------------------------------------------------------------------------

_BREAKING_ATTRS = {"physicalType", "logicalType", "primaryKey"}


def classify_changelog(result: ChangelogResult) -> tuple[list[Change], list[Change]]:
    breaking: list[Change] = []
    non_breaking: list[Change] = []
    idx = {e.path: e for e in result.entries}
    removed_required: list[tuple[str, str]] = []

    wholly_changed = {
        e.path
        for e in result.summary
        if e.type in (ChangelogType.added, ChangelogType.removed)
    }

    def _field_type(path: str, value_key: str) -> str:
        for attr in ("physicalType", "logicalType"):
            e = idx.get(f"{path}.{attr}")
            if e:
                val = e.old_value if value_key == "old" else e.new_value
                if val:
                    return val
        return "unknown"

    def _add(change: Change) -> None:
        (breaking if change.breaking else non_breaking).append(change)

    # Schema / field level: from summary
    for entry in result.summary:
        parts = entry.path.split(".")

        if len(parts) == 2 and parts[0] == "schema":
            schema_name = parts[1]
            if entry.type == ChangelogType.removed:
                _add(Change(schema_name, "", f"Removed schema: {schema_name}", breaking=True))
            elif entry.type == ChangelogType.added:
                _add(Change(schema_name, "", f"Added schema: {schema_name}", breaking=False))

        elif len(parts) == 4 and parts[0] == "schema" and parts[2] == "properties":
            schema_name, field_name = parts[1], parts[3]
            if entry.type == ChangelogType.removed:
                req = idx.get(f"{entry.path}.required")
                was_required = req is not None and req.old_value == "True"
                ftype = _field_type(entry.path, "old")
                if was_required:
                    removed_required.append((field_name, ftype))
                    _add(Change(
                        schema_name, field_name,
                        f"Removed required field: {field_name} (existing data may not satisfy)",
                        breaking=True,
                    ))
                else:
                    _add(Change(schema_name, field_name, f"Removed optional field: {field_name}", breaking=False))
            elif entry.type == ChangelogType.added:
                req = idx.get(f"{entry.path}.required")
                req_label = "required" if req and req.new_value == "True" else "optional"
                ftype = _field_type(entry.path, "new")
                rename_note = build_rename_note(ftype, removed_required)
                note = f" -- {rename_note}" if rename_note else ""
                _add(Change(
                    schema_name, field_name,
                    f"Added {req_label} field: {field_name}{note}",
                    breaking=False,
                ))

    # Attribute level: from entries, skipping sub-entries of wholly-changed paths
    for entry in result.entries:
        if entry.path in wholly_changed or any(
            entry.path.startswith(p + ".") for p in wholly_changed
        ):
            continue

        parts = entry.path.split(".")

        if entry.path == "version" and entry.type == ChangelogType.updated:
            _add(Change("", "", f"Version updated: {entry.old_value} -> {entry.new_value}", breaking=False))
            continue

        if len(parts) != 5 or parts[0] != "schema" or parts[2] != "properties":
            continue

        schema_name, field_name, attr = parts[1], parts[3], parts[4]

        if entry.type == ChangelogType.updated:
            if attr in _BREAKING_ATTRS:
                _add(Change(
                    schema_name, field_name,
                    f"Field {attr} changed: {field_name} {entry.old_value!r} -> {entry.new_value!r}",
                    breaking=True,
                ))
            elif attr == "required":
                if entry.old_value == "False" and entry.new_value == "True":
                    _add(Change(
                        schema_name, field_name,
                        f"Required changed: {field_name} false -> true (existing data may not satisfy)",
                        breaking=True,
                    ))
                else:
                    _add(Change(
                        schema_name, field_name,
                        f"Required changed: {field_name} true -> false (constraint loosened)",
                        breaking=False,
                    ))
        elif entry.type == ChangelogType.removed and attr == "unique" and entry.old_value == "True":
            _add(Change(
                schema_name, field_name,
                f"Unique constraint removed: {field_name} (consumers relying on uniqueness may break)",
                breaking=True,
            ))

    return breaking, non_breaking


def build_rename_note(added_type: str, removed_required: list[tuple[str, str]]) -> str:
    """Hints at a possible rename when an added field shares its type with a removed required field."""
    for rem_name, rem_type in removed_required:
        if rem_type and rem_type != "unknown" and rem_type == added_type:
            return f"may be a rename of {rem_name} -- verify manually."
    return ""


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

SEP = "=" * 60
SUB = "-" * 60


def print_report(
    old_path: Path,
    new_path: Path,
    old_version: str,
    new_version: str,
    breaking: list[Change],
    non_breaking: list[Change],
) -> None:
    print(SEP)
    print("  Breaking Change Detection")
    print(SEP)
    print()
    print(f"  OLD: {old_path}")
    print(f"       Contract version: {old_version}")
    print(f"  NEW: {new_path}")
    print(f"       Contract version: {new_version}")

    all_changes = breaking + non_breaking
    if not all_changes:
        print()
        print("  No schema differences found.")
    else:
        print()
        print(SUB)
        print("  SCHEMA CHANGES")
        print(SUB)

        by_schema: dict[str, list[Change]] = defaultdict(list)
        for change in all_changes:
            by_schema[change.schema_name].append(change)

        for schema_name, changes in by_schema.items():
            print()
            if schema_name:
                print(f"  Schema: {schema_name}")
            for change in changes:
                tag = "BREAKING    " if change.breaking else "NON-BREAKING"
                print(f"  [{tag}] {change.message}")

    print()
    print(SUB)
    print("  SUMMARY")
    print(SUB)
    print()
    print(f"  Non-breaking changes: {len(non_breaking)}")
    print(f"  Breaking changes:     {len(breaking)}")
    print()

    if breaking:
        print("  RESULT: BREAKING CHANGES DETECTED")
        print()
        print("  See docs/deprecation-process.md for the migration playbook.")
    else:
        print("  RESULT: NO BREAKING CHANGES")

    print()
    print(SEP)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _contract_version(path: Path) -> str:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f).get("version", "unknown")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect breaking changes between two ODCS data contracts."
    )
    parser.add_argument("old_contract", type=Path, help="Path to the old (v1) contract YAML")
    parser.add_argument("new_contract", type=Path, help="Path to the new (v2) contract YAML")
    args = parser.parse_args()

    for p in (args.old_contract, args.new_contract):
        if not p.exists():
            print(f"ERROR: File not found: {p}", file=sys.stderr)
            sys.exit(1)

    result = DataContract(data_contract_file=str(args.old_contract)).changelog(
        DataContract(data_contract_file=str(args.new_contract))
    )

    breaking, non_breaking = classify_changelog(result)

    print_report(
        args.old_contract,
        args.new_contract,
        _contract_version(args.old_contract),
        _contract_version(args.new_contract),
        breaking,
        non_breaking,
    )

    sys.exit(1 if breaking else 0)


if __name__ == "__main__":
    main()
