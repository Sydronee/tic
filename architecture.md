# Hospital Price Transparency Pipeline — Architecture

## Overview

This repository implements a small end-to-end pipeline for working with hospital price transparency data:

1. Discover real `in-network-rates.json[.gz]` files from the UHC blob listing.
2. Stream-parse each file into DuckDB with bounded memory use.
3. Export the core tables to Parquet for browser-safe consumption.
4. Load those Parquet files in `analytics.html` for local, client-side analysis.

The design keeps ingestion, storage, and dashboard loading separated so each step can be retried independently.

## Pipeline

```
Discovery            Streaming parse            DuckDB storage             Dashboard export
fetch_uhc_index.py ─▶ stream_parser.py ────────▶ transparency.duckdb ─────▶ export_for_dashboard.py
                                                                                   │
                                                                                   ▼
                                                                           analytics.html
```

## Discovery

`fetch_uhc_index.py` crawls the two-level UHC blob structure. The top-level `blobs/` endpoint returns employer-specific index files, not the rate files themselves, so the script drills into each index and extracts the actual `in-network-rates.json` or `in-network-rates.json.gz` URLs.

`fetch_and_filter_blobs.py` is a smaller helper that saves the raw blob response and writes a filtered JSON list of likely in-network-rates files. It is useful for quick local experiments, but `fetch_uhc_index.py` is the more complete discovery path.

## Parsing and storage

`stream_parser.py` streams a single MRF file directly into DuckDB. It uses `gzip.GzipFile` for on-the-fly decompression and `ijson` so the full `in_network` array never has to live in memory.

Only rows with `billing_class = 'institutional'` are kept. That matches the facility/hospital use case and avoids mixing in the `professional` rows that require different analysis.

The storage layout in `schema.sql` is a small star schema:

- `payers` stores plan-level metadata.
- `billing_codes` stores procedure/code metadata.
- `negotiated_rates` stores the fact rows.
- `providers` is reserved for later identity resolution work.

`procedure_price_stats` and `procedure_price_by_payer` are SQL views built on top of that schema and are used for dashboard-style aggregation.

## Dashboard export

`export_for_dashboard.py` exports `negotiated_rates`, `payers`, and `billing_codes` to Parquet. The exporter normalizes the output schema so the dashboard can consume the files consistently even if the source database has slightly different column names or optional fields.

The dashboard in `analytics.html` loads those Parquet files locally in the browser. It now mounts them as temporary tables to avoid DuckDB catalog conflicts with the built-in demo tables that use the same base names.

## End-to-end usage

```bash
pip install duckdb ijson requests

python fetch_uhc_index.py --limit 50 --out manifest.csv
python stream_parser.py path/to/file_in-network-rates.json.gz
python export_for_dashboard.py --db transparency.duckdb --out ./web
python -m http.server 8000
```

Then open `http://localhost:8000/analytics.html` and select the three exported Parquet files together.

## Operational notes

- The parser is memory-bounded by batch size, not by file size.
- DuckDB is single-writer per database file, so parallel ingestion should use separate shard files or separate processes.
- Parquet is the preferred hand-off format for the dashboard because it is more stable across DuckDB build versions than a raw `.duckdb` file.
