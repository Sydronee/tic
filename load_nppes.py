#!/usr/bin/env python3
"""
load_nppes.py
--------------
Load the CMS NPPES full-replacement CSV into enrichment.duckdb, filtered
to only the NPIs that appear in transparency.duckdb's providers table.

The NPPES file is ~11GB with ~330 columns and ~8M rows. This script uses
DuckDB's native CSV reader with column projection and an IN-subquery filter
so only the rows and columns you actually need are materialised.

Typical run time: 3–6 minutes depending on disk speed.

Usage:
    python load_nppes.py
    python load_nppes.py --nppes /path/to/npidata_pfile_*.csv
    python load_nppes.py --all-npis   # load all active NPIs (no filter)

Refresh cycle: monthly (re-run to replace the table in enrichment.duckdb).
"""

import argparse
import time
from pathlib import Path

import duckdb

# --------------------------------------------------------------------- #
# Defaults                                                               #
# --------------------------------------------------------------------- #
DEFAULT_NPPES_GLOB   = "npidata_pfile_*.csv"
DEFAULT_ENRICHMENT   = "enrichment.duckdb"
DEFAULT_TRANSPARENCY = "transparency.duckdb"
SCHEMA_FILE          = "schema_enrichment.sql"
MEMORY_LIMIT         = "6GB"    # headroom for the 11 GB CSV read + join

# NPPES column names exactly as they appear in the file header.
# Quoting is required because they contain spaces and parentheses.
COL_NPI             = "NPI"
COL_ENTITY_TYPE     = "Entity Type Code"
COL_ORG_NAME        = "Provider Organization Name (Legal Business Name)"
COL_LAST_NAME       = "Provider Last Name (Legal Name)"
COL_FIRST_NAME      = "Provider First Name"
COL_CITY            = "Provider Business Practice Location Address City Name"
COL_STATE           = "Provider Business Practice Location Address State Name"
COL_ZIP             = "Provider Business Practice Location Address Postal Code"
COL_TAXONOMY        = "Healthcare Provider Taxonomy Code_1"
COL_TAX_SWITCH      = "Healthcare Provider Primary Taxonomy Switch_1"
COL_DEACTIVATION    = "NPI Deactivation Date"


def q(col: str) -> str:
    """Double-quote a column name for use in SQL."""
    return f'"{col}"'


def find_nppes_file(hint: str | None) -> Path:
    """Resolve the NPPES CSV path from an explicit path or a glob."""
    if hint:
        p = Path(hint)
        if p.is_file():
            return p
        raise FileNotFoundError(f"NPPES file not found: {hint}")

    # Try common download locations
    candidates = list(Path(".").glob(DEFAULT_NPPES_GLOB))
    candidates += list(Path("./downloads").glob(DEFAULT_NPPES_GLOB))
    if not candidates:
        raise FileNotFoundError(
            f"Could not find {DEFAULT_NPPES_GLOB} in the current directory. "
            "Pass --nppes /path/to/npidata_pfile_*.csv explicitly."
        )
    if len(candidates) > 1:
        # Prefer the most recently modified file (latest monthly release)
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        print(f"  [warn] Multiple NPPES files found; using most recent: {candidates[0]}")
    return candidates[0]


def build_insert_sql(nppes_path: Path, all_npis: bool) -> str:
    """
    Build the INSERT … SELECT statement.

    Key choices:
    - all_varchar=true: avoids DuckDB type-inference failures on the
      Deactivation Date column (empty string vs date string mix).
    - IN subquery: DuckDB executes this as a hash-join, efficient for
      238k NPIs. No temp file needed.
    - LEFT(REPLACE(zip, '-', ''), 5): normalises 5- and 9-digit ZIPs.
    - CASE on Entity Type: unified provider_name for individuals/orgs.
    - WHERE deactivation IS NULL OR = '': excludes deactivated records.
    """

    npi_filter = (
        ""
        if all_npis
        else f"""
        AND CAST({q(COL_NPI)} AS BIGINT) IN (
            SELECT DISTINCT npi
            FROM transparency.providers
            WHERE npi IS NOT NULL
        )"""
    )

    return f"""
        INSERT INTO nppes
        SELECT
            CAST({q(COL_NPI)} AS BIGINT)                                  AS npi,
            {q(COL_ENTITY_TYPE)}                                           AS entity_type,
            CASE
                WHEN {q(COL_ENTITY_TYPE)} = '2'
                    THEN {q(COL_ORG_NAME)}
                ELSE TRIM({q(COL_FIRST_NAME)} || ' ' || {q(COL_LAST_NAME)})
            END                                                            AS provider_name,
            NULLIF(TRIM({q(COL_FIRST_NAME)}), '')                         AS first_name,
            NULLIF(TRIM({q(COL_LAST_NAME)}), '')                          AS last_name,
            NULLIF(TRIM({q(COL_ORG_NAME)}), '')                           AS org_name,
            NULLIF(TRIM({q(COL_CITY)}), '')                               AS city,
            NULLIF(TRIM({q(COL_STATE)}), '')                              AS state,
            NULLIF(
                LEFT(REPLACE({q(COL_ZIP)}, '-', ''), 5),
            '')                                                            AS zip5,
            NULLIF(TRIM({q(COL_TAXONOMY)}), '')                           AS taxonomy_code,
            NULLIF(TRIM({q(COL_TAX_SWITCH)}), '')                         AS taxonomy_is_primary
        FROM read_csv(
            '{nppes_path.as_posix()}',
            header       = true,
            all_varchar  = true,
            parallel     = true
        )
        WHERE
            ({q(COL_DEACTIVATION)} IS NULL OR {q(COL_DEACTIVATION)} = '')
            {npi_filter}
    """


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nppes",          default=None,                help="Path to npidata_pfile_*.csv")
    ap.add_argument("--enrichment-db",  default=DEFAULT_ENRICHMENT,  help="Path to enrichment.duckdb (created if absent)")
    ap.add_argument("--transparency-db",default=DEFAULT_TRANSPARENCY, help="Path to transparency.duckdb (read-only)")
    ap.add_argument("--all-npis",       action="store_true",         help="Load all active NPIs instead of filtering to known providers")
    ap.add_argument("--memory",         default=MEMORY_LIMIT,        help="DuckDB memory limit (default: 6GB)")
    args = ap.parse_args()

    # ----------------------------------------------------------------- #
    # Resolve paths                                                       #
    # ----------------------------------------------------------------- #
    nppes_path = find_nppes_file(args.nppes)
    enrichment_db = Path(args.enrichment_db)
    transparency_db = Path(args.transparency_db)

    print(f"NPPES file      : {nppes_path}  ({nppes_path.stat().st_size / 1e9:.1f} GB)")
    print(f"Enrichment DB   : {enrichment_db}")
    print(f"Transparency DB : {transparency_db}")
    print(f"NPI filter      : {'ALL active NPIs' if args.all_npis else 'providers in transparency.duckdb only'}")
    print()

    if not args.all_npis and not transparency_db.exists():
        raise FileNotFoundError(
            f"transparency.duckdb not found at '{transparency_db}'. "
            "Either run stream_parser.py first, or pass --all-npis to skip the filter."
        )

    # ----------------------------------------------------------------- #
    # Connect and configure                                               #
    # ----------------------------------------------------------------- #
    con = duckdb.connect(str(enrichment_db))
    con.execute(f"SET memory_limit='{args.memory}'")
    con.execute("SET preserve_insertion_order = false")   # faster bulk load

    # Attach transparency DB read-only so we can pull the NPI filter set
    if not args.all_npis:
        con.execute(f"ATTACH '{transparency_db.as_posix()}' AS transparency (READ_ONLY)")

    # ----------------------------------------------------------------- #
    # Apply schema (idempotent)                                           #
    # ----------------------------------------------------------------- #
    if Path(SCHEMA_FILE).exists():
        print(f"Applying schema from {SCHEMA_FILE}...")
        with open(SCHEMA_FILE, encoding="utf-8") as f:
            con.execute(f.read())
    else:
        print(f"[warn] {SCHEMA_FILE} not found — skipping schema init. "
              "nppes table must already exist in enrichment.duckdb.")

    # ----------------------------------------------------------------- #
    # Drop and recreate nppes for a clean monthly refresh                 #
    # ----------------------------------------------------------------- #
    print("Dropping existing nppes table (clean refresh)...")
    con.execute("DROP TABLE IF EXISTS nppes")
    con.execute("""
        CREATE TABLE nppes (
            npi               BIGINT PRIMARY KEY,
            entity_type       VARCHAR,
            provider_name     VARCHAR,
            first_name        VARCHAR,
            last_name         VARCHAR,
            org_name          VARCHAR,
            city              VARCHAR,
            state             VARCHAR,
            zip5              VARCHAR,
            taxonomy_code     VARCHAR,
            taxonomy_is_primary VARCHAR
        )
    """)

    # ----------------------------------------------------------------- #
    # Load                                                                #
    # ----------------------------------------------------------------- #
    insert_sql = build_insert_sql(nppes_path, args.all_npis)

    print("Loading NPPES data... (this takes 3–6 minutes)")
    t0 = time.time()

    con.execute(insert_sql)

    elapsed = time.time() - t0

    # ----------------------------------------------------------------- #
    # Post-load indexes                                                   #
    # ----------------------------------------------------------------- #
    print("Building indexes...")
    con.execute("CREATE INDEX IF NOT EXISTS idx_nppes_npi   ON nppes(npi)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_nppes_state ON nppes(state)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_nppes_zip   ON nppes(zip5)")

    # ----------------------------------------------------------------- #
    # Summary                                                             #
    # ----------------------------------------------------------------- #
    n = con.execute("SELECT COUNT(*) FROM nppes").fetchone()[0]
    n_orgs = con.execute("SELECT COUNT(*) FROM nppes WHERE entity_type = '2'").fetchone()[0]
    n_indiv = con.execute("SELECT COUNT(*) FROM nppes WHERE entity_type = '1'").fetchone()[0]
    n_states = con.execute("SELECT COUNT(DISTINCT state) FROM nppes").fetchone()[0]
    null_zip = con.execute("SELECT COUNT(*) FROM nppes WHERE zip5 IS NULL").fetchone()[0]

    con.execute("CHECKPOINT")
    con.close()

    print()
    print(f"Done in {elapsed:.0f}s")
    print(f"  Total rows loaded  : {n:,}")
    print(f"  Organizations      : {n_orgs:,}")
    print(f"  Individuals        : {n_indiv:,}")
    print(f"  States covered     : {n_states}")
    print(f"  Missing ZIP        : {null_zip:,}")
    print()
    print(f"enrichment.duckdb is ready. Next step: load the ZIP→county crosswalk.")
    print("  Run: python load_zip_county.py")


if __name__ == "__main__":
    main()