#!/usr/bin/env python3
"""
run_pipeline.py — Melodify Data Contracts POC Orchestrator

Chains the full contract-driven pipeline with circuit-breaking:
  Stage 1: Lint contracts        (shift-left validation)
  Stage 2: Export artifacts      (contract → dbt + soda config)
  Stage 3: dbt build             (seed + source tests + staging + marts)
  Stage 4: Soda scan             (semantic quality checks)
  Stage 5: Summary report        (pass/fail per stage)

Design grounded in:
  - CI/CD integration, single chained operation, fast feedback
  - Circuit breaker pattern — stop pipeline on quality threshold breach
  - Layered testing — structural (dbt) + semantic (Soda) from one contract

Exit codes:
   0  = all stages passed
   1  = generic / unexpected error (fallback — argparse errors, etc.)
  10  = lint stage failed
  11  = export stage failed
  12  = dbt build stage failed
  13  = soda scan stage failed
  20  = expected-failure mode: pipeline completed successfully (regression —
        the seeded violation was masked)
  21  = expected-failure mode: pipeline halted at a stage OTHER than the one
        expected

Usage:
  python scripts/run_pipeline.py                    # all domains, full pipeline
  python scripts/run_pipeline.py --domain listeners  # single domain
  python scripts/run_pipeline.py --skip-data         # skip dbt seed step
  python scripts/run_pipeline.py --domain playback --skip-data
  python scripts/run_pipeline.py --domain listeners --expect-failure-at dbt_build
"""

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Domain registry: maps domain name to contract file(s) and export routing.
# This is the single place where the inbound/outbound split is encoded.
#
# Why this structure?
# - Listeners uses a split pattern: inbound contract for source layer,
#   (future) outbound contract for mart layer.
# - Content and Playback use a single contract for both roles.
# - The orchestrator must know which contract to use for which export type.


STAGES = {
    "lint":      "Lint",
    "export":    "Export",
    "dbt_build": "dbt Build",
    "soda_scan": "Soda Scan",
}

EXIT_CODES = {
    "lint":      10,
    "export":    11,
    "dbt_build": 12,
    "soda_scan": 13,
}

DOMAINS = {
    "listeners": {
        "version": "v1",
        "contracts": {
            "inbound": "contracts/listeners/v1/inbound.odcs.yaml",
            "outbound": "contracts/listeners/v1/outbound.odcs.yaml",
        },
        "export_source": "inbound",  # which contract feeds source-layer exports
        "soda_checks": "soda/checks/listeners.yml",
    },
    "content": {
        "version": "v1",
        "contracts": {
            "inbound": "contracts/content/v1/datacontract.odcs.yaml",
        },
        "export_source": "inbound",
        "soda_checks": "soda/checks/content.yml",
    },
    "playback": {
        "version": "v1",
        "contracts": {
            "inbound": "contracts/playback/v1/datacontract.odcs.yaml",
        },
        "export_source": "inbound",
        "soda_checks": "soda/checks/playback.yml",
    },
}


# ---------------------------------------------------------------------------
# Data classes for tracking results
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    """Result of a single pipeline stage."""

    name: str
    success: bool
    duration_seconds: float
    details: str = ""
    skipped: bool = False


