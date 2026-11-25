# TiC Developer Guide and Architecture

This guide describes the Transparency in Coverage (TiC) repository: how data
moves through the pipeline, where its responsibilities live, how to work on
the code, and what each tracked file is for. It covers TiC only; the sibling
Benchmarks dashboard has its own [runbook](../Benchmarks/RUNBOOK.md).

## What this project does

The project discovers, downloads, parses, enriches, validates, and aggregates
hospital price-transparency data from payer-published Machine-Readable Files
(MRFs). It is designed to process large JSON files as a stream rather than
loading an entire file into memory.

The main stages are:

1. **Discover source files.** `fetch_uhc_index.py` crawls UHC's two-level blob
   index and writes matching rate-file URLs to CSV.
   `fetch_cigna_index.py` extracts rate-file URLs from a local Cigna CMS index
   and writes a runner-compatible JSON manifest. The Cigna size-checking tools
   can optionally measure and sort the remote files.
2. **Ingest rates.** `runner.py` downloads and processes manifest entries;
   entries with `size_bytes` metadata are sorted smallest-first. `runnerMulti.py`
   overlaps downloads by prefetching upcoming files. Both use `ingest_utils.py`
   for manifest validation, atomic downloads, retries, checksums, and JSONL
   run events.
3. **Parse and persist.** `stream_parser.py` streams each MRF into
   `transparency.duckdb` using `schema.sql`. Each file is handled in a
   transaction and rate rows are inserted in bounded batches. Only
   `billing_class = 'institutional'` prices are retained.
4. **Enrich.** The loader scripts populate `enrichment.duckdb` with provider,
   geographic, and code-reference data using `schema_enrichment.sql`.
5. **Build and validate.** `build_benchmarks.py` joins the two databases and
   materializes query-ready benchmark tables. `validate_data.py` checks the
   transparency database for structural and data-quality problems.

```text
UHC index ──▶ fetch_uhc_index.py ────────┐
                                         ├─▶ runner.py / runnerMulti.py
Cigna index ─▶ fetch_cigna_index.py ─────┘          │
                                                    ▼
                                           stream_parser.py
                                                    │
                                             transparency.duckdb
                                                    │
NPPES / ZIP / code references ──▶ enrichment.duckdb
                                                    │
                                                    ▼
                                          build_benchmarks.py
                                                    │
                                     benchmark and aggregate tables
```

`fetch_uhc_index.py` produces CSV, while the runners consume JSON manifests.
The conversion step and complete operational commands are documented in the
[Benchmarks runbook](../Benchmarks/RUNBOOK.md).

## Data and database layout

### `transparency.duckdb`

Created and populated by the parser and runners. Its core tables, defined in
[`schema.sql`](./schema.sql), are:

- `payers`: plan and reporting-entity metadata, one row per source-file plan.
- `billing_codes`: billing-code metadata and negotiation arrangement.
- `negotiated_rates`: institutional price facts, linked to payer and code IDs.
- `providers`: provider-group-to-NPI/TIN mappings. A group can have multiple
  rows, one per NPI (or a row with a null NPI when none is listed). Inline
  groups without a CMS ID receive a stable synthetic negative reference ID.
- `ingested_files`: source-file ledger used to prevent duplicate ingestion.
- `procedure_price_stats` and `procedure_price_by_payer`: basic SQL views
  available before the larger benchmark build.

The parser uses `provider_reference_ids` on each rate to connect rate rows to
`providers`. CMS inputs can omit fields such as `setting`; downstream code
should treat those values as nullable. The parser intentionally excludes
professional-class prices.

### `enrichment.duckdb`

Created and refreshed separately so reference data can be updated without
re-ingesting rate files. [`schema_enrichment.sql`](./schema_enrichment.sql)
declares:

- `nppes`: provider identities and normalized ZIP codes from the CMS NPPES
  registry.
- `zip_county`: ZIP-to-county relationships, including ZIPs that span more
  than one county.
- `code_descriptions`: canonical billing-code descriptions.
- `taxonomy_descriptions`: taxonomy-to-specialty descriptions.

`load_nppes.py`, `load_zip_county.py`, and `load_code_descriptions.py` rebuild
their respective tables. `load_code_descriptions.py` currently creates
`code_descriptions` with `billing_code`, `billing_code_type`, and `description`;
that differs from the longer column layout declared in
`schema_enrichment.sql`. Check both the loader and existing database before
changing this schema. There is no taxonomy-description loader in this
repository yet.

### Benchmark output

