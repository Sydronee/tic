#!/usr/bin/env python3
"""
build_benchmarks.py  (v2 — fast)
----------------------------------
Builds the benchmarks materialized tables in transparency.duckdb.

Key fix over v1: eliminated UNNEST from the main join path.
Instead we pre-build a lean bridge table (provider_reference_id → one NPI)
so the rate×provider join is a plain equi-join on a BIGINT column.

Run from TiC/:
    python build_benchmarks.py [--drop-first] [--stats-only]
"""

import argparse
import time
from pathlib import Path
import duckdb


def hms(secs):
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"


def build(transparency_db, enrichment_db, drop_first, stats_only):
    t_total = time.time()

    if not Path(transparency_db).exists():
        raise FileNotFoundError(transparency_db)
    if not Path(enrichment_db).exists():
        raise FileNotFoundError(enrichment_db)

    print(f"Connecting to {transparency_db} ...")
    con = duckdb.connect(transparency_db)
    con.execute(f"ATTACH '{enrichment_db}' AS ref (READ_ONLY)")
    con.execute("PRAGMA threads=8")          # use all cores
    con.execute("PRAGMA memory_limit='4GB'") # generous working memory

    # ------------------------------------------------------------------
    # STEP 1: provider bridge — one representative NPI per
    #         provider_reference_id (no UNNEST in the hot path)
    #
    # The MRF spec allows many NPIs per reference_id (network bundles),
    # but for rate benchmarking we need one location. We pick the NPI
    # with the smallest provider_id (stable, deterministic).
    # A separate unnest-based table handles true per-NPI breakdowns.
    # ------------------------------------------------------------------
    if not stats_only:
        print("\nStep 1/4 — building provider bridge ...")
        t0 = time.time()
        con.execute("DROP TABLE IF EXISTS _provider_bridge")
        con.execute("""
            CREATE TEMP TABLE _provider_bridge AS
            SELECT
                provider_reference_id,
                -- One NPI per reference_id: pick the org/facility NPI
                -- if there's a choice (entity_type='2'), else any.
                FIRST(npi ORDER BY
                    CASE WHEN n.entity_type = '2' THEN 0 ELSE 1 END,
                    p.provider_id
                )                           AS npi,
                FIRST(p.facility_name ORDER BY
                    CASE WHEN n.entity_type = '2' THEN 0 ELSE 1 END,
                    p.provider_id
                )                           AS facility_name,
                FIRST(p.tin_type  ORDER BY p.provider_id) AS tin_type,
                FIRST(p.tin_value ORDER BY p.provider_id) AS tin_value,
                FIRST(p.network_name ORDER BY p.provider_id) AS network_name,
                COUNT(DISTINCT p.npi)       AS npi_count   -- how many NPIs bundled
            FROM providers p
            LEFT JOIN ref.nppes n ON n.npi = p.npi
            WHERE provider_reference_id IS NOT NULL
            GROUP BY provider_reference_id
        """)
        n = con.execute("SELECT COUNT(*) FROM _provider_bridge").fetchone()[0]
        print(f"  bridge rows: {n:,}  ({hms(time.time()-t0)})")

        # ------------------------------------------------------------------
        # STEP 2: ZIP → dominant county (1-to-1 for map joins)
        # ------------------------------------------------------------------
        print("Step 2/4 — building county lookup ...")
        t0 = time.time()
        con.execute("DROP TABLE IF EXISTS _dominant_county")
        con.execute("""
            CREATE TEMP TABLE _dominant_county AS
            SELECT DISTINCT ON (zip5)
                zip5,
                fips        AS county_fips,
                county_name,
                state_abbr  AS county_state
            FROM ref.zip_county
            WHERE zip5 IS NOT NULL
            ORDER BY zip5, tot_ratio DESC
        """)
        print(f"  county rows: {con.execute('SELECT COUNT(*) FROM _dominant_county').fetchone()[0]:,}  ({hms(time.time()-t0)})")

        # ------------------------------------------------------------------
        # STEP 3: benchmarks — rate-grain, one row per negotiated_rate row,
        #         joined to one provider via the bridge (no UNNEST).
        #
        # Rate filter: negotiated/fee-schedule/derived, >0, <10M.
        # RC billing codes: normalised to 4-digit zero-padded.
        # Description: canonical (enrichment) preferred over MRF free-text.
        # ------------------------------------------------------------------
        if drop_first:
            print("Step 3/4 — dropping old benchmarks ...")
            con.execute("DROP TABLE IF EXISTS benchmarks")

        print("Step 3/4 — building benchmarks ...")
        t0 = time.time()
        con.execute("""
            CREATE TABLE IF NOT EXISTS benchmarks AS
            SELECT
                -- Rate identity
                r.payer_id,
                r.code_id,

                -- Code dimensions
                CASE WHEN bc.billing_code_type = 'RC'
                     THEN LPAD(TRIM(bc.billing_code), 4, '0')
                     ELSE bc.billing_code
                END                                     AS billing_code,
                bc.billing_code_type,
                bc.billing_code_type_version,
                COALESCE(cd.description,
                         bc.description,
                         bc.name)                       AS code_description,
                (cd.description IS NOT NULL)            AS has_canonical_description,

                -- Rate facts
                r.negotiation_arrangement,
                r.billing_class,
                r.setting,
                r.negotiated_type,
                r.negotiated_rate,
                r.service_code,
                r.billing_code_modifier,
                r.expiration_date,
                r.ingested_at,

                -- Payer
                py.reporting_entity_name                AS payer_name,
                py.reporting_entity_type,
                py.plan_name,
                py.plan_market_type,
                py.last_updated_on                      AS rate_file_date,

                -- Provider (from bridge — one NPI per reference_id)
                -- provider_reference_ids[1] is the first/only ref in the array;
                -- network-wide rates have [0] which returns NULL from bridge.
                r.provider_reference_ids[1]             AS provider_reference_id,
                pb.npi_count,
                pb.npi,
                COALESCE(pb.facility_name,
                         n.provider_name)               AS provider_name,
                pb.tin_type,
                pb.tin_value,
                pb.network_name,

                -- Provider geo (NPPES)
                n.entity_type                           AS provider_entity_type,
                n.city                                  AS provider_city,
                n.state                                 AS provider_state,
                n.zip5                                  AS provider_zip5,
                n.taxonomy_code                         AS provider_taxonomy_code,

                -- County geo
                dc.county_fips,
                dc.county_name,
                dc.county_state,

                -- Source
                r.source_file

            FROM negotiated_rates r

            -- Rate quality filter (remove garbage rows)
            WHERE r.negotiated_type IN ('negotiated', 'fee schedule', 'derived')
              AND r.negotiated_rate  > 0
              AND r.negotiated_rate  < 10000000

            -- Code lookup
            JOIN billing_codes bc
                ON bc.code_id = r.code_id

            -- Code description (LEFT — missing description is OK)
            LEFT JOIN ref.code_descriptions cd
                ON  cd.billing_code_type = bc.billing_code_type
                AND cd.billing_code      = CASE WHEN bc.billing_code_type = 'RC'
                                                THEN LPAD(TRIM(bc.billing_code), 4, '0')
                                                ELSE bc.billing_code END

            -- Payer
            JOIN payers py
                ON py.payer_id = r.payer_id

            -- Provider bridge — equi-join on first element of the array
            -- (avoids UNNEST; covers ~99% of rates correctly)
            LEFT JOIN _provider_bridge pb
                ON pb.provider_reference_id = r.provider_reference_ids[1]
               AND r.provider_reference_ids[1] != 0

            -- NPPES (LEFT — not all NPIs in our subset)
            LEFT JOIN ref.nppes n
                ON n.npi = pb.npi
            
            -- County
            LEFT JOIN _dominant_county dc
                ON dc.zip5 = n.zip5
        """)

        n_bm = con.execute("SELECT COUNT(*) FROM benchmarks").fetchone()[0]
        print(f"  benchmarks: {n_bm:,} rows  ({hms(time.time()-t0)})")

        print("Building indexes ...")
        t0 = time.time()
        for sql in [
            "CREATE INDEX IF NOT EXISTS idx_bm_code     ON benchmarks(billing_code, billing_code_type)",
            "CREATE INDEX IF NOT EXISTS idx_bm_payer    ON benchmarks(payer_name)",
            "CREATE INDEX IF NOT EXISTS idx_bm_state    ON benchmarks(provider_state)",
            "CREATE INDEX IF NOT EXISTS idx_bm_county   ON benchmarks(county_fips)",
            "CREATE INDEX IF NOT EXISTS idx_bm_npi      ON benchmarks(npi)",
            "CREATE INDEX IF NOT EXISTS idx_bm_rate     ON benchmarks(negotiated_rate)",
        ]:
            con.execute(sql)
        print(f"  Done ({hms(time.time()-t0)})")

    # ------------------------------------------------------------------
    # STEP 4: stat tables (fast aggregations over benchmarks)
    # ------------------------------------------------------------------
    print("\nStep 4/4 — building stat tables ...")

    stats = [
        ("benchmarks_code_stats", """
            SELECT
                billing_code, billing_code_type,
                MAX(code_description)                               AS code_description,
                BOOL_OR(has_canonical_description)                  AS has_canonical_description,
                COUNT(*)                                            AS n_rates,
                COUNT(DISTINCT npi)                                 AS n_providers,
                COUNT(DISTINCT payer_name)                          AS n_payers,
                COUNT(DISTINCT county_fips)                         AS n_counties,
                ROUND(MIN(negotiated_rate),   2)                    AS rate_min,
                ROUND(PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_p10,
                ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_p25,
                ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_median,
                ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_p75,
                ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_p90,
                ROUND(MAX(negotiated_rate),   2)                    AS rate_max,
                ROUND(AVG(negotiated_rate),   2)                    AS rate_avg,
                ROUND(STDDEV(negotiated_rate),2)                    AS rate_stddev
            FROM benchmarks
            GROUP BY billing_code, billing_code_type
        """),
        ("benchmarks_geo_stats", """
            SELECT
                billing_code, billing_code_type,
                MAX(code_description)                               AS code_description,
                provider_state, county_fips,
                MAX(county_name)                                    AS county_name,
                COUNT(*)                                            AS n_rates,
                COUNT(DISTINCT npi)                                 AS n_providers,
                COUNT(DISTINCT payer_name)                          AS n_payers,
                ROUND(MIN(negotiated_rate),   2)                    AS rate_min,
                ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_p25,
                ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_median,
                ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_p75,
                ROUND(MAX(negotiated_rate),   2)                    AS rate_max,
                ROUND(AVG(negotiated_rate),   2)                    AS rate_avg
            FROM benchmarks
            WHERE provider_state IS NOT NULL
            GROUP BY billing_code, billing_code_type, provider_state, county_fips
        """),
        ("benchmarks_payer_stats", """
            SELECT
                billing_code, billing_code_type,
                MAX(code_description)                               AS code_description,
                payer_name, MAX(plan_market_type)                   AS plan_market_type,
                COUNT(*)                                            AS n_rates,
                COUNT(DISTINCT npi)                                 AS n_providers,
                COUNT(DISTINCT provider_state)                      AS n_states,
                ROUND(MIN(negotiated_rate),   2)                    AS rate_min,
                ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_p25,
                ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_median,
                ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_p75,
                ROUND(MAX(negotiated_rate),   2)                    AS rate_max,
                ROUND(AVG(negotiated_rate),   2)                    AS rate_avg
            FROM benchmarks
            GROUP BY billing_code, billing_code_type, payer_name
        """),
        ("benchmarks_provider_stats", """
            SELECT
                billing_code, billing_code_type,
                MAX(code_description)                               AS code_description,
                npi,
                MAX(provider_name)                                  AS provider_name,
                MAX(provider_city)                                  AS provider_city,
                MAX(provider_state)                                  AS provider_state,
                MAX(provider_zip5)                                  AS provider_zip5,
                MAX(county_fips)                                    AS county_fips,
                MAX(county_name)                                    AS county_name,
                MAX(provider_taxonomy_code)                         AS taxonomy_code,
                COUNT(*)                                            AS n_rates,
                COUNT(DISTINCT payer_name)                          AS n_payers,
                ROUND(MIN(negotiated_rate),   2)                    AS rate_min,
                ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY negotiated_rate), 2) AS rate_median,
                ROUND(MAX(negotiated_rate),   2)                    AS rate_max,
                ROUND(AVG(negotiated_rate),   2)                    AS rate_avg
            FROM benchmarks
            WHERE npi IS NOT NULL
            GROUP BY billing_code, billing_code_type, npi
        """),
    ]

    for tname, sql in stats:
        t0 = time.time()
        con.execute(f"DROP TABLE IF EXISTS {tname}")
        con.execute(f"CREATE TABLE {tname} AS {sql}")
        n = con.execute(f"SELECT COUNT(*) FROM {tname}").fetchone()[0]
        print(f"  {tname}: {n:,} rows  ({hms(time.time()-t0)})")

    # ------------------------------------------------------------------
    # Sanity report
    # ------------------------------------------------------------------
    print("\nSanity check:")
    cov = con.execute("""
        SELECT billing_code_type,
               COUNT(*)                                             AS n_rates,
               ROUND(100.0*SUM(has_canonical_description::INT)/COUNT(*),1) AS canonical_pct,
               COUNT(DISTINCT billing_code)                         AS distinct_codes
        FROM benchmarks
        GROUP BY billing_code_type
        ORDER BY n_rates DESC
    """).df()
    print(cov.to_string(index=False))

    geo = con.execute("""
        SELECT
            COUNT(*)                                                AS total_rates,
            ROUND(100.0*COUNT(npi)/COUNT(*),1)                     AS npi_pct,
            ROUND(100.0*COUNT(provider_state)/COUNT(*),1)          AS geo_pct,
            ROUND(100.0*COUNT(county_fips)/COUNT(*),1)             AS county_pct
        FROM benchmarks
    """).fetchone()
    print(f"\n  NPI coverage: {geo[1]}%  |  State: {geo[2]}%  |  County: {geo[3]}%")

    print("\nCheckpointing ...")
    con.execute("PRAGMA force_compression='zstd'")
    con.execute("CHECKPOINT")
    con.close()
    print(f"\nFinished in {hms(time.time()-t_total)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transparency-db", default="transparency.duckdb")
    ap.add_argument("--enrichment-db",   default="enrichment.duckdb")
    ap.add_argument("--drop-first",      action="store_true",
                    help="DROP TABLE benchmarks before rebuild")
    ap.add_argument("--stats-only",      action="store_true",
                    help="Skip benchmarks rebuild; only redo stat tables")
    args = ap.parse_args()
    build(args.transparency_db, args.enrichment_db, args.drop_first, args.stats_only)

if __name__ == "__main__":
    main()
