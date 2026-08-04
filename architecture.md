# Hospital Price Transparency Pipeline — Architecture

## About the UHC blob fetch you asked for

I tried fetching `https://transparency-in-coverage.uhc.com/api/v1/uhc/blobs/`
directly and it errored both times. That's consistent with how this endpoint
is documented elsewhere — it's known to be large and intermittently flaky
(occasional 500s). It's also not in my sandbox's network allowlist, so I
can't run the crawl from here regardless.

More importantly, **the filter you described needs to happen one level deeper
than the top-level blob list.** The `blobs/` endpoint returns entries like:

```json
{"name": "2024-06-01_Some-Employer_index.json", "downloadUrl": "https://uhc-tic-mrf.azureedge.net/..."}
```

Those are per-employer **table-of-contents** files — none of them are named
`in-network-rates.json`. Each one has to be downloaded and its
`reporting_structure[].in_network_files[].location` fields inspected; *those*
URLs are the actual `..._in-network-rates.json.gz` files. There can be tens
of thousands of top-level entries, so crawling all of them is itself a
non-trivial job (and hits that flaky endpoint many times).

`fetch_uhc_index.py` (included) does exactly this two-level crawl — with
retry/backoff and a `--limit` flag so you can test locally against a small
slice before scaling up. Run it on your machine:

```bash
pip install requests
python fetch_uhc_index.py --limit 50 --out manifest.csv
```

That produces `manifest.csv` of real `in-network-rates.json[.gz]` URLs,
which `stream_parser.py` then consumes.

## Pipeline overview

```
┌─────────────────────┐     ┌──────────────────────┐     ┌──────────────────┐
│ 1. Discovery         │     │ 2. Streaming parse    │     │ 3. Storage        │
│ fetch_uhc_index.py    │───▶│ stream_parser.py       │───▶│ DuckDB             │
│ blobs/ → index files   │    │ ijson + gzip streaming  │    │ transparency.duckdb │
│ → manifest.csv           │    │ filter: institutional     │    │                     │
└─────────────────────┘     └──────────────────────┘     └──────────────────┘
                                                                     │
                                                     ┌───────────────┴───────────────┐
                                                     ▼                                ▼
                                          ┌────────────────────┐         ┌──────────────────────┐
                                          │ 4. Stats views       │         │ 5. API (FastAPI)       │
                                          │ procedure_price_stats │         │ /procedures/{code}      │
                                          │ procedure_price_by_payer│       │ /payers, /stats           │
                                          └────────────────────┘         └──────────────────────┘
                                                                                       │
                                                                                       ▼
                                                                         ┌──────────────────────┐
                                                                         │ 6. Frontend dashboard   │
                                                                         │ static HTML/JS + Chart.js │
                                                                         └──────────────────────┘
```

### 1. Discovery (`fetch_uhc_index.py`)
Crawls the two-level blob structure and writes a manifest of only the
in-network-rates file URLs — no rate data is downloaded at this stage, just
URLs + plan metadata. This is the checkpoint boundary: re-running discovery
is cheap and safe to repeat/resume.

### 2. Streaming parse + filter (`stream_parser.py`)
For each manifest URL: stream the HTTP response through `gzip.GzipFile`
(decompressing on the fly, never writing the multi-GB `.gz` to disk), and
feed that into `ijson`, which parses the JSON incrementally and yields one
`in_network` billing-code object at a time — the array itself (which can
have millions of entries) is never materialized in memory.

For each billing code, iterate `negotiated_rates[].negotiated_prices[]` and
keep only rows where `billing_class == "institutional"` — that's the CMS
schema's actual field for "non-professional" (facility/hospital) charges, as
opposed to `"professional"` (physician) charges. Rows are buffered and
flushed to DuckDB every 20,000 records, so memory use is bounded regardless
of file size.

**Scaling across files:** `ijson`'s C backend is single-threaded per file.
To process many files concurrently, run multiple OS processes (e.g.
`multiprocessing.Pool` or a simple job queue), one file per worker — but
have each worker **write to its own DuckDB file or Parquet shard**, not the
same open connection. DuckDB is single-writer per database file; concurrent
writers to one file will contend or error. A common pattern: workers write
Parquet shards, then a single `COPY`/`INSERT ... SELECT * FROM read_parquet(...)`
pass loads everything into the shared `transparency.duckdb` at the end —
DuckDB's Parquet reader is fast enough that this beats row-by-row inserts at
scale.

### 3. Storage (DuckDB)
See `schema.sql`. A small star schema: `payers` and `billing_codes` as
dimensions, `negotiated_rates` as the fact table, with a `providers` table
left for optional phase-2 identity resolution (see note below).

### 4. Benchmarking views
`procedure_price_stats` and `procedure_price_by_payer` use DuckDB's native
`MEDIAN()` aggregate alongside `MIN`/`MAX`/`AVG` — no window-function
gymnastics needed. These are plain SQL views, so a dashboard query is just
`SELECT * FROM procedure_price_stats WHERE billing_code = '470'`.

### 5. API layer
A thin FastAPI service is the natural next layer (not included here since
you asked specifically for architecture + schema + parser, but sketched for
completeness):

```python
from fastapi import FastAPI
import duckdb

app = FastAPI()
con = duckdb.connect("transparency.duckdb", read_only=True)

@app.get("/procedures/{billing_code}/stats")
def procedure_stats(billing_code: str):
    return con.execute(
        "SELECT * FROM procedure_price_stats WHERE billing_code = ?", [billing_code]
    ).fetchdf().to_dict(orient="records")

@app.get("/procedures/{billing_code}/by-payer")
def procedure_by_payer(billing_code: str):
    return con.execute(
        "SELECT * FROM procedure_price_by_payer WHERE billing_code = ?", [billing_code]
    ).fetchdf().to_dict(orient="records")
```

DuckDB handles concurrent *readers* fine (`read_only=True`), so the API
process can run alongside future ingestion jobs safely as long as ingestion
uses a separate write connection/process.

### 6. Frontend
Static HTML/CSS/JS hitting the API — a bar/box-plot per procedure code
(min/median/avg/max across payers) is the natural first view, since that's
exactly what `procedure_price_stats` returns.

## Domain nuances worth knowing before you scale this up

- **`negotiated_type` matters.** Not every price is a flat dollar amount —
  CMS allows `"negotiated"`, `"percentage"`, `"derived"`, and `"fee schedule"`.
  The schema captures all of them, but the stats views only aggregate
  `"negotiated"` (fixed dollar) rows; percentage-of-billed-charges rows need
  a different denominator to be meaningful and are intentionally excluded
  from the numeric aggregates.
- **Provider identity is a separate resolution step.** The raw file only
  gives you NPIs/TINs (or an integer `provider_reference_id` pointing at a
  separate array — which itself is sometimes hosted at an external URL for
  very large payers). Turning that into a hospital *name* means either
  parsing the payer's provider-reference file or joining against NPPES.
  That's deliberately out of scope for the fact table itself so ingestion
  isn't blocked on it — `negotiated_rates.provider_reference_ids` holds the
  raw IDs for a later batch join into `providers`.
- **File sizes vary enormously.** Some payers' in-network files are
  low hundreds of MB; others (notably some UHC files) run into tens of GB.
  The streaming design here handles both the same way — it never depends on
  file size fitting in memory.

## Dependencies

```
pip install duckdb ijson requests
```
`ijson` auto-selects a compiled C backend (`yajl2_c`) if available on your
system, which is significantly faster than the pure-Python fallback for
files with millions of records — worth confirming with
`python -c "import ijson; print(ijson.backend)"` before a big run.
