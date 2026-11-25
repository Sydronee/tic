# Transparency in Coverage (TiC) Data Pipeline

Tools for fetching, parsing, storing, validating, benchmarking, and exploring
hospital price transparency data from UHC and Cigna in-network-rates files.

## What is here

- `fetch_uhc_index.py` discovers in-network-rates file URLs from UHC blob listings.
- `fetch_cigna_index.py` converts a Cigna CMS index JSON into the same manifest format.
- `stream_parser.py` streams a single MRF file into DuckDB without loading the full payload into memory. It uses a single `ijson.parse(stream)` pass (no double-download), persists top-level `provider_references` into a `providers` table, captures `negotiation_arrangement` and `setting`, and writes array-typed columns as SQL arrays for downstream queries.
- `runner.py` batches downloads from a filtered JSON manifest and feeds them into the parser.
- `load_code_descriptions.py` loads CMS code descriptions into the enrichment database.
- `build_benchmarks.py` builds benchmark tables and aggregate statistics.
- `validate_data.py` runs structural, referential, completeness, format, and
  coverage checks against the DuckDB database.
- `schema.sql` defines the DuckDB schema and benchmark views.
- `architecture.md` is the developer guide, with the pipeline architecture,
  data model, development commands, and a file-by-file inventory.

## Install

Create and activate a virtual environment, then install the Python dependencies:

```bash
python -m pip install -r requirements.txt
```

For development and validation:

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

The CI workflow also runs Python compilation, generated-artifact checks, and
Ruff linting.

## Typical workflow

For the current combined UHC/Cigna ingestion, enrichment, and dashboard
workflow, see the sibling [Benchmarks runbook](../Benchmarks/RUNBOOK.md).
`fetch_uhc_index.py` writes CSV, while `runner.py` consumes JSON manifests; the
runbook includes the conversion step.

To parse one local rate file directly:

```bash
python stream_parser.py path/to/file_in-network-rates.json.gz
```

To batch from an existing runner-compatible manifest:

```bash
python runner.py --manifest path/to/runner_manifest.json --max-files 5
```

Use `--dry-run` to validate and preview a manifest without downloading files
or writing a database:

```bash
python runner.py --manifest path/to/runner_manifest.json --dry-run --max-files 5
```

`runner.py` and `runnerMulti.py` retry transient HTTP failures, write downloads
atomically, validate `size_bytes` when the manifest provides it, and record a
SHA-256 checksum for each completed file. Progress updates are atomic, and
completed files are also recorded in the database to prevent duplicate
ingestion. Machine-readable events are appended to `ingestion_runs.jsonl`.

Both runners also accept `cigna_file_sizes.json` directly; they read its
`files` array and sort by `size_bytes` before processing.

Both runners validate manifest entries before opening DuckDB. Invalid entries
fail with an actionable error instead of being silently skipped.

Optionally load CMS reference descriptions:

```bash
python load_code_descriptions.py \
	--msdrg reference_data/appendix_A.txt \
	--rvu reference_data/PPRRVU2026_Jan_QPP.csv \
	--icd reference_data/icd10pcs_tables_2027.xml \
	--hcpcs reference_data/HCPC2026_ANWEB.xlsx
```

The `--hcpcs` input is optional. Revenue-code descriptions are embedded in the
loader, and `CSTM-ALL` is represented as payer-defined.

Build benchmark tables:

```bash
python build_benchmarks.py --drop-first
```

Use `--transparency-db` and `--enrichment-db` when your databases are not at
their default paths. The benchmark build requires both databases to exist.

Validate the ingested data:

```bash
python validate_data.py --db transparency.duckdb --fast --fail-on warn
```

The browser-only Parquet dashboard has been removed. The current comparison
dashboard and its startup instructions are maintained in the sibling
[Benchmarks project](../Benchmarks/RUNBOOK.md).

For the TiC pipeline architecture, data model, development workflow, and
responsibilities of each tracked file, see the [developer guide](architecture.md).

## Notes

- The parser keeps only `billing_class = 'institutional'` rows by default.
- The parser stores group-level `provider_reference_id` values into a `providers` relation so you can resolve integer references in `negotiated_rates.provider_reference_ids` to NPIs/TINs/facility names later.
- `schema.sql` is idempotent, so it can be re-run against an existing DuckDB file — including to pick up newly-added columns (`version`, `name`, `network_name`) on a database created by an older version of the parser.
