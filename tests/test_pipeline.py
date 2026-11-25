import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from fetch_cigna_index import iter_rate_files
from ingest_utils import download_file, validate_manifest


class FakeRaw:
    def __init__(self, chunks):
        self.chunks = chunks
        self.decode_content = True

    def stream(self, _chunk_size, decode_content=False):
        self.decode_content = decode_content
        yield from self.chunks


class FakeResponse:
    def __init__(self, status_code, chunks=()):
        self.status_code = status_code
        self.raw = FakeRaw(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class PipelineTests(unittest.TestCase):
    def test_cigna_manifest_filters_and_deduplicates_rate_files(self):
        index = {
            "reporting_structure": [
                {
                    "reporting_plans": [{"plan_name": "Example"}],
                    "in_network_files": [
                        {"location": "https://example.test/a-in-network-rates.json.gz"},
                        {"location": "https://example.test/other.json"},
                        {"location": "https://example.test/a-in-network-rates.json.gz"},
                    ],
                }
            ]
        }

        files = list(iter_rate_files(index))

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["name"], "a-in-network-rates.json.gz")
        self.assertEqual(files[0]["plan_name"], "Example")

    @patch("ingest_utils.time.sleep")
    @patch("ingest_utils.requests.get")
    def test_download_retries_and_writes_atomically(self, get, _sleep):
        get.side_effect = [
            FakeResponse(503),
            FakeResponse(200, [b"hello", b" world"]),
        ]

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "download.bin"
            result = download_file("https://example.test/file", str(target), retries=1)

            self.assertEqual(target.read_bytes(), b"hello world")
            self.assertEqual(result["bytes"], 11)
            self.assertFalse((target.with_name("download.bin.tmp")).exists())
            self.assertEqual(get.call_count, 2)

    @patch("ingest_utils.time.sleep")
    @patch("ingest_utils.requests.get")
    def test_download_does_not_retry_not_found_or_forbidden(self, get, _sleep):
        for status_code in (403, 404):
            with self.subTest(status_code=status_code):
                get.reset_mock()
                get.return_value = FakeResponse(status_code)
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory) / "download.bin"
                    with self.assertRaises(requests.HTTPError):
                        download_file("https://example.test/file", str(target), retries=3)
                self.assertEqual(get.call_count, 1)
                self.assertFalse(_sleep.called)

    def test_manifest_payload_is_json_serializable(self):
        files = list(iter_rate_files({"reporting_structure": []}))
        self.assertEqual(json.dumps({"blobs": files}), '{"blobs": []}')

    def test_manifest_validation_reports_invalid_entries(self):
        with self.assertRaisesRegex(ValueError, "missing a non-empty downloadUrl"):
            validate_manifest({"blobs": [{"name": "missing-url"}]})


if __name__ == "__main__":
    unittest.main()
