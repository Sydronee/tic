#!/usr/bin/env python3
"""Create a compact runner manifest sorted from smallest to largest file."""

import argparse
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="cigna_file_sizes.json",
                        help="Size report produced by check_cigna_file_sizes.py")
    parser.add_argument("--out", default="cigna_in_network_rates_by_size.json",
                        help="Compact manifest for runner.py or runnerMulti.py")
    args = parser.parse_args()

    with open(args.sizes, encoding="utf-8") as source:
        report = json.load(source)

    files = [
        {
            "name": item["name"],
            "downloadUrl": item["url"],
            "size_bytes": item["size_bytes"],
        }
        for item in report.get("files", [])
        if item.get("url") and item.get("size_bytes") is not None
    ]
    files.sort(key=lambda item: item["size_bytes"])

    with open(args.out, "w", encoding="utf-8") as destination:
        json.dump({"blobs": files}, destination, indent=2)
        destination.write("\n")
    print(f"Wrote {len(files):,} size-sorted files to {args.out}")


if __name__ == "__main__":
    main()