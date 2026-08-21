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
import hashlib
from contextlib import contextmanager

import duckdb
import ijson
import requests

BATCH_SIZE = 122_880  # Match DuckDB's native RowGroup size for optimal storage

TOP_LEVEL_SCALAR_FIELDS = {
    "reporting_entity_name", "reporting_entity_type", "plan_name",
    "plan_id", "plan_id_type", "plan_market_type", "last_updated_on", "version",
}

INSERT_SQL = """
    INSERT INTO negotiated_rates (
        payer_id, code_id, negotiation_arrangement, billing_class, setting,
        negotiated_type, negotiated_rate, service_code, billing_code_modifier,
        expiration_date, provider_reference_ids, source_file
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
"""



# Some MRF hosts (UHC's blob endpoint among them) 403 requests that don't
# look like they came from a browser. runner.py's own downloader already
# sends this; stream_parser needs the same header for its direct-URL path
# (`python stream_parser.py <url>`, or calling process_file() with a URL).
_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


@contextmanager
def open_stream(path_or_url):
    """Yield a file-like object of DECOMPRESSED bytes, streaming from disk
    or HTTP, without ever writing the .gz payload to disk."""
    is_gz = path_or_url.endswith(".gz")
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        resp = requests.get(path_or_url, headers=_HTTP_HEADERS, stream=True, timeout=120)
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
               last_updated_on, version, source_file)
           VALUES (?,?,?,?,?,?,?,?,?) RETURNING payer_id""",
        [header.get("reporting_entity_name"), header.get("reporting_entity_type"),
         header.get("plan_name"), header.get("plan_id"), header.get("plan_id_type"),
         header.get("plan_market_type"), header.get("last_updated_on"),
         header.get("version"), source_file],
    ).fetchone()
    return row[0]


def get_or_create_code(con, code, code_type, code_version, description, name, negotiation_arrangement, cache):
    key = (code, code_type, negotiation_arrangement)
    if key in cache:
        return cache[key]
    # NOTE: negotiation_arrangement is part of the lookup, matching the cache
    # key above. Previously this only matched on (code, code_type), so a code
    # seen under a second negotiation_arrangement would silently reuse the
    # first row's id and arrangement instead of getting its own row.
    row = con.execute(
        "SELECT code_id FROM billing_codes WHERE billing_code = ? AND billing_code_type = ? "
        "AND negotiation_arrangement IS NOT DISTINCT FROM ?",
        [code, code_type, negotiation_arrangement],
    ).fetchone()
    if row is None:
        row = con.execute(
            """INSERT INTO billing_codes (billing_code, billing_code_type,
                   billing_code_type_version, description, name, negotiation_arrangement)
               VALUES (?,?,?,?,?,?) RETURNING code_id""",
            [code, code_type, code_version, description, name, negotiation_arrangement],
        ).fetchone()
    cache[key] = row[0]
    return row[0]


def _provider_group_key(prov):
    """Stable content-derived key for a provider_group that has no CMS-assigned
    id of its own (i.e. one embedded inline on a negotiated_rate rather than
    referenced from the top-level provider_references array). Used so that
    re-parsing the same file, or seeing an identical group in another file,
    resolves to the same synthetic provider_reference_id instead of creating
    a duplicate row each time."""
    tin = prov.get("tin") or {}
    raw = "|".join([
        str(tin.get("type") or ""),
        str(tin.get("value") or ""),
        ",".join(sorted(str(n) for n in (prov.get("npi") or []))),
    ])
    return "embedded:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def get_or_create_provider_group(con, group_id, prov, cache, network_name=None):
    """Resolve a provider_group to a provider_reference_id, inserting one
    providers row per NPI in the group.

    - group_id given (from top-level provider_references): a real CMS id.
      De-duplicated per (group_id, npi) so re-running a file doesn't insert
      duplicate rows. `network_name` (required by the CMS schema on the
      enclosing provider_references item) is stored alongside it.
    - group_id is None (embedded provider_groups on a negotiated_rate item):
      mints a synthetic negative id, keyed by _provider_group_key, so the
      same embedded group always resolves to the same id. Embedded groups
      have no enclosing provider_references item, so network_name is
      always unavailable (NULL) for these.

    Returns the provider_reference_id to store on negotiated_rates rows.
    """
    tin = prov.get("tin") or {}
    tin_type = tin.get("type")
    tin_value = tin.get("value")
    facility_name = tin.get("business_name")
    npis = prov.get("npi") or [None]  # keep the group even if it lists no NPI

    if group_id is not None:
        cache_key = ("ref", group_id)
        if cache_key not in cache:
            cache[cache_key] = True
            for npi in npis:
                exists = con.execute(
                    "SELECT 1 FROM providers WHERE provider_reference_id = ? "
                    "AND npi IS NOT DISTINCT FROM ? LIMIT 1",
                    [group_id, npi],
                ).fetchone()
                if not exists:
                    con.execute(
                        "INSERT INTO providers (provider_reference_id, npi, tin_type, "
                        "tin_value, facility_name, network_name, group_key) VALUES (?,?,?,?,?,?,?)",
                        [group_id, npi, tin_type, tin_value, facility_name, network_name, f"ref:{group_id}"],
                    )
        return group_id

    group_key = _provider_group_key(prov)
    if group_key in cache:
        return cache[group_key]
    row = con.execute(
        "SELECT provider_reference_id FROM providers WHERE group_key = ? LIMIT 1",
        [group_key],
    ).fetchone()
    if row:
        cache[group_key] = row[0]
        return row[0]

    synthetic_id = -con.execute("SELECT nextval('seq_synthetic_provider_ref')").fetchone()[0]
    for npi in npis:
        con.execute(
            "INSERT INTO providers (provider_reference_id, npi, tin_type, "
            "tin_value, facility_name, network_name, group_key) VALUES (?,?,?,?,?,?,?)",
            [synthetic_id, npi, tin_type, tin_value, facility_name, None, group_key],
        )
    cache[group_key] = synthetic_id
    return synthetic_id


def _build_value_from_events(event_iter, prefix, event, value):
    """Recursive builder that consumes events from ijson.parse and builds
    the corresponding Python object (dict, list, or scalar).
    """
    if event in ("string", "number", "boolean") or event == "null":
        return value

    if event == "start_map":
        obj = {}
        for p, ev, val in event_iter:
            if ev == "map_key":
                key = val
                # next event(s) correspond to the value for this key
                p2, ev2, val2 = next(event_iter)
                obj[key] = _build_value_from_events(event_iter, p2, ev2, val2)
                continue
            if ev == "end_map":
                return obj

    if event == "start_array":
        arr = []
        for p, ev, val in event_iter:
            if ev == "end_array":
                return arr
            arr.append(_build_value_from_events(event_iter, p, ev, val))

    # Fallback
    return None

def process_file(con, source, source_file_label):
    # 1. Start an explicit transaction block for the entire file pass
    con.execute("BEGIN TRANSACTION;")
    
    try:
        with open_stream(source) as stream:
            parser = ijson.parse(stream)

            header = {}
            payer_id = None
            code_cache = {}
            provider_cache = {}
            batch = []
            n_seen = n_kept = 0

            in_in_network = False

            for prefix, event, value in parser:
                # collect top-level scalars
                if not in_in_network and prefix in TOP_LEVEL_SCALAR_FIELDS and event in ("string", "number"):
                    header[prefix] = value
                    continue

                # top-level provider_references array items
                if prefix == "provider_references.item" and event == "start_map":
                    obj = _build_value_from_events(parser, prefix, event, value)
                    # obj should be a mapping with provider_group_id, provider_groups
                    # and network_name (all three required by the CMS schema).
                    group_id = obj.get("provider_group_id")
                    network_name = obj.get("network_name")
                    for prov in obj.get("provider_groups", []):
                        get_or_create_provider_group(con, group_id, prov, provider_cache, network_name)
                    continue

                # detect start of in_network array and switch to processing items
                if prefix == "in_network" and event == "start_array":
                    in_in_network = True
                    # create payer row now that we have header
                    payer_id = get_or_create_payer(con, header, source_file_label)
                    continue

                # process each in_network.item map
                if in_in_network and prefix == "in_network.item" and event == "start_map":
                    item = _build_value_from_events(parser, prefix, event, value)
                    n_seen += 1

                    negotiation_arrangement = item.get("negotiation_arrangement")

                    code_id = get_or_create_code(
                        con,
                        item.get("billing_code"),
                        item.get("billing_code_type"),
                        item.get("billing_code_type_version"),
                        item.get("description"),
                        item.get("name"),
                        negotiation_arrangement,
                        code_cache,
                    )

                    for nr in item.get("negotiated_rates", []):
                        # collect provider ids from either provider_references or provider_groups
                        provider_ids = []
                        if "provider_references" in nr:
                            provider_ids = nr.get("provider_references") or []
                        else:
                            # Embedded provider_groups have no id of their own — resolve
                            # (and persist) each one to a real provider_reference_id
                            # instead of stuffing raw NPIs into this column.
                            for g in nr.get("provider_groups", []) or []:
                                pid = get_or_create_provider_group(con, None, g, provider_cache)
                                provider_ids.append(pid)

                        for price in nr.get("negotiated_prices", []):
                            if price.get("billing_class") != "institutional":
                                continue
                            n_kept += 1

                            # normalize arrays/values so DuckDB receives proper array types
                            service_code = price.get("service_code") or []
                            billing_code_modifier = price.get("billing_code_modifier") or []

                            # ensure provider_ids are simple ints
                            provider_ids_clean = [int(p) for p in provider_ids if p is not None]

                            batch.append((
                                payer_id,
                                code_id,
                                negotiation_arrangement,
                                price.get("billing_class"),
                                price.get("setting"),
                                price.get("negotiated_type"),
                                price.get("negotiated_rate"),
                                service_code,
                                billing_code_modifier,
                                price.get("expiration_date"),
                                provider_ids_clean,
                                source_file_label,
                            ))

                    if len(batch) >= BATCH_SIZE:
                        con.executemany(INSERT_SQL, batch)
                        batch.clear()

            # flush remaining
            if batch:
                con.executemany(INSERT_SQL, batch)

            # commit transaction
            con.execute("COMMIT;")

            print(f"{source_file_label}: {n_seen} billing codes scanned, {n_kept} institutional rate rows kept.")

    except Exception:
        con.execute("ROLLBACK;")
        raise

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