#!/usr/bin/env python3
"""Build a runner-compatible manifest from a Cigna CMS index JSON file."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse


def iter_rate_files(index):
    """Yield unique in-network-rates files from a CMS reporting index."""
    seen = set()
    for reporting_structure in index.get("reporting_structure", []):
        plans = reporting_structure.get("reporting_plans", [])
        plan = plans[0] if plans else {}
        for entry in reporting_structure.get("in_network_files", []):
            url = entry.get("location")
            if not url or "in-network-rates" not in urlparse(url).path.lower():
                continue
            if url in seen:
                continue
            seen.add(url)
            path_name = Path(urlparse(url).path).name
            yield {
                "name": path_name or entry.get("description") or f"rate_file_{len(seen)}",
                "downloadUrl": url,
                "description": entry.get("description"),
                "plan_name": plan.get("plan_name"),
                "plan_id": plan.get("plan_id"),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("index", help="Cigna *_index.json file")
    parser.add_argument("--out", default="cigna_in_network_rates.json",
                        help="Output manifest consumed by runner.py")
    args = parser.parse_args()

    with open(args.index, encoding="utf-8") as source:
        index = json.load(source)

    files = list(iter_rate_files(index))
    payload = {
        "reporting_entity_name": index.get("reporting_entity_name"),
        "reporting_entity_type": index.get("reporting_entity_type"),
        "blobs": files,
    }
    with open(args.out, "w", encoding="utf-8") as destination:
        json.dump(payload, destination, indent=2)
        destination.write("\n")
    print(f"Wrote {len(files):,} in-network-rates files to {args.out}")


if __name__ == "__main__":
    main()