#!/usr/bin/env python3
"""
load_zip_county.py
-------------------
Download the Census Bureau ZCTA-to-County relationship file and load it
into the zip_county table in enrichment.duckdb.

No account or API key required — the Census file is publicly available.

Source:
    Census 2020 ZCTA-to-County relationship file (pipe-delimited)
    https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/
        tab20_zcta520_county20_natl.txt

The file maps each ZIP Code Tabulation Area (ZCTA, i.e. ZIP) to one or
more counties with an area-based overlap ratio. A ZIP that spans two
counties gets two rows — use tot_ratio to pick the dominant one if you
need a single county per ZIP.

Usage:
    python load_zip_county.py
    python load_zip_county.py --file /path/to/tab20_zcta520_county20_natl.txt
    python load_zip_county.py --enrichment-db /path/to/enrichment.duckdb

Refresh cycle: rarely — the ZIP/county relationship is stable across years.
Re-run if Census releases a new ZCTA vintage (every ~10 years).
"""

import argparse
import io
import time
from pathlib import Path

import requests
import duckdb

# --------------------------------------------------------------------- #
# Constants                                                               #
# --------------------------------------------------------------------- #
DEFAULT_ENRICHMENT = "enrichment.duckdb"
CENSUS_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
    "tab20_zcta520_county20_natl.txt"
)
LOCAL_CACHE = "tab20_zcta520_county20_natl.txt"

# State FIPS → 2-letter abbreviation (all 50 + DC + territories)
STATE_FIPS_TO_ABBR = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA",
    "08": "CO", "09": "CT", "10": "DE", "11": "DC", "12": "FL",
    "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
    "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME",
    "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
    "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
    "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR",
    "78": "VI",
}


def fetch_census_file(local_hint: str | None) -> list[dict]:
    """
    Return the parsed rows from the Census ZCTA-county file.

    Priority:
        1. Explicit --file path
        2. Cached local copy (LOCAL_CACHE)
        3. Download from Census
    """
    # 1. Explicit path
    if local_hint:
        path = Path(local_hint)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {local_hint}")
        print(f"Using local file: {path}")
        text = path.read_text(encoding="utf-8")
        return _parse_census_text(text)

    # 2. Cached copy
    cache = Path(LOCAL_CACHE)
    if cache.exists():
        print(f"Using cached file: {cache}")
        text = cache.read_text(encoding="utf-8")
        return _parse_census_text(text)

    # 3. Download
    print(f"Downloading Census ZCTA-county file from:\n  {CENSUS_URL}")
    resp = requests.get(CENSUS_URL, timeout=120)
    resp.raise_for_status()
    text = resp.text

    # Cache for future runs
    cache.write_text(text, encoding="utf-8")
    print(f"Cached to {cache} for future runs.")
    return _parse_census_text(text)


def _parse_census_text(text: str) -> list[dict]:
    """
    Parse the pipe-delimited Census file into a list of row dicts.

    Relevant columns (there are 10 total):
        GEOID_ZCTA5_20   — 5-digit ZIP/ZCTA
        GEOID_COUNTY_20  — 5-digit county FIPS
        NAMELSAD_COUNTY_20 — county name with type suffix e.g. "Travis County"
        AREALAND_PART    — land area of the ZIP-county intersection (sq metres)
        AREALAND_ZCTA5_20 — total land area of the ZIP (sq metres)

    tot_ratio = AREALAND_PART / AREALAND_ZCTA5_20 (area-based overlap).
    Note: Census uses land area for the ratio, which is a better proxy for
    population coverage than total area (avoids ocean/lake inflation).
    """
    rows = []
    lines = text.splitlines()
    if not lines:
        raise ValueError("Census file appears to be empty.")

    header = [h.strip() for h in lines[0].split("|")]
    idx = {name: i for i, name in enumerate(header)}

    required = {"GEOID_ZCTA5_20", "GEOID_COUNTY_20", "NAMELSAD_COUNTY_20",
                "AREALAND_PART", "AREALAND_ZCTA5_20"}
    missing = required - idx.keys()
    if missing:
        raise ValueError(f"Census file missing expected columns: {missing}")

    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < len(header):
            continue

        zip5   = parts[idx["GEOID_ZCTA5_20"]].strip().zfill(5)
        fips   = parts[idx["GEOID_COUNTY_20"]].strip().zfill(5)
        county = parts[idx["NAMELSAD_COUNTY_20"]].strip()
        state_fips = fips[:2]
        state_abbr = STATE_FIPS_TO_ABBR.get(state_fips, "")

        try:
            area_part = float(parts[idx["AREALAND_PART"]])
            area_zcta = float(parts[idx["AREALAND_ZCTA5_20"]])
            tot_ratio = round(area_part / area_zcta, 6) if area_zcta > 0 else 0.0
        except (ValueError, ZeroDivisionError):
            tot_ratio = 0.0

        rows.append({
            "zip5":        zip5,
            "fips":        fips,
            "county_name": county,
            "state_abbr":  state_abbr,
            "tot_ratio":   tot_ratio,
        })

    return rows


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--file",          default=None,                help="Local Census ZCTA-county file (skips download)")
    ap.add_argument("--enrichment-db", default=DEFAULT_ENRICHMENT,  help="Path to enrichment.duckdb")
    args = ap.parse_args()

    enrichment_db = Path(args.enrichment_db)
    if not enrichment_db.exists():
        raise FileNotFoundError(
            f"enrichment.duckdb not found at '{enrichment_db}'. "
            "Run load_nppes.py first."
        )

    # ----------------------------------------------------------------- #
    # Parse source file                                                   #
    # ----------------------------------------------------------------- #
    t0 = time.time()
    rows = fetch_census_file(args.file)
    print(f"Parsed {len(rows):,} ZIP-county rows in {time.time() - t0:.1f}s")

    # ----------------------------------------------------------------- #
    # Load into enrichment.duckdb                                         #
    # ----------------------------------------------------------------- #
    con = duckdb.connect(str(enrichment_db))

    print("Loading into zip_county table...")
    t1 = time.time()

    con.execute("DROP TABLE IF EXISTS zip_county")
    con.execute("""
        CREATE TABLE zip_county (
            zip5        VARCHAR,
            fips        VARCHAR,
            county_name VARCHAR,
            state_abbr  VARCHAR,
            tot_ratio   DOUBLE
        )
    """)

    # Bulk insert via DuckDB's VALUES from a Python list
    con.executemany(
        "INSERT INTO zip_county VALUES (?, ?, ?, ?, ?)",
        [(r["zip5"], r["fips"], r["county_name"], r["state_abbr"], r["tot_ratio"])
         for r in rows],
    )

    con.execute("CREATE INDEX IF NOT EXISTS idx_zip_county_zip  ON zip_county(zip5)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_zip_county_fips ON zip_county(fips)")
    con.execute("CHECKPOINT")

    elapsed = time.time() - t1

    # ----------------------------------------------------------------- #
    # Summary                                                             #
    # ----------------------------------------------------------------- #
    n          = con.execute("SELECT COUNT(*) FROM zip_county").fetchone()[0]
    n_zips     = con.execute("SELECT COUNT(DISTINCT zip5) FROM zip_county").fetchone()[0]
    n_counties = con.execute("SELECT COUNT(DISTINCT fips) FROM zip_county").fetchone()[0]
    n_states   = con.execute("SELECT COUNT(DISTINCT state_abbr) FROM zip_county").fetchone()[0]

    # ZIPs that cross county lines (tot_ratio < 1 for at least one mapping)
    n_split = con.execute("""
        SELECT COUNT(DISTINCT zip5) FROM zip_county
        WHERE zip5 IN (SELECT zip5 FROM zip_county GROUP BY zip5 HAVING COUNT(*) > 1)
    """).fetchone()[0]

    # Quick sanity: how many of your NPPES ZIPs resolve to a county?
    match_pct = con.execute("""
        SELECT ROUND(100.0 * COUNT(DISTINCT n.zip5) /
               NULLIF((SELECT COUNT(DISTINCT zip5) FROM nppes WHERE zip5 IS NOT NULL), 0), 1)
        FROM nppes n
        JOIN zip_county z ON z.zip5 = n.zip5
    """).fetchone()[0]

    con.close()

    print()
    print(f"Done in {elapsed:.1f}s")
    print(f"  Total ZIP-county rows : {n:,}")
    print(f"  Distinct ZIPs         : {n_zips:,}")
    print(f"  Distinct counties     : {n_counties:,}")
    print(f"  States/territories    : {n_states}")
    print(f"  ZIPs spanning >1 county: {n_split:,}")
    print(f"  NPPES ZIP match rate  : {match_pct}%")
    print()
    print("Next step: load authoritative code descriptions.")
    print("  Run: python load_code_descriptions.py")


if __name__ == "__main__":
    main()