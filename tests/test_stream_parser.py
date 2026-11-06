import unittest
from pathlib import Path

import duckdb

import stream_parser


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "sample_mrf.json"


class StreamParserTests(unittest.TestCase):
    def test_process_file_keeps_institutional_rows_and_is_idempotent(self):
        con = duckdb.connect(":memory:")
        try:
            con.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))

            stream_parser.process_file(con, str(FIXTURE), "sample_mrf.json")

            rows = con.execute(
                "SELECT billing_class, setting, negotiated_rate "
                "FROM negotiated_rates ORDER BY negotiated_rate"
            ).fetchall()
            self.assertEqual(
                rows,
                [
                    ("institutional", None, 90.0),
                    ("institutional", "inpatient", 100.0),
                ],
            )

            stream_parser.process_file(con, str(FIXTURE), "sample_mrf.json")

            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM negotiated_rates").fetchone()[0],
                2,
            )
            self.assertEqual(
                con.execute("SELECT rows_kept FROM ingested_files").fetchone()[0],
                2,
            )
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
