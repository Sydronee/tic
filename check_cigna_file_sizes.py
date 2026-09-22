#!/usr/bin/env python3
"""Check remote sizes for every file in a runner-compatible manifest."""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests


USER_AGENT = "price-transparency-pipeline/0.1"


def get_content_length(url, timeout):
    """Return (bytes, status, method, error) without downloading a file."""
    headers = {"User-Agent": USER_AGENT}
    try:
        response = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
        length = response.headers.get("Content-Length")
        if length is not None:
            return int(length), response.status_code, "HEAD", None
    except (requests.RequestException, ValueError) as exc:
        head_error = str(exc)
    else:
        head_error = "HEAD response did not include Content-Length"

    # Some hosts omit Content-Length on HEAD but provide it for a one-byte range.
    try:
        range_headers = {**headers, "Range": "bytes=0-0"}
        response = requests.get(
            url,
            headers=range_headers,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
        response.raise_for_status()
        content_range = response.headers.get("Content-Range", "")
        if "/" in content_range:
            return int(content_range.rsplit("/", 1)[1]), response.status_code, "RANGE", None
        length = response.headers.get("Content-Length")
        if response.status_code == 200 and length is not None:
            return int(length), response.status_code, "GET", None
        return None, response.status_code, "RANGE", f"No total size in Content-Range ({head_error})"
    except (requests.RequestException, ValueError) as exc:
        return None, None, "RANGE", f"{head_error}; fallback failed: {exc}"


def check_item(index, item, timeout):
    url = item.get("downloadUrl") if isinstance(item, dict) else item
    name = item.get("name") if isinstance(item, dict) else Path(urlparse(url).path).name
    size, status, method, error = get_content_length(url, timeout)
    return {
        "index": index,
        "name": name,
        "url": url,
        "size_bytes": size,
        "size_gib": round(size / (1024 ** 3), 3) if size is not None else None,
        "http_status": status,
        "method": method,
        "error": error,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="cigna_in_network_rates.json")
    parser.add_argument("--out", default="cigna_file_sizes.json")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    with open(args.manifest, encoding="utf-8") as source:
        items = json.load(source).get("blobs", [])
    if not items:
        raise SystemExit(f"No files found in {args.manifest}")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(check_item, index, item, args.timeout): index
            for index, item in enumerate(items)
        }
        for completed, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            size = (
                f"{result['size_gib']:.3f} GiB"
                if result["size_gib"] is not None
                else f"FAILED: {result['error']}"
            )
            print(f"[{completed}/{len(items)}] {result['name']}: {size}", flush=True)

    sized = [result for result in results if result["size_bytes"] is not None]
    results.sort(
        key=lambda result: (
            result["size_bytes"] is None,
            result["size_bytes"] if result["size_bytes"] is not None else 0,
        )
    )
    report = {
        "manifest": str(Path(args.manifest)),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(results),
        "sized_count": len(sized),
        "failed_count": len(results) - len(sized),
        "smallest": min(sized, key=lambda result: result["size_bytes"]) if sized else None,
        "largest": max(sized, key=lambda result: result["size_bytes"]) if sized else None,
        "files": results,
    }
    with open(args.out, "w", encoding="utf-8") as destination:
        json.dump(report, destination, indent=2)
        destination.write("\n")

    print(f"\nWrote report to {args.out}")
    if report["smallest"]:
        print(f"Smallest: {report['smallest']['name']} ({report['smallest']['size_bytes']:,} bytes)")
        print(f"Largest:  {report['largest']['name']} ({report['largest']['size_bytes']:,} bytes)")
    if report["failed_count"]:
        print(f"Warning: {report['failed_count']} file(s) had no usable size", file=sys.stderr)


if __name__ == "__main__":
    main()