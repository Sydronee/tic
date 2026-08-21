#!/usr/bin/env python3
"""
export_for_dashboard.py
------------------------
Dump the relations the dashboard needs into Parquet (including `providers`).

Parquet is the recommended hand-off because its format is stable across DuckDB
versions, whereas a raw .duckdb file must match the storage version compiled
into DuckDB WASM — a mismatch is the most common reason the dashboard refuses
to attach a database directly.

    python export_for_dashboard.py --db transparency.duckdb --out ./web

Then serve the folder and pick the exported .parquet files in the dashboard's
"Connect a data source" dialog (negotiated_rates, payers, billing_codes, providers):

    python -m http.server 8000
"""

import argparse
from pathlib import Path

import duckdb

TABLES = ["negotiated_rates", "payers", "billing_codes", "providers"]

EXPORT_SCHEMAS = {
    "negotiated_rates": [
        ("rate_id", ["rate_id"], "row_number() OVER ()"),
        ("payer_id", ["payer_id"], None),
        ("code_id", ["code_id"], None),
        ("billing_class", ["billing_class"], None),
        ("negotiated_type", ["negotiated_type"], None),
        ("negotiated_rate", ["negotiated_rate", "rate"], None),
        ("service_code", ["service_code"], "[]::VARCHAR[]"),
        ("billing_code_modifier", ["billing_code_modifier"], "[]::VARCHAR[]"),
        ("negotiation_arrangement", ["negotiation_arrangement"], "NULL::VARCHAR"),
        ("setting", ["setting"], "NULL::VARCHAR"),
        ("expiration_date", ["expiration_date"], None),
        ("provider_reference_ids", ["provider_reference_ids"], "[]::BIGINT[]"),
        ("source_file", ["source_file"], None),
        ("ingested_at", ["ingested_at"], "now()::TIMESTAMP"),
    ],
    "payers": [
        ("payer_id", ["payer_id"], None),
        ("reporting_entity_name", ["reporting_entity_name"], None),
        ("reporting_entity_type", ["reporting_entity_type"], None),
        ("plan_name", ["plan_name"], None),
        ("plan_id", ["plan_id"], None),
        ("plan_id_type", ["plan_id_type"], None),
        ("plan_market_type", ["plan_market_type"], None),
        ("last_updated_on", ["last_updated_on"], None),
        ("version", ["version"], "NULL::VARCHAR"),
        ("source_file", ["source_file"], None),
    ],
    "billing_codes": [
        ("code_id", ["code_id"], None),
        ("billing_code", ["billing_code"], None),
        ("billing_code_type", ["billing_code_type"], None),
        ("billing_code_type_version", ["billing_code_type_version", "code_type_version"], "NULL::VARCHAR"),
        ("description", ["description"], None),
        ("name", ["name"], "NULL::VARCHAR"),
        ("negotiation_arrangement", ["negotiation_arrangement"], "NULL::VARCHAR"),
    ],
    "providers": [
        ("provider_id", ["provider_id"], None),
        ("provider_reference_id", ["provider_reference_id"], None),
        ("npi", ["npi"], None),
        ("tin_type", ["tin_type"], None),
        ("tin_value", ["tin_value"], None),
        ("facility_name", ["facility_name"], None),
        ("network_name", ["network_name"], "[]::VARCHAR[]"),
    ],
}


def table_columns(con, table):
    return {row[1] for row in con.execute(f"PRAGMA table_info('{table}')").fetchall()}


def build_projection(con, table):
    available = table_columns(con, table)
    parts = []
    for out_name, candidates, fallback in EXPORT_SCHEMAS[table]:
        source = next((name for name in candidates if name in available), None)
        if source is not None:
            if source == out_name:
                parts.append(out_name)
            else:
                parts.append(f"{source} AS {out_name}")
        elif fallback is not None:
            parts.append(f"{fallback} AS {out_name}")
        else:
            raise SystemExit(
                f"{table} is missing required column '{out_name}'. "
                f"Found: {', '.join(sorted(available)) or '(none)'}"
            )
    return ", ".join(parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="transparency.duckdb")
    ap.add_argument("--out", default=".", help="Directory to write .parquet files into")
    ap.add_argument("--compression", default="snappy", choices=["snappy", "zstd", "gzip", "uncompressed"])
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(args.db, read_only=True)

    present = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    missing = [t for t in TABLES if t not in present]
    if missing:
        raise SystemExit(
            f"{args.db} is missing table(s): {', '.join(missing)}.\n"
            f"Found: {', '.join(sorted(present)) or '(none)'}\n"
            "Run stream_parser.py first to populate the database."
        )

    total = 0
    for t in TABLES:
        dest = out / f"{t}.parquet"
        projection = build_projection(con, t)
        con.execute(
            f"COPY (SELECT {projection} FROM {t}) TO '{dest.as_posix()}' "
            f"(FORMAT PARQUET, COMPRESSION '{args.compression}')"
        )
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        con.execute(f"SELECT count(*) FROM read_parquet('{dest.as_posix()}')").fetchone()[0]
        size = dest.stat().st_size / 1024
        total += n
        print(f"  {t:20s} {n:>10,} rows  →  {dest.name}  ({size:,.0f} KB)")

    print(f"\n{total:,} rows exported to {out.resolve()}")
    print("Load the exported Parquet files (negotiated_rates, payers, billing_codes, providers) in the dashboard's data-source dialog.")


if __name__ == "__main__":
    main()