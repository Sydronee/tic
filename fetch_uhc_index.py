#!/usr/bin/env python3
"""
fetch_uhc_index.py
-------------------
Step 1 of the pipeline: crawl UHC's Transparency-in-Coverage blob index and
build a manifest of only the actual `*_in-network-rates.json[.gz]` file URLs.

IMPORTANT — this is a TWO-LEVEL structure, not a flat list:

  Level 1: GET https://transparency-in-coverage.uhc.com/api/v1/uhc/blobs/
           -> {"blobs": [{"name": "<date>_<employer>_index.json",
                           "downloadUrl": "..."}, ...]}
           These are per-employer TABLE-OF-CONTENTS files. None of them
           are named "in-network-rates.json" themselves.

  Level 2: each downloadUrl points to an index document whose
           reporting_structure[].in_network_files[].location
           fields are the URLs of the actual rate files, named like
           "..._in-network-rates.json.gz".

This script filters at level 2 for that filename suffix and writes the
matching URLs (+ minimal metadata) to a manifest CSV, WITHOUT downloading
the (often multi-GB) rate files themselves — that happens in stream_parser.py.

Scale warning: the level-1 list alone typically has tens of thousands of
entries, and the endpoint is documented to be intermittently unreliable
(occasional 500s). Start with --limit for local testing; remove it (or
raise it) once you have the full pipeline working end to end.
"""

import argparse
import csv
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BLOB_LIST_URL = "https://transparency-in-coverage.uhc.com/api/v1/uhc/blobs/"
TARGET_SUFFIXES = ("in-network-rates.json", "in-network-rates.json.gz")


def fetch_json_with_retry(url, session, max_retries=5, timeout=60):
    delay = 2
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            print(f"  [warn] HTTP {resp.status_code} for {url} (attempt {attempt}/{max_retries})",
                  file=sys.stderr)
        except (requests.RequestException, ValueError) as e:
            print(f"  [warn] {e} (attempt {attempt}/{max_retries})", file=sys.stderr)
        time.sleep(delay)
        delay = min(delay * 2, 60)
    return None


def get_blob_list(session):
    print(f"Fetching top-level blob list from {BLOB_LIST_URL} ...")
    data = fetch_json_with_retry(BLOB_LIST_URL, session)
    if not data or "blobs" not in data:
        raise RuntimeError(
            "Could not retrieve/parse the top-level blob list. "
            "This endpoint is known to be occasionally flaky/slow — retry, "
            "or check your network/proxy settings."
        )
    return data["blobs"]


def extract_in_network_files(index_json):
    """Given one parsed *_index.json document, yield every nested file
    whose location matches our target filename suffix."""
    for rs in index_json.get("reporting_structure", []):
        plans = rs.get("reporting_plans", [])
        plan_name = plans[0].get("plan_name") if plans else None
        plan_id = plans[0].get("plan_id") if plans else None
        for f in rs.get("in_network_files", []):
            loc = f.get("location", "")
            if loc.endswith(TARGET_SUFFIXES):
                yield loc, plan_name, plan_id


def process_one_blob(blob, session):
    name = blob.get("name", "")
    url = blob.get("downloadUrl")
    if not url:
        return []
    index_json = fetch_json_with_retry(url, session)
    if index_json is None:
        return []
    return [
        {"index_name": name, "index_url": url, "rate_file_url": loc,
         "plan_name": plan_name, "plan_id": plan_id}
        for loc, plan_name, plan_id in extract_in_network_files(index_json)
    ]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="manifest.csv", help="Output manifest CSV path")
    ap.add_argument("--limit", type=int, default=50,
                     help="Max number of level-1 index files to crawl. The full "
                          "list is large (tens of thousands) — start small.")
    ap.add_argument("--workers", type=int, default=8, help="Concurrent HTTP workers")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": "price-transparency-pipeline/0.1"})

    blobs = get_blob_list(session)
    print(f"Top-level blob count: {len(blobs)} (processing first {args.limit})")
    blobs = blobs[: args.limit]

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_one_blob, b, session): b for b in blobs}
        for i, fut in enumerate(as_completed(futures), 1):
            blob = futures[fut]
            try:
                rows.extend(fut.result())
            except Exception as e:
                print(f"  [error] {blob.get('name')}: {e}", file=sys.stderr)
            if i % 10 == 0 or i == len(blobs):
                print(f"  processed {i}/{len(blobs)} index files, "
                      f"{len(rows)} matching in-network-rates files found so far")

    out_path = Path(args.out)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["index_name", "index_url", "rate_file_url", "plan_name", "plan_id"]
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Done. {len(rows)} in-network-rates file URLs written to {out_path}")


if __name__ == "__main__":
    main()
