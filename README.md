# Hospital Price Transparency Pipeline

Tools for fetching, parsing, storing, and exploring hospital price transparency data from UHC-style in-network-rates files.

## What is here

- `fetch_uhc_index.py` discovers in-network-rates file URLs from UHC blob listings.
- `fetch_and_filter_blobs.py` is a smaller helper that filters the top-level blob response into a local JSON list.
- `stream_parser.py` streams a single MRF file into DuckDB without loading the full payload into memory.
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

The dashboard expects these exported files:

- `negotiated_rates.parquet`
- `payers.parquet`
- `billing_codes.parquet`

The Parquet loader in `analytics.html` mounts each file as a temporary table named after the base relation, so the dashboard SQL can keep reading from `negotiated_rates`, `payers`, and `billing_codes` without schema changes.

## Notes

- The parser keeps only `billing_class = 'institutional'` rows.
- The exported Parquet schema is normalized to match the dashboard’s expected columns.
- `schema.sql` is idempotent, so it can be re-run against an existing DuckDB file.