[`build_benchmarks.py`](./build_benchmarks.py) attaches the enrichment database
read-only and writes its output to `transparency.duckdb`. Its main
`benchmarks` table combines rate, payer, code, provider, and geographic data.
It also builds `benchmarks_code_stats`, `benchmarks_geo_stats`,
`benchmarks_geo_payer_stats`, `benchmarks_payer_stats`,
`benchmarks_payer_provider_stats`, and `benchmarks_provider_stats`.

The build selects one representative NPI per provider reference for the main
benchmark join, and uses the dominant county for each ZIP. These choices make
the aggregate joins practical; they do not represent every NPI or every
county associated with a provider or ZIP. The build filters out non-positive,
very large, and unsupported negotiated-rate rows. Use `--drop-first` when the
main `benchmarks` table must be rebuilt from scratch.

## Working on the project

The CI workflow targets Python 3.11. From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
ruff check .
python -m compileall -q .
python scripts/check_generated_files.py
```

`requirements-dev.txt` installs the runtime requirements plus Ruff, pytest,
and coverage. The configured CI test command is `unittest`; pytest and
coverage are available for local development.

### Useful entry points

Run one local MRF directly:

```bash
python stream_parser.py path/to/file_in-network-rates.json.gz
```

Preview a manifest without downloading or writing to the database:

```bash
python runner.py --manifest path/to/runner_manifest.json --dry-run --max-files 5
```

Process a small batch:

```bash
python runner.py --manifest path/to/runner_manifest.json --max-files 5
```

Validate a database:

```bash
python validate_data.py --db transparency.duckdb --fast --fail-on warn
```

Build benchmark tables after both databases are ready:

```bash
python build_benchmarks.py --drop-first
```

The scripts accept additional path and performance options; use `--help` for
the current command-line interface. A full payer-to-dashboard workflow,
including reference-data inputs and UHC manifest conversion, is in the
[Benchmarks runbook](../Benchmarks/RUNBOOK.md).

### Operational behavior to preserve

- The parsers and runners are idempotent by source-file label through
  `ingested_files`. Do not remove or bypass that ledger without replacing its
  duplicate-prevention behavior.
- `runner.py` and `runnerMulti.py` use separate progress files by default.
  When changing a manifest or starting a separate payer run, use a distinct
  progress path so a prior run's index is not reused accidentally. Entries
  with size metadata are sorted smallest-first before progress is applied.
- `runnerMulti.py` downloads ahead of parsing; keep DuckDB writes in the main
  processing loop. DuckDB permits only one writer per database file.
- `schema.sql` is the high-throughput ingestion schema. `schema_final.sql` is
  an optional post-ingestion optimization step; do not apply it while
  ingestion is still running.
- Reference loaders replace their target tables. Confirm the selected input
  files and database paths before running a refresh.
- `.gitignore` excludes local databases, downloads, generated manifests,
  progress files, logs, reports, and `reference_data/`. Keep these generated
  or bulky data files out of commits. The CI artifact check enforces this for
  tracked files.

## File-by-file guide

The inventory below covers every file tracked in this TiC repository. Runtime
databases, downloaded source files, generated manifests, caches, and reports
are local data artifacts rather than source files; their ignore rules are in
`.gitignore`.

### Repository and documentation

| File | Purpose |
| --- | --- |
| [`.gitattributes`](./.gitattributes) | Assigns Git LFS attributes to DuckDB files. Generated databases are still excluded from commits by the artifact check. |
| [`.gitignore`](./.gitignore) | Excludes virtual environments, caches, databases, downloads, reference data, generated manifests, progress files, logs, and reports. |
| [`README.md`](./README.md) | Project introduction, installation, common commands, and links to the operational runbook and this guide. |
| [`architecture.md`](./architecture.md) | This developer guide: pipeline architecture, data model, workflows, and file inventory. |
| [`RECENT_COMMITS.md`](./RECENT_COMMITS.md) | Historical summary of a specific November 2026 set of commits; not a complete or continuously updated changelog. |
| [`TODO.md`](./TODO.md) | Current project backlog for reference-data coverage and engineering follow-up. |

### Ingestion and discovery

| File | Purpose |
| --- | --- |
| [`fetch_uhc_index.py`](./fetch_uhc_index.py) | Crawls UHC's top-level blob list and employer indexes, then writes matching rate-file URLs and plan metadata to CSV. `--limit 0` processes all top-level entries. |
| [`fetch_cigna_index.py`](./fetch_cigna_index.py) | Extracts and de-duplicates Cigna in-network-rate URLs from a local CMS index and writes the JSON `blobs` manifest used by the runners. |
| [`check_cigna_file_sizes.py`](./check_cigna_file_sizes.py) | Checks remote Cigna manifest entries with HTTP HEAD or a one-byte range request and writes a size/status report. It does not download the full rate files. |
| [`create_size_sorted_manifest.py`](./create_size_sorted_manifest.py) | Converts a size report into a compact runner manifest ordered from smallest to largest file. |
| [`runner.py`](./runner.py) | Sequential manifest processor: validates entries, initializes the schema, downloads files, calls the parser, records progress/run events, and removes successful temporary downloads. |
| [`runnerMulti.py`](./runnerMulti.py) | Alternative runner that prefetches upcoming downloads concurrently while parsing and writing files sequentially. |
| [`ingest_utils.py`](./ingest_utils.py) | Shared manifest validation, retry policy, atomic file download, expected-size validation, SHA-256 calculation, and JSONL run logging. |
| [`stream_parser.py`](./stream_parser.py) | Streaming MRF parser and single-file CLI. Parses JSON, gzip, and local ZIP inputs into DuckDB in batches, storing institutional prices and provider mappings. |

### Enrichment, build, and validation

| File | Purpose |
| --- | --- |
| [`load_nppes.py`](./load_nppes.py) | Rebuilds the NPPES table from a CMS CSV, normally filtering to NPIs already present in `transparency.duckdb`. |
| [`load_zip_county.py`](./load_zip_county.py) | Downloads or reads the Census ZIP/ZCTA-to-county relationship file and rebuilds the enrichment database's ZIP/county table. |
| [`load_code_descriptions.py`](./load_code_descriptions.py) | Parses CMS reference files and embedded revenue-code definitions, then rebuilds canonical billing-code descriptions. |
| [`build_benchmarks.py`](./build_benchmarks.py) | Joins transparency and enrichment data, creates the main benchmark table and grouped statistics, and supports rebuild/performance options. |
| [`validate_data.py`](./validate_data.py) | Read-only structural, referential, completeness, domain, format, duplicate, and coverage checks for the transparency database; can emit text and JSON reports. |

### Schemas and configuration

| File | Purpose |
| --- | --- |
| [`schema.sql`](./schema.sql) | Idempotent high-throughput schema for the transparency database, including payer, code, rate, provider, and ingestion-ledger tables plus basic views. |
| [`schema_enrichment.sql`](./schema_enrichment.sql) | Schema for NPPES, ZIP/county, code-description, and taxonomy-description reference tables in the enrichment database. |
| [`schema_final.sql`](./schema_final.sql) | Optional post-ingestion indexes, compression, and checkpoint commands for the transparency database. |
| [`pyproject.toml`](./pyproject.toml) | Ruff settings and local pytest/coverage configuration. |
| [`requirements.txt`](./requirements.txt) | Pinned runtime Python dependencies. |
| [`requirements-dev.txt`](./requirements-dev.txt) | Development dependency additions for tests, linting, and coverage. |
| [`.github/workflows/ci.yml`](./.github/workflows/ci.yml) | GitHub Actions checks on pushes and pull requests: install dependencies, reject tracked generated artifacts, compile Python, run unittest, and run Ruff. |

### Tests and repository checks

| File | Purpose |
| --- | --- |
| [`tests/test_pipeline.py`](./tests/test_pipeline.py) | Unit tests for Cigna manifest filtering, JSON serialization, manifest validation, atomic download/retry behavior, and immediate handling of non-retryable HTTP errors. |
| [`tests/test_stream_parser.py`](./tests/test_stream_parser.py) | In-memory DuckDB test for institutional-only parsing, nullable `setting`, and duplicate-ingestion prevention. |
| [`tests/fixtures/sample_mrf.json`](./tests/fixtures/sample_mrf.json) | Small synthetic MRF used by the parser test; includes provider references, inline provider groups, institutional and professional rates, and a missing setting. |
| [`scripts/check_generated_files.py`](./scripts/check_generated_files.py) | CI guard that fails if generated databases, archives, reports, or named generated outputs are tracked by Git. |

## Changing the pipeline safely

When modifying ingestion or schema behavior:

1. Update the relevant schema and parser/loader together, preserving
   compatibility with existing DuckDB files where practical.
2. Add or adjust a focused test using the synthetic fixture or mocked HTTP
   responses; tests must not require live payer endpoints or large reference
   datasets.
3. Run the test suite, compilation check, generated-artifact check, and Ruff
   commands listed above.
4. Update this guide and the README when entry points, database layouts, or
   operational assumptions change.
