# Hospital Price Transparency Pipeline

Tools for fetching, parsing, storing, and exploring hospital price transparency data from UHC-style in-network-rates files.

## What is here

- `fetch_uhc_index.py` discovers in-network-rates file URLs from UHC blob listings.
- `fetch_cigna_index.py` converts a Cigna CMS index JSON into the same manifest format.
- `fetch_and_filter_blobs.py` is a smaller helper that filters the top-level blob response into a local JSON list.
- `stream_parser.py` streams a single MRF file into DuckDB without loading the full payload into memory. It uses a single `ijson.parse(stream)` pass (no double-download), persists top-level `provider_references` into a `providers` table, captures `negotiation_arrangement` and `setting`, and writes array-typed columns as SQL arrays so they can be exported directly.
- `runner.py` batches downloads from a filtered JSON manifest and feeds them into the parser.
- `export_for_dashboard.py` exports the three dashboard tables to Parquet.
- `analytics.html` is the browser dashboard that reads the exported tables.
- `schema.sql` defines the DuckDB schema and benchmark views.
- `architecture.md` explains the pipeline at a higher level.

## Install

Create and activate a virtual environment, then install the Python dependencies:

```bash
pip install duckdb ijson requests
```

## Typical workflow

1. Discover source URLs:

```bash
python fetch_uhc_index.py --limit 50 --out manifest.csv
```

2. Parse one file into DuckDB:

```bash
python stream_parser.py path/to/file_in-network-rates.json.gz
```

3. Or batch download and process from a filtered manifest:

```bash
python runner.py --max-files 5
```

For a Cigna index, create a manifest and use separate output files so the UHC
run can remain resumable:

```bash
python fetch_cigna_index.py 2026-09-01_cigna-health-life-insurance-company_index.json \
	--out cigna_in_network_rates.json
python runner.py --manifest cigna_in_network_rates.json \
	--db cigna.duckdb --progress cigna_processed_count.txt --max-files 1
```

Remove `--max-files 1` to continue through all 124 files. The runner supports
Cigna's `.json.gz` and `.zip` rate-file formats; ZIPs are streamed from their
`in-network-rates` JSON member without extracting the archive permanently.

To check the remote compressed size of every Cigna file without downloading
them, write a sorted report:

```bash
python check_cigna_file_sizes.py
```

The report is saved to `cigna_file_sizes.json`; its `smallest`, `largest`, and
`files` fields contain the summary and per-file results.

Create the compact manifest used for smallest-to-largest processing:

```bash
python create_size_sorted_manifest.py
python runner.py --manifest cigna_in_network_rates_by_size.json \
	--db cigna.duckdb --progress cigna_processed_count.txt
```

The prefetching runner uses the same manifest:

```bash
python runnerMulti.py --manifest cigna_in_network_rates_by_size.json \
	--db cigna.duckdb --progress cigna_processed_count.txt
```

Both runners retry transient HTTP failures with exponential backoff, write
downloads atomically, validate `size_bytes` when the manifest provides it, and
record a SHA-256 checksum for each completed file. Progress updates are also
atomic, so an interrupted run can be resumed without a partially-written
counter. Machine-readable success and failure events are appended to
`ingestion_runs.jsonl`; use `--run-log PATH` to choose another location.

Both runners also accept `cigna_file_sizes.json` directly; they read its
`files` array and sort by `size_bytes` before processing.

4. Export dashboard-ready Parquet files:

```bash
	python export_for_dashboard.py --db transparency.duckdb --out ./web
```

5. Serve the folder and open the dashboard:

```bash
python -m http.server 8000
```

Then open `http://localhost:8000/analytics.html` and load the three Parquet files together.

## Dashboard contract

The dashboard expects these exported files (the exporter now also writes provider mappings):

- `negotiated_rates.parquet` (includes `negotiation_arrangement` and `setting`)
- `payers.parquet`
- `billing_codes.parquet` (includes optional `negotiation_arrangement`)
- `providers.parquet` (mapping of `provider_reference_id` → NPI/TIN/facility)

The Parquet loader in `analytics.html` mounts each file as a temporary table named after the base relation, so the dashboard SQL can keep reading from `negotiated_rates`, `payers`, and `billing_codes` without schema changes.

## Notes

- The parser keeps only `billing_class = 'institutional'` rows by default.
- The parser stores group-level `provider_reference_id` values into a `providers` relation so you can resolve integer references in `negotiated_rates.provider_reference_ids` to NPIs/TINs/facility names later.
- The exported Parquet schema is normalized to match the dashboard's expected columns; array-typed columns (e.g., `service_code`, `billing_code_modifier`, `provider_reference_ids`, `network_name`) are emitted as proper Parquet arrays so DuckDB WASM can read them as arrays.
- `schema.sql` is idempotent, so it can be re-run against an existing DuckDB file — including to pick up newly-added columns (`version`, `name`, `network_name`) on a database created by an older version of the parser.