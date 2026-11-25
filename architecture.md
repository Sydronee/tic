# Hospital Price Transparency Pipeline — Architecture

## Overview

This repository implements an ingestion and benchmarking pipeline for
hospital price transparency data:

1. Discover real `in-network-rates.json[.gz]` files from a UHC blob listing or a Cigna CMS index.
2. Stream-parse each file into DuckDB with bounded memory use.
3. Enrich provider and code data from reference sources.
4. Build materialized benchmark rollups for the sibling Benchmarks dashboard.

The design keeps ingestion, storage, enrichment, and serving separated so each
step can be retried independently.

## Pipeline

```
UHC / Cigna indexes ─▶ fetchers ─▶ runner.py / stream_parser.py ─▶ transparency.duckdb ─┐
NPPES / ZIP reference data ──────────────────────────────────────▶ enrichment.duckdb ───┴▶ build_benchmarks.py
                                                                                         └▶ Benchmarks dashboard
```

## Discovery

`fetch_uhc_index.py` crawls the two-level UHC blob structure. The top-level `blobs/` endpoint returns employer-specific index files, not the rate files themselves, so the script drills into each index and extracts the actual `in-network-rates.json` or `in-network-rates.json.gz` URLs.

`fetch_cigna_index.py` reads a local Cigna CMS index, extracts unique
`reporting_structure[].in_network_files[].location` URLs containing
`in-network-rates`, and writes the `{\"blobs\": [...]}` manifest consumed by
`runner.py`. The runner accepts separate manifest, database, progress, and
download paths; use the same database with separate progress files for a
combined payer comparison.

## Parsing and storage


`stream_parser.py` streams a single MRF file directly into DuckDB. It uses `gzip.GzipFile` for on-the-fly decompression and a single `ijson.parse(stream)` event loop so the file is downloaded and parsed in one pass (avoiding repeated network downloads). The parser:

- collects top-level metadata (`reporting_entity_name`, `plan_id`, `version`, etc.) — `version` is a CMS-required field
- persists `provider_references` into the `providers` table (one row per provider object), including the CMS-required `network_name`, so integer `provider_reference_id` values can be resolved later
- extracts and stores `negotiation_arrangement` and the CMS-required `name` (distinct from `description`) at the billing-code level
- captures `setting` for each `negotiated_price`
- keeps only rows where `billing_class = 'institutional'` by default

Note: CMS marks `setting` as required on every `negotiated_price`, but real-world source files are inconsistent about populating it — expect `NULL` on a meaningful share of rows, concentrated in `percentage`/`per diem` `negotiated_type` entries. This is a source-data gap, not a parser bug, and downstream consumers of `setting` should handle `NULL` explicitly.

Arrays such as `service_code`, `billing_code_modifier`, and
`provider_reference_ids` are normalized and written as SQL arrays for
downstream queries.

The storage layout in `schema.sql` is a small star schema:

- `payers` stores plan-level metadata.
- `billing_codes` stores procedure/code metadata.
- `negotiated_rates` stores the fact rows.

`procedure_price_stats` and `procedure_price_by_payer` are SQL views built on top of that schema and are used for dashboard-style aggregation.

## Benchmark build and serving

`build_benchmarks.py` joins the transparency and enrichment databases and
materializes code-, payer-, provider-, state-, and county-level statistics.
The sibling `Benchmarks/server_benchmarks.js` serves the dashboard from those
tables. See `../Benchmarks/RUNBOOK.md` for the operational commands.

For a complete end-to-end workflow, use `../Benchmarks/RUNBOOK.md`.

## Operational notes

- The parser is memory-bounded by batch size, not by file size.
- DuckDB is single-writer per database file, so stop the dashboard before ingestion or rebuilding benchmark tables.
