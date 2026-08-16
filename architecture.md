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


`stream_parser.py` streams a single MRF file directly into DuckDB. It uses `gzip.GzipFile` for on-the-fly decompression and a single `ijson.parse(stream)` event loop so the file is downloaded and parsed in one pass (avoiding repeated network downloads). The parser:

- collects top-level metadata (`reporting_entity_name`, `plan_id`, etc.)
- persists `provider_references` into the `providers` table (one row per provider object) so integer `provider_reference_id` values can be resolved later
- extracts and stores `negotiation_arrangement` at the billing-code level
- captures `setting` for each `negotiated_price`
- keeps only rows where `billing_class = 'institutional'` by default

Arrays such as `service_code`, `billing_code_modifier`, and `provider_reference_ids` are normalized and written as SQL arrays so they can be exported and consumed safely by the browser dashboard.

The storage layout in `schema.sql` is a small star schema:

- `payers` stores plan-level metadata.
- `billing_codes` stores procedure/code metadata.
- `negotiated_rates` stores the fact rows.
- `providers` is reserved for later identity resolution work.

`procedure_price_stats` and `procedure_price_by_payer` are SQL views built on top of that schema and are used for dashboard-style aggregation.

## Dashboard export

`export_for_dashboard.py` exports `negotiated_rates`, `payers`, `billing_codes`, and `providers` to Parquet. The exporter normalizes the output schema so the dashboard can consume the files consistently even if the source database has slightly different column names or optional fields.

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
Then open `http://localhost:8000/analytics.html` and select the exported Parquet files (negotiated_rates, payers, billing_codes, providers).

## Operational notes

- The parser is memory-bounded by batch size, not by file size.
- DuckDB is single-writer per database file, so parallel ingestion should use separate shard files or separate processes.
- Parquet is the preferred hand-off format for the dashboard because it is more stable across DuckDB build versions than a raw `.duckdb` file.