@dataclass
class PipelineResult:
    """Aggregated result of the full pipeline run."""

    stages: list[StageResult] = field(default_factory=list)
    halted_at: Optional[str] = None

    def add(self, result: StageResult):
        self.stages.append(result)

    @property
    def all_passed(self) -> bool:
        return all(s.success or s.skipped for s in self.stages)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_cmd(
    cmd: list[str],
    cwd: Optional[Path] = None,
    capture: bool = True,
) -> subprocess.CompletedProcess:
    """
    Run a shell command and return the result.

    We use subprocess.run with capture so the orchestrator can inspect
    exit codes and output. This is the foundation of the circuit breaker:
    a non-zero exit code means the stage failed.

    Windows encoding fix:
    The datacontract CLI uses the rich library which outputs emoji.
    Windows defaults to cp1252 encoding, which cannot handle these characters.

    Using text=True would make subprocess decode output using the system
    encoding (cp1252), crashing on emoji bytes. Instead, we:
    1. Set PYTHONIOENCODING=utf-8 so the child process WRITES in UTF-8
    2. Set NO_COLOR=1 so rich skips legacy Windows console rendering
    3. Capture raw bytes (text=False) so the parent does not decode via cp1252
    4. Decode the bytes as UTF-8 ourselves with errors='replace' as safety net

    This gives us a clean CompletedProcess with .stdout and .stderr as strings.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"

    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=False,  # capture raw bytes
        env=env,
    )

    # Decode bytes to str ourselves, safely
    return subprocess.CompletedProcess(
        args=result.args,
        returncode=result.returncode,
        stdout=result.stdout.decode("utf-8", errors="replace") if result.stdout else "",
        stderr=result.stderr.decode("utf-8", errors="replace") if result.stderr else "",
    )


def print_stage_header(stage_num: int, name: str, domain: Optional[str] = None):
    """Print a visible stage separator for console readability."""
    scope = f" [{domain}]" if domain else " [all domains]"
    print(f"\n{'='*60}")
    print(f"  Stage {stage_num}: {name}{scope}")
    print(f"{'='*60}")


def print_result(success: bool, message: str):
    """Print a pass/fail indicator."""
    icon = "PASS" if success else "FAIL"
    print(f"  [{icon}] {message}")


def get_schema_names(contract_path: Path) -> list[str]:
    """
    Parse an ODCS contract YAML and return the list of schema names.

    ODCS v3.1.0 contracts define schemas under the 'schema' key as a list
    of objects, each with a 'name' field. For example:

        schema:
          - name: artists
            ...
          - name: tracks
            ...
          - name: albums
            ...

    Returns ['artists', 'tracks', 'albums'].

    The datacontract CLI's dbt-staging-sql export requires --schema-name
    when a contract has multiple schemas. We use this function to discover
    schemas dynamically rather than hardcoding them.
    """
    with open(contract_path, "r", encoding="utf-8") as f:
        contract = yaml.safe_load(f)

    schema = contract.get("schema", [])
    if not schema:
        return []
    # ODCS v3.1.0 uses a list of objects with 'name' keys (not a dict)
    if isinstance(schema, list):
        return [item["name"] for item in schema if isinstance(item, dict) and "name" in item]
    # Fallback: legacy dict format
    return list(schema.keys())


def inject_schema_main(sources_yml_path: Path):
    """
    Post-export fix: inject 'schema: main' into generated sources.yml.

    Why this is needed:
    The datacontract CLI's dbt-sources export does not propagate the
    server.schema field into the generated YAML. Without schema: main,
    dbt-duckdb treats the source name as a schema name, causing queries
    to compile to nonexistent schemas.

    We use YAML parse+dump (not a regex) because:
    - The CLI export format varies in indentation across contracts
    - A regex on '- name:' matches table and column names too, corrupting the file
    - YAML round-trip correctly targets only the top-level source objects

    The injection is idempotent: if schema: main already exists, we skip it.
    """
    if not sources_yml_path.exists():
        print(f"  WARNING: {sources_yml_path} not found, cannot inject schema")
        return False

    content = sources_yml_path.read_text(encoding="utf-8")
    data = yaml.safe_load(content)

    if not data or "sources" not in data:
        print(f"  WARNING: No 'sources' key found in {sources_yml_path.name}")
        return False

    sources = data.get("sources", [])

    # Check if already correct: schema=main AND no stray database field (idempotent)
    already_correct = all(
        s.get("schema") == "main" and "database" not in s
        for s in sources
        if isinstance(s, dict)
    )
    if already_correct:
        print(f"  schema: main already present in {sources_yml_path.name}")
        return True

    # Inject schema: main and remove the database field on each source definition.
    # The 'database' key is exported from the contract's server block when
    # --server is passed (e.g. database: ../duckdb/melodify.duckdb). That path
    # is contract-relative and invalid for dbt-duckdb. The correct database path
    # is already defined in dbt/profiles.yml and must not be overridden here.
    for source in sources:
        if isinstance(source, dict):
            source["schema"] = "main"
            source.pop("database", None)

    # Write back as valid YAML — reformats the file but preserves all dbt-relevant structure
    output = yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True)
    sources_yml_path.write_text(output, encoding="utf-8")
    print(f"  Injected schema: main into {sources_yml_path.name}")
    return True


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def stage_lint(project_root: Path, domains: list[str]) -> StageResult:
    """
    Stage 1: Lint all contracts for the specified domains.

    This is the earliest shift-left check. If a contract is syntactically
    invalid, there is no point exporting or building anything from it.

    Grounded in: shift-left validation,
    contract as single source of truth -- must be valid first
    """
    print_stage_header(1, "Lint Contracts")
    start = time.time()

    all_passed = True
    details_parts = []

    for domain in domains:
        config = DOMAINS[domain]
        for role, contract_path in config["contracts"].items():
            full_path = project_root / contract_path
            label = f"{domain}/{role} ({full_path.name})"
            print(f"\n  Linting {label}...")

            result = run_cmd(
                ["uv", "run", "python", "-m", "datacontract.cli", "lint", str(full_path)],
                cwd=project_root,
            )

            if result.returncode == 0:
                print_result(True, label)
                details_parts.append(f"{label}: passed")
            else:
                print_result(False, label)
                print(f"  STDERR: {result.stderr.strip()}")
                details_parts.append(f"{label}: FAILED")
                all_passed = False

    duration = time.time() - start
    return StageResult(
        name="Lint",
        success=all_passed,
        duration_seconds=duration,
        details="; ".join(details_parts),
    )


def stage_export(project_root: Path, domains: list[str]) -> StageResult:
    """
    Stage 2: Export dbt sources, staging SQL, and Soda checks from contracts.

    This stage regenerates all derived configuration from the contracts,
    proving the contract is the single source of truth. Every run overwrites
    the previous exports -- ensuring reproducibility.

    Post-export, we inject schema: main into sources.yml files (STATE.md #3).

    The staging SQL export requires --schema-name when a contract has
    multiple schemas (entities). We parse the contract YAML to discover
    schemas dynamically, then export each one individually.

    Grounded in: generate everything from the contract
    """
    print_stage_header(2, "Export Artifacts")
    start = time.time()

    all_passed = True
    details_parts = []

    for domain in domains:
        config = DOMAINS[domain]
        source_role = config["export_source"]
        contract_path = project_root / config["contracts"][source_role]

        print(f"\n  --- Domain: {domain} ---")
        print(f"  Source contract: {contract_path.name} (role: {source_role})")

        # ----- Export 1: dbt-sources -----
        sources_out = project_root / f"dbt/models/sources/{domain}/sources.yml"
        sources_out.parent.mkdir(parents=True, exist_ok=True)
        print(f"\n  Exporting dbt-sources -> {sources_out.relative_to(project_root)}")

        result = run_cmd(
            [
                "uv",
                "run",
                "python",
                "-m",
                "datacontract.cli",
                "export",
                str(contract_path),
                "--format",
                "dbt-sources",
                "--server",
                "local-duckdb",
            ],
            cwd=project_root,
        )

        if result.returncode == 0 and result.stdout.strip():
            sources_out.write_text(result.stdout, encoding="utf-8")
            print_result(True, "dbt-sources exported")

            # Post-export: inject schema: main
            inject_schema_main(sources_out)
            details_parts.append(f"{domain}/dbt-sources: passed")
        else:
            print_result(False, "dbt-sources export failed")
            print(f"  STDERR: {result.stderr.strip()}")
            details_parts.append(f"{domain}/dbt-sources: FAILED")
            all_passed = False

        # ----- Export 2: dbt-staging-sql (per schema) -----
        staging_dir = project_root / f"dbt/models/staging/{domain}"
        staging_dir.mkdir(parents=True, exist_ok=True)

        schemas = get_schema_names(contract_path)
        print(
            f"\n  Exporting dbt-staging-sql -> {staging_dir.relative_to(project_root)}/"
        )
        print(f"  Schemas found in contract: {schemas}")

        staging_ok = True
        for schema_name in schemas:
            result = run_cmd(
                [
                    "uv",
                    "run",
                    "python",
                    "-m",
                    "datacontract.cli",
                    "export",
                    str(contract_path),
                    "--format",
                    "dbt-staging-sql",
                    "--schema-name",
                    schema_name,
                    "--server",
                    "local-duckdb",
                ],
                cwd=project_root,
            )

            if result.returncode == 0 and result.stdout.strip():
                out_file = staging_dir / f"stg_{schema_name}.sql"
                out_file.write_text(result.stdout, encoding="utf-8")
                print(f"    Written: stg_{schema_name}.sql")
            else:
                print(f"    FAILED: stg_{schema_name}.sql")
                print(f"    STDERR: {result.stderr.strip()}")
                staging_ok = False

        if staging_ok:
            print_result(True, f"dbt-staging-sql exported ({len(schemas)} models)")
            details_parts.append(f"{domain}/dbt-staging-sql: passed")
        else:
            print_result(False, "dbt-staging-sql export had failures")
            details_parts.append(f"{domain}/dbt-staging-sql: FAILED")
            all_passed = False

        # ----- Export 3: sodacl -----
        soda_out = project_root / config["soda_checks"]
        soda_out.parent.mkdir(parents=True, exist_ok=True)
        print(f"\n  Exporting sodacl -> {soda_out.relative_to(project_root)}")

        result = run_cmd(
            [
                "uv",
                "run",
                "python",
                "-m",
                "datacontract.cli",
                "export",
                str(contract_path),
                "--format",
                "sodacl",
                "--server",
                "local-duckdb",
            ],
            cwd=project_root,
        )

        if result.returncode == 0 and result.stdout.strip():
            soda_out.write_text(result.stdout, encoding="utf-8")
            print_result(True, "sodacl exported")
            details_parts.append(f"{domain}/sodacl: passed")
        else:
            print_result(False, "sodacl export failed")
            print(f"  STDERR: {result.stderr.strip()}")
            details_parts.append(f"{domain}/sodacl: FAILED")
            all_passed = False

    duration = time.time() - start
    return StageResult(
        name="Export",
        success=all_passed,
        duration_seconds=duration,
        details="; ".join(details_parts),
    )


def stage_dbt_build(
    project_root: Path,
    domains: list[str],
    skip_data: bool,
) -> StageResult:
    """
    Stage 3: Run dbt seed (optional) + dbt build scoped to domains.

    The dbt build command runs source tests, creates staging views, and
    materializes marts with contract enforcement -- all in dependency order.

    Domain scoping uses the '+' operator on the mart path, which tells dbt:
    "build this model AND all its upstream dependencies." This correctly
    handles cross-domain consumption (e.g., playback consuming content
    staging models via ref()).

    Grounded in:
    - domain ownership -- each domain owns its mart
    - circuit breaker -- if dbt fails, data is not materialized,
      so Soda scans would produce misleading results
    - source test failure causes downstream SKIP
    """
    print_stage_header(3, "dbt Build")
    start = time.time()

    dbt_dir = project_root / "dbt"
    details_parts = []

    # Step 3a: dbt seed (unless --skip-data)
    if not skip_data:
        print("\n  Running dbt seed...")
        result = run_cmd(
            ["uv", "run", "python", "-m", "dbt.cli.main", "seed", "--profiles-dir", "."],
            cwd=dbt_dir,
        )
        if result.returncode != 0:
            print_result(False, "dbt seed failed")
            print(f"  STDOUT:\n{result.stdout[-500:]}")
            print(f"  STDERR:\n{result.stderr[-500:]}")
            return StageResult(
                name="dbt Build",
                success=False,
                duration_seconds=time.time() - start,
                details="dbt seed FAILED",
            )
        print_result(True, "dbt seed complete")
        details_parts.append("seed: passed")
    else:
        print("\n  Skipping dbt seed (--skip-data)")
        details_parts.append("seed: skipped")

    # Step 3b: dbt build with domain scoping
    #
    # We use the '+' prefix on the mart path to include upstream deps.
    # Example for playback:
    #   dbt build --select +models/marts/playback/
    # This builds:
    #   1. Any source tests for sources consumed by playback staging
    #   2. Staging views consumed by the playback mart
    #   3. The playback mart itself (with contract enforcement)
    #   4. Tests on the mart
    #
    # For cross-domain deps (fct_play_events refs content staging),
    # the '+' operator traverses the ref() DAG edge and includes
    # content staging models automatically.

    # Build the --select arguments: one '+path' per domain mart folder
    select_args = []
    for domain in domains:
        mart_path = f"models/marts/{domain}/"
        select_args.extend(["--select", f"+{mart_path}"])

    print(f"\n  Running dbt build with selection: {' '.join(select_args)}")

    result = run_cmd(
        ["uv", "run", "python", "-m", "dbt.cli.main", "build", "--profiles-dir", "."] + select_args,
        cwd=dbt_dir,
    )

    # Parse dbt output for pass/fail/warn/error/skip counts
    # dbt outputs a summary line like: "Done. PASS=61 WARN=0 ERROR=0 SKIP=0 TOTAL=61"
    dbt_summary = ""
    if result.stdout:
        # Print the last 20 lines for visibility
        output_lines = result.stdout.strip().split("\n")
        print("\n  dbt output (last 20 lines):")
        for line in output_lines[-20:]:
            print(f"    {line}")

        # Extract summary line
        for line in output_lines:
            if "PASS=" in line or "Done." in line:
                dbt_summary = line.strip()

    if result.returncode == 0:
        print_result(True, f"dbt build complete -- {dbt_summary}")
        details_parts.append(f"build: passed ({dbt_summary})")
        success = True
    else:
        # dbt returns non-zero if any test fails (including expected failures).
        # We still report it but let the user see the details.
        # The L008 email null IS an expected failure -- dbt will report ERROR=1.
        #
        # Decision: We treat dbt non-zero as a FAILURE for circuit-breaking.
        # The user can inspect the output to decide if it is acceptable.
        # In a production pipeline, you would distinguish expected vs unexpected.
        print_result(False, f"dbt build had failures -- {dbt_summary}")
        if result.stderr:
            print(f"  STDERR:\n{result.stderr[-500:]}")
        details_parts.append(f"build: FAILED ({dbt_summary})")
        success = False

    duration = time.time() - start
    return StageResult(
        name="dbt Build",
        success=success,
        duration_seconds=duration,
        details="; ".join(details_parts),
    )


def stage_soda_scan(project_root: Path, domains: list[str]) -> StageResult:
    """
    Stage 4: Run Soda scans for the specified domains.

    Soda catches semantic violations that dbt source tests do not cover:
    - Invalid enum values (e.g., tier=invalid_tier in subscriptions)
    - Out-of-range values (e.g., duration_seconds=-30 in tracks)

    These checks are generated from the contract's quality rules via
    the sodacl export in Stage 2.

    Grounded in:
    - layered testing -- Soda as the semantic quality layer
    - complementary validation from the same contract
    - Soda DuckDB adapter quirks (schema handling)
    """
    print_stage_header(4, "Soda Scan")
    start = time.time()

    soda_dir = project_root / "soda"
    all_passed = True
    details_parts = []

    for domain in domains:
        config = DOMAINS[domain]
        checks_file = project_root / config["soda_checks"]

        if not checks_file.exists():
            print(f"\n  WARNING: {checks_file} not found, skipping {domain}")
            details_parts.append(f"{domain}: SKIPPED (no checks file)")
            continue

        print(f"\n  Scanning {domain}...")

        result = run_cmd(
            [
                "uv",
                "run",
                "python",
                "-m",
                "soda",
                "scan",
                "-d",
                "melodify",
                "-c",
                "soda/configuration.yml",
                str(checks_file),
            ],
            cwd=project_root,
        )

        # Print Soda output for visibility
        if result.stdout:
            output_lines = result.stdout.strip().split("\n")
            print("\n  Soda output (last 15 lines):")
            for line in output_lines[-15:]:
                print(f"    {line}")

        # Soda exit codes:
        # 0 = all checks passed
        # 1 = check failures or warnings
        # 2 = errors (connection issues, etc.)
        if result.returncode == 0:
            print_result(True, f"{domain} scan passed")
            details_parts.append(f"{domain}: passed")
        else:
            print_result(False, f"{domain} scan had failures")
            details_parts.append(f"{domain}: FAILED")
            all_passed = False

    duration = time.time() - start
    return StageResult(
        name="Soda Scan",
        success=all_passed,
        duration_seconds=duration,
        details="; ".join(details_parts),
    )


def stage_summary(pipeline_result: PipelineResult):
    """
    Stage 5: Print the pipeline summary report.

    This is the fast-feedback output that tells you at a glance
    what passed and what failed, and where the pipeline halted.

    Grounded in: fast feedback from CI/CD integration
    """
    print(f"\n{'='*60}")
    print(f"  PIPELINE SUMMARY")
    print(f"{'='*60}\n")

    total_duration = sum(s.duration_seconds for s in pipeline_result.stages)

    for stage in pipeline_result.stages:
        if stage.skipped:
            status = "SKIP"
        elif stage.success:
            status = "PASS"
        else:
            status = "FAIL"

        print(f"  [{status}] {stage.name:<15} ({stage.duration_seconds:.1f}s)")
        if stage.details:
            print(f"         {stage.details}")

    print(f"\n  Total duration: {total_duration:.1f}s")

    if pipeline_result.halted_at:
        display_name = STAGES[pipeline_result.halted_at]
        print(f"\n  PIPELINE HALTED at stage: {display_name}")
        print(f"  Reason: Circuit breaker triggered -- downstream stages skipped.")
        print(f"  (Stop pipeline when quality thresholds are not met)")

    if pipeline_result.all_passed:
        print(f"\n  RESULT: ALL STAGES PASSED")
    else:
        print(f"\n  RESULT: PIPELINE FAILED")

    print(f"\n{'='*60}")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Melodify Data Contracts POC -- Pipeline Orchestrator",
        epilog=(
            "Examples:\n"
            "  python scripts/run_pipeline.py                     # all domains\n"
            "  python scripts/run_pipeline.py --domain listeners  # single domain\n"
            "  python scripts/run_pipeline.py --skip-data         # skip dbt seed\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--domain",
        choices=list(DOMAINS.keys()),
        help="Run pipeline for a single domain only. Default: all domains.",
    )
    parser.add_argument(
        "--skip-data",
        action="store_true",
        help="Skip dbt seed step (assume seeds already loaded).",
    )
    parser.add_argument(
        "--expect-failure-at",
        choices=list(STAGES.keys()),
        metavar="STAGE",
        help=(
            "Negative-test mode. Assert the pipeline halts at the given "
            "stage. Exit 0 if it halts there, exit 20 if it completes "
            "successfully (regression), exit 21 if it halts at a "
            "different stage. Use only for domains with seeded failures."
        ),
    )

    args = parser.parse_args()

    # Determine which domains to process
    domains = [args.domain] if args.domain else list(DOMAINS.keys())

    # Determine project root.
    # The script lives in scripts/, so project root is one level up.
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent

    print(f"Melodify Data Contracts POC -- Pipeline Run")
    print(f"Domains: {', '.join(domains)}")
    print(f"Skip data: {args.skip_data}")
    print(f"Project root: {project_root}")

    if args.expect_failure_at:
        print(f"\n{'='*64}")
        print(f"  NEGATIVE TEST MODE: expecting halt at stage '{args.expect_failure_at}'")
        print(f"{'='*64}")

    pipeline = PipelineResult()

    def finish(pipeline: PipelineResult):
        """
        Print stage_summary and resolve the final exit code.

        Called at every exit point (early halt or clean completion) so that
        --expect-failure-at inversion is applied consistently regardless of
        which stage tripped the circuit breaker.
        """
        stage_summary(pipeline)
        if args.expect_failure_at:
            expected = args.expect_failure_at
            if pipeline.halted_at == expected:
                print(f"\n  NEGATIVE TEST: PASS (halted at expected stage '{expected}')")
                sys.exit(0)
            elif pipeline.halted_at is None:
                print(f"\n  NEGATIVE TEST: FAIL (expected halt at '{expected}', "
                      f"but pipeline completed successfully — seeded violation "
                      f"may have been masked)")
                sys.exit(20)
            else:
                print(f"\n  NEGATIVE TEST: FAIL (expected halt at '{expected}', "
                      f"but halted at '{pipeline.halted_at}')")
                sys.exit(21)
        if pipeline.halted_at:
            sys.exit(EXIT_CODES[pipeline.halted_at])
        sys.exit(0)

    # --- Stage 1: Lint ---
    result = stage_lint(project_root, domains)
    pipeline.add(result)
    if not result.success:
        pipeline.halted_at = "lint"
        # Mark remaining stages as skipped
        for name in ["Export", "dbt Build", "Soda Scan"]:
            pipeline.add(
                StageResult(name=name, success=False, duration_seconds=0, skipped=True)
            )
        finish(pipeline)

    # --- Stage 2: Export ---
    result = stage_export(project_root, domains)
    pipeline.add(result)
    if not result.success:
        pipeline.halted_at = "export"
        for name in ["dbt Build", "Soda Scan"]:
            pipeline.add(
                StageResult(name=name, success=False, duration_seconds=0, skipped=True)
            )
        finish(pipeline)

    # --- Stage 3: dbt Build ---
    result = stage_dbt_build(project_root, domains, args.skip_data)
    pipeline.add(result)
    if not result.success:
        pipeline.halted_at = "dbt_build"
        pipeline.add(
            StageResult(
                name="Soda Scan", success=False, duration_seconds=0, skipped=True
            )
        )
        finish(pipeline)

    # --- Stage 4: Soda Scan ---
    result = stage_soda_scan(project_root, domains)
    pipeline.add(result)
    if not result.success:
        pipeline.halted_at = "soda_scan"

    # --- Stage 5: Summary + exit ---
    finish(pipeline)


if __name__ == "__main__":
    main()
