# Recent Commit Changes

This document covers commits created from **2026-11-04 through 2026-11-09**. The repository history contains 17 commits in that period; all were created on 2026-11-06.

## Commit summary

### `b02a86e` — Refresh project TODO

- Replaced the previous long-form TODO list with a shorter project plan.
- Added data-quality tasks for CMS reference files, code-description loading, benchmark rebuilding, and dashboard re-exporting.
- Added engineering tasks for tests, CI, and keeping generated data out of version control.
- Changed file: [`TODO.md`](./TODO.md).

### `7b2bab0` — Add ingestion pipeline tests

- Created [`tests/test_pipeline.py`](./tests/test_pipeline.py).
- Added tests for:
  - Cigna manifest filtering to in-network-rates files.
  - Duplicate manifest-file removal.
  - Atomic download writing and retry behavior.
  - JSON serialization of generated manifests.
  - Validation errors for malformed manifest entries.
- Added fake HTTP response and raw-stream helpers for deterministic download tests.

### `bf659ba` — Add Python tooling configuration

- Created [`pyproject.toml`](./pyproject.toml).
- Configured Ruff with a 100-character line limit.
- Configured Ruff to target Python 3.11.
- Excluded generated/build directories and selected operational files from linting.

### `11762b68` — Pin development dependencies

- Created [`requirements-dev.txt`](./requirements-dev.txt).
- Added pinned development dependencies for Ruff, pytest, and coverage tooling.

### `c8d6dab` — Use structured logging in Cigna tools

- Updated [`check_cigna_file_sizes.py`](./check_cigna_file_sizes.py), [`create_size_sorted_manifest.py`](./create_size_sorted_manifest.py), and [`fetch_cigna_index.py`](./fetch_cigna_index.py).
- Replaced ad-hoc progress/error output with the standard logging module.
- Added consistent informational and warning messages.
- Preserved the existing Cigna manifest and file-size processing behavior.

### `9a59b04` — Validate ingestion manifests

- Added `validate_manifest()` to [`ingest_utils.py`](./ingest_utils.py).
- Validated that manifests are objects containing a `blobs` or `files` list.
- Validated URL entries, object entries, required `downloadUrl` values, and non-negative integer `size_bytes` values.
- Updated [`runner.py`](./runner.py) and [`runnerMulti.py`](./runnerMulti.py) to validate manifests before processing.
- Extended [`tests/test_pipeline.py`](./tests/test_pipeline.py) with invalid-manifest coverage.

### `24f90da` — Add ingestion dry-run mode

- Added `--dry-run` to [`runner.py`](./runner.py) and [`runnerMulti.py`](./runnerMulti.py).
- Dry-run mode validates and lists manifest entries without initializing the database, downloading files, or writing ingestion data.
- Added support for displaying the configured file limit and manifest positions during dry runs.

### `cec0fe7` — Document development workflow

- Added a development workflow section to [`README.md`](./README.md).
- Documented environment setup, dependency installation, test execution, linting, CI expectations, and generated-file checks.

### `dea5716` — Add generated artifact guard

- Created [`scripts/check_generated_files.py`](./scripts/check_generated_files.py).
- Added checks that reject tracked generated artifacts such as databases, downloaded archives, reports, dashboard exports, and temporary outputs.
- Added actionable failure output identifying disallowed tracked files.

### `25ece96` — Add continuous integration workflow

- Created [`.github/workflows/ci.yml`](./.github/workflows/ci.yml).
- Configured CI to run on pushes and pull requests.
- Added a Python setup step and dependency installation.
- Added Ruff checks, unit-test execution, and generated-artifact validation.

### `718dad9` — Update README workflow documentation

- Expanded [`README.md`](./README.md) with operational workflow documentation.
- Documented UHC and Cigna index discovery, manifest filtering, validation, dry-run usage, single- and multi-runner execution, progress tracking, and generated outputs.
- Added examples for running the pipeline and checking generated artifacts.

### `dbc020a` — Prevent duplicate file ingestion

- Added the `ingested_files` table to [`schema.sql`](./schema.sql), keyed by `source_file`, with `rows_kept` and `loaded_at`.
- Updated [`stream_parser.py`](./stream_parser.py) to:
  - Check for an existing source file inside the transaction.
  - Print a skip message and return for already-ingested files.
  - Insert the source-file record with the kept-row count before committing.
- Updated [`runner.py`](./runner.py) and [`runnerMulti.py`](./runnerMulti.py) to skip files already recorded in `ingested_files`, rather than relying only on `processed_count.txt`.

### `c231322` — Add stream parser coverage

- Created [`tests/fixtures/sample_mrf.json`](./tests/fixtures/sample_mrf.json), a small MRF fixture containing:
  - A top-level `provider_references` entry.
  - Embedded `provider_groups`.
  - Institutional prices.
  - A professional price that should be dropped.
  - An institutional price without `setting`.
- Created [`tests/test_stream_parser.py`](./tests/test_stream_parser.py).
- Added an in-memory DuckDB test that executes [`schema.sql`](./schema.sql), processes the fixture, verifies institutional-only rows and `NULL` settings, and verifies that processing the same file twice does not add rows.

### `963c53b` — Limit download retries to transient failures

- Updated [`ingest_utils.py`](./ingest_utils.py) so retries occur only for:
  - Status codes in `RETRYABLE_STATUS_CODES`.
  - Connection errors.
  - Timeout errors.
- Removed the duplicate `raise_for_status()` call.
- Made HTTP 403 and 404 failures immediate, without retries.
- Added a test in [`tests/test_pipeline.py`](./tests/test_pipeline.py) covering both status codes.

### `a5f5186` — Log possible parser data loss

- Updated [`stream_parser.py`](./stream_parser.py) to count top-level `provider_references` items containing `location` instead of `provider_groups`.
- Printed that count after processing each file.
- Added warnings when a field from `TOP_LEVEL_SCALAR_FIELDS` appears after `in_network` processing has started.

### `1ff1ce5` — Use dataframe batches for rate inserts

- Updated [`stream_parser.py`](./stream_parser.py) to build a pandas DataFrame for each insert batch.
- Replaced `executemany()` inserts with `INSERT INTO negotiated_rates (...) SELECT ... FROM df`.
- Passed `use_float=True` to `ijson.parse`.
- Removed the old positional `INSERT_SQL` batch-insert path.
- Preserved the parser behavior verified by the stream-parser tests.

### `eb4ed8f` — Update pipeline documentation

- Renamed the [`README.md`](./README.md) title to “Transparency in Coverage (TiC) Data Pipeline”.
- Removed `analytics.html` from [`.gitignore`](./.gitignore).
- Consolidated the individual `./reference_data/...` ignore entries into `/reference_data/`.
- Removed the outdated provider identity-resolution sentence from [`architecture.md`](./architecture.md).
- Removed completed CI and basic-test items from [`TODO.md`](./TODO.md).

## Overall result

These commits added pipeline validation and CI foundations, improved runner safety and observability, prevented duplicate ingestion, added parser and download tests, optimized rate inserts, and updated project documentation to match the current implementation.
