"""Shared download and run-log helpers for the ingestion runners."""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

import requests

HTTP_HEADERS = {
    "User-Agent": "TiC-price-transparency-pipeline/1.0",
}
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(
    url: str,
    target_path: str,
    *,
    expected_size: int | None = None,
    retries: int = 4,
    connect_timeout: float = 30,
    read_timeout: float = 300,
) -> dict[str, Any]:
    """Download atomically with retries and optional manifest-size validation."""
    if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
        size = os.path.getsize(target_path)
        if expected_size is None or size == expected_size:
            return {"path": target_path, "bytes": size, "sha256": _file_sha256(target_path), "cached": True}
        os.remove(target_path)

    os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
    temp_target = f"{target_path}.tmp"
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        try:
            with requests.get(
                url,
                headers=HTTP_HEADERS,
                stream=True,
                timeout=(connect_timeout, read_timeout),
            ) as response:
                if response.status_code in RETRYABLE_STATUS_CODES:
                    response.raise_for_status()
                response.raise_for_status()
                # Manifest sizes refer to the compressed object. Requests
                # normally auto-decompresses Content-Encoding: gzip while
                # iterating, which would make valid downloads fail size checks.
                response.raw.decode_content = False
                with open(temp_target, "wb") as stream:
                    for chunk in response.raw.stream(1024 * 1024, decode_content=False):
                        if chunk:
                            stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())

            size = os.path.getsize(temp_target)
            if size == 0:
                raise IOError("download produced an empty file")
            if expected_size is not None and size != expected_size:
                raise IOError(f"download size mismatch: expected {expected_size}, got {size}")

            os.replace(temp_target, target_path)
            return {"path": target_path, "bytes": size, "sha256": _file_sha256(target_path), "cached": False}
        except (requests.RequestException, OSError, IOError) as exc:
            last_error = exc
            if os.path.exists(temp_target):
                os.remove(temp_target)
            if attempt == retries:
                break
            delay = min(60.0, 2**attempt) + random.uniform(0, 0.25)
            time.sleep(delay)

    raise RuntimeError(f"download failed after {retries + 1} attempts: {url}") from last_error


def append_run_log(log_path: str, **fields: Any) -> None:
    """Append one machine-readable event and flush it immediately."""
    record = {"timestamp": _timestamp(), **fields}
    with open(log_path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
