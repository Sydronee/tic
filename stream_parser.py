#!/usr/bin/env python3
"""
stream_parser.py
-----------------
Step 2 of the pipeline: stream-parse a single CMS in-network-rates MRF
(.json or .json.gz, local path or remote URL) WITHOUT loading it fully
into memory, keep only institutional (facility/hospital — i.e.
"non-professional") negotiated prices, and bulk-load the results into
DuckDB in bounded-size batches.

Memory profile: bounded by BATCH_SIZE regardless of file size. We never
materialize the full `in_network` array — ijson yields one billing-code
object at a time, and negotiated_rates fact rows are flushed to DuckDB
every BATCH_SIZE rows.

Usage:
    python stream_parser.py path/to/file_in-network-rates.json.gz
    python stream_parser.py https://.../..._in-network-rates.json.gz
"""

import argparse
import gzip
from contextlib import contextmanager

import duckdb
import ijson
import requests

BATCH_SIZE = 20_000

TOP_LEVEL_SCALAR_FIELDS = {
    "reporting_entity_name", "reporting_entity_type", "plan_name",
    "plan_id", "plan_id_type", "plan_market_type", "last_updated_on", "version",
}

INSERT_SQL = """
    INSERT INTO negotiated_rates (
        payer_id, code_id, billing_class, negotiated_type, negotiated_rate,
        service_code, billing_code_modifier, expiration_date,
        provider_reference_ids, source_file
    ) VALUES (?,?,?,?,?,?,?,?,?,?)
"""


@contextmanager
def open_stream(path_or_url):
    """Yield a file-like object of DECOMPRESSED bytes, streaming from disk
    or HTTP, without ever writing the .gz payload to disk."""
    is_gz = path_or_url.endswith(".gz")
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        resp = requests.get(path_or_url, stream=True, timeout=120)
        resp.raise_for_status()
        raw = resp.raw
        raw.decode_content = True  # transparently handle any transport-level encoding
        stream = gzip.GzipFile(fileobj=raw) if is_gz else raw
        try:
            yield stream
        finally:
            resp.close()
    else:
        f = open(path_or_url, "rb")
        stream = gzip.GzipFile(fileobj=f) if is_gz else f
        try:
            yield stream
        finally:
            f.close()


def extract_header(stream):
    """Cheap first pass: stops the INSTANT the huge `in_network` array
    begins, so it only ever reads a few KB — never buffers the array."""
    header = {}
    for prefix, event, value in ijson.parse(stream):
        if prefix == "in_network" and event == "start_array":
            break
        if prefix in TOP_LEVEL_SCALAR_FIELDS and event in ("string", "number"):
            header[prefix] = value
    return header


def get_or_create_payer(con, header, source_file):
    row = con.execute(
        "SELECT payer_id FROM payers WHERE plan_id IS NOT DISTINCT FROM ? AND source_file = ?",
        [header.get("plan_id"), source_file],
    ).fetchone()
    if row:
        return row[0]
    row = con.execute(
        """INSERT INTO payers (reporting_entity_name, reporting_entity_type,
               plan_name, plan_id, plan_id_type, plan_market_type,
               last_updated_on, source_file)
           VALUES (?,?,?,?,?,?,?,?) RETURNING payer_id""",
        [header.get("reporting_entity_name"), header.get("reporting_entity_type"),
         header.get("plan_name"), header.get("plan_id"), header.get("plan_id_type"),
         header.get("plan_market_type"), header.get("last_updated_on"), source_file],
    ).fetchone()
    return row[0]


def get_or_create_code(con, code, code_type, code_version, description, cache):
    key = (code, code_type)
    if key in cache:
        return cache[key]
    row = con.execute(
        "SELECT code_id FROM billing_codes WHERE billing_code = ? AND billing_code_type = ?",
        [code, code_type],
    ).fetchone()
    if row is None:
        row = con.execute(
            """INSERT INTO billing_codes (billing_code, billing_code_type,
                   billing_code_type_version, description)
               VALUES (?,?,?,?) RETURNING code_id""",
            [code, code_type, code_version, description],
        ).fetchone()
    cache[key] = row[0]
    return row[0]


def process_file(con, source, source_file_label):
    with open_stream(source) as stream:
        header = extract_header(stream)

    payer_id = get_or_create_payer(con, header, source_file_label)
    code_cache = {}
    batch = []
    n_kept = n_seen = 0

    # Re-open the stream for the main pass (the header pass above only
    # consumed the first few KB — one small extra request/read is
    # negligible next to parsing a multi-GB file).
    with open_stream(source) as stream:
        for item in ijson.items(stream, "in_network.item", use_float=True):
            n_seen += 1
            code_id = get_or_create_code(
                con, item.get("billing_code"), item.get("billing_code_type"),
                item.get("billing_code_type_version"), item.get("description"),
                code_cache,
            )

            for nr in item.get("negotiated_rates", []):
                if "provider_references" in nr:
                    provider_ids = nr["provider_references"]
                else:
                    provider_ids = [
                        npi for g in nr.get("provider_groups", []) for npi in g.get("npi", [])
                    ]

                for price in nr.get("negotiated_prices", []):
                    # This is the core filter: "non-professional" == institutional
                    # (facility/hospital) billing class, per the CMS schema.
                    if price.get("billing_class") != "institutional":
                        continue
                    n_kept += 1
                    batch.append((
                        payer_id, code_id, price.get("billing_class"),
                        price.get("negotiated_type"), price.get("negotiated_rate"),
                        price.get("service_code"), price.get("billing_code_modifier"),
                        price.get("expiration_date"), provider_ids, source_file_label,
                    ))

            if len(batch) >= BATCH_SIZE:
                con.executemany(INSERT_SQL, batch)
                batch.clear()

    if batch:
        con.executemany(INSERT_SQL, batch)

    print(f"{source_file_label}: {n_seen} billing codes scanned, "
          f"{n_kept} institutional rate rows kept.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="Local path or URL to a .json/.json.gz MRF file")
    ap.add_argument("--db", default="transparency.duckdb")
    ap.add_argument("--schema", default="schema.sql")
    args = ap.parse_args()

    con = duckdb.connect(args.db)
    con.execute(open(args.schema).read())  # idempotent CREATE TABLE/VIEW IF NOT EXISTS

    label = args.source.rsplit("/", 1)[-1]
    process_file(con, args.source, label)
    con.close()


if __name__ == "__main__":
    main()
