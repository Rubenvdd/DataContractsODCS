# Melodify - Data Contracts ODCS Meetup demo

A proof-of-concept demonstrating **contract-driven data pipelines** using the Open Data Contract Standard (ODCS v3.1.0), dbt, Soda Core, and DuckDB.

Built for a meetup talk on how data contracts can shift data quality left and serve as the single source of truth for pipeline configuration.

## What this POC demonstrates

**One contract, multiple outputs.** Each domain has an ODCS contract that generates dbt source definitions, staging SQL, and Soda quality checks. Change the contract, re-export, and the pipeline updates automatically.

**Layered testing from a single source.** dbt source tests catch structural violations (nulls, duplicates, types). Soda catches semantic violations (invalid enums, out-of-range values). Both layers are generated from the same contract.

**Circuit-breaking orchestration.** A Python orchestrator chains lint, export, dbt build, and Soda scan with hard stops — if any stage fails, downstream stages are skipped to prevent bad data from flowing further.

**Domain ownership.** Three domains (listeners, content, playback) each own their mart. Cross-domain consumption is explicit: the playback domain consumes content staging models through dbt's `ref()`.

## Architecture

```
contracts/*.odcs.yaml          -- ODCS v3.1.0 (single source of truth)
        |
        v
  datacontract CLI exports     -- generates dbt sources, staging SQL, Soda checks
        |
        v
  dbt build                   -- seeds -> source tests -> staging views -> marts
        |                        (contract: enforced: true on marts)
        v
  Soda scan                   -- semantic quality checks from contract
        |
        v
  Summary report              -- pass/fail per stage
```

## Three domains

| Domain | Contract | Mart | Key concept |
|--------|----------|------|-------------|
| **Listeners** | Inbound contract (split pattern) | `dim_listeners` | Two-contract pattern: inbound for source, outbound for mart (Phase 3) |
| **Content** | Single contract (both roles) | `dim_tracks` | Within-domain joins: tracks + artists + albums |
| **Playback** | Single contract (both roles) | `fct_play_events` | Cross-domain consumption: playback consumes content |

## Intentional data quality issues

Four issues are planted in the seed data to demonstrate which layer catches what:

| Issue | What's wrong | dbt catches it? | Soda catches it? |
|-------|-------------|-----------------|------------------|
| T022 | Negative track duration | No | Yes — `quality_sql` |

## Quick start

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/)

```bash
# Clone and install
git clone <repo-url>
cd "Open data contracts"
uv sync

# Run the full pipeline for a single domain
uv run python scripts/run_pipeline.py --domain content

# Run all domains
uv run python scripts/run_pipeline.py

# Skip seed loading (faster reruns)
uv run python scripts/run_pipeline.py --skip-data
```

### Demo sequence for the meetup

```bash
# 1. Happy path — content passes dbt, Soda catches T022
uv run python scripts/run_pipeline.py --domain content

# 2. Circuit breaker — listeners fails at dbt on L008
uv run python scripts/run_pipeline.py --domain listeners --skip-data

# 3. Cross-domain — playback pulls in content deps automatically
uv run python scripts/run_pipeline.py --domain playback --skip-data
```

## Project structure

```
contracts/                    ODCS v3.1.0 contracts (source of truth)
  listeners/inbound.odcs.yaml
  content/datacontract.odcs.yaml
  playback/datacontract.odcs.yaml

dbt/                          dbt project
  seeds/                      7 CSV files with intentional issues
  models/
    sources/                  Generated from contracts
    staging/                  Generated from contracts
    marts/                    Hand-written, contract: enforced: true

soda/                         Soda Core
  configuration.yml           DuckDB connection
  checks/                     Generated from contracts

scripts/
  run_pipeline.py             Orchestrator (lint > export > build > scan)
```

## Tools and versions

| Tool | Version |
|------|---------|
| datacontract CLI | 0.11.6 |
| dbt-core | 1.11.8 |
| dbt-duckdb | 1.10.1 |
| Soda Core | 3.5.6 |
| Python | 3.11 (via uv) |
