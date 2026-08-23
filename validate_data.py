#!/usr/bin/env python3
"""
validate_data.py
-----------------
Data-quality / validity checks for transparency.duckdb, matched to the
current schema.sql (payers, billing_codes, negotiated_rates, providers).

By default this writes a plain-text report (validation_report.txt) with a
per-check pass/warn/error status, bad-row counts, and % bad, plus a summary
table ranking every check by % bad — so you can see at a glance which
fields are the least trustworthy and by how much.

Checks are grouped into sections:
  A. Structure     - do the tables/columns schema.sql defines actually exist; row counts
  B. Referential    - do foreign-key-like references resolve
  C. Completeness   - are fields that should be populated actually populated
  D. Domain         - do enumerated/bounded fields hold expected values
                       (negotiated_type, setting, billing_class, reporting_entity_type,
                       plan_market_type, tin_type, rate sanity)
  E. Format         - billing codes, NPIs (full CMS check-digit + leading-digit rule),
                       TINs, and dates look like what they claim to be
  F. Duplicates     - exact or logical duplicate rows
  G. Coverage       - informational summary (not pass/fail), one row per source_file

Usage:
    python validate_data.py --db transparency.duckdb
    python validate_data.py --db transparency.duckdb --txt report.txt
    python validate_data.py --db transparency.duckdb --json report.json
    python validate_data.py --db transparency.duckdb --fast          # skip the O(n) explode-based checks
    python validate_data.py --db transparency.duckdb --fail-on warn  # exit 1 on warnings too, not just errors
    python validate_data.py --db transparency.duckdb --sample 10

Exit code is 0 unless a check at or above --fail-on severity failed
(default --fail-on is "error"), which makes this usable as a CI gate.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from typing import Optional

import duckdb

# ---------------------------------------------------------------------------
# CMS Table-in-Network MRF domain constants
# ---------------------------------------------------------------------------

ALLOWED_BILLING_CLASS = {"institutional", "professional"}
ALLOWED_NEGOTIATED_TYPE = {"negotiated", "fee schedule", "percentage", "per diem"}
ALLOWED_SETTING = {"inpatient", "outpatient"}  # NULL is also acceptable, checked separately
# "npi" is not in the formal CMS enum (ein/ssn) but is a common real-world workaround
# payers use for a provider_group that has no TIN of its own.
ALLOWED_TIN_TYPE = {"ein", "ssn", "npi"}
# The three canonical CMS labels, plus shorthand variants seen routinely in production
# MRFs (e.g. UHC-style files commonly write just "Insurer").
ALLOWED_REPORTING_ENTITY_TYPE = {
    "group health plan", "health insurance issuer", "third-party administrator",
    "insurer", "issuer", "tpa",
}
ALLOWED_PLAN_MARKET_TYPE = {"group", "individual"}
# EIN: XX-XXXXXXX, SSN: XXX-XX-XXXX. Dashes optional either way.
TIN_PATTERN = r"^\d{2}-?\d{7}$|^\d{3}-?\d{2}-?\d{4}$"
CODE_TYPE_PATTERNS = {
    # billing_code_type -> (regex matched against TRIM(billing_code), human description)
    "CPT":    (r"^\d{5}$|^\d{4}[A-Za-z]$", "5 digits, or 4 digits + a Category III/PLA letter (e.g. 0591T)"),
    "HCPCS":  (r"^[A-Za-z]\d{4}$", "1 letter + 4 digits"),
    "MS-DRG": (r"^\d{3,4}$", "3 or 4 digits (some payers zero-pad to 4)"),
    "NDC":    (r"^\d{10,11}$", "10-11 digits"),
    "APC":    (r"^\d{4}$", "4 digits"),
    "RC":     (r"^\d{3,4}$", "3-4 digits"),
}

# Beyond these, flag negotiated_rate as a statistical outlier worth a look.
PERCENTAGE_RATE_WARN_ABOVE = 1000     # percentage-type values shouldn't realistically exceed this
DOLLAR_RATE_WARN_ABOVE = 250_000      # single-line-item dollar rates rarely exceed this


# ---------------------------------------------------------------------------
# Check result plumbing
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    section: str
    name: str
    level: str                     # "error" | "warn" | "info"
    description: str
    passed: bool
    n_bad: int = 0
    n_total: Optional[int] = None
    sample: list = field(default_factory=list)
    note: str = ""

    @property
    def pct_bad(self) -> float:
        if not self.n_total:
            return 0.0
        return 100.0 * self.n_bad / self.n_total


LEVEL_RANK = {"info": 0, "warn": 1, "error": 2}


class Validator:
    def __init__(self, con: duckdb.DuckDBPyConnection, sample_size: int = 5, fast: bool = False):
        self.con = con
        self.sample_size = sample_size
        self.fast = fast
        self.results: list[CheckResult] = []
        self._tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}

    # -- helpers ------------------------------------------------------------

    def table_exists(self, table: str) -> bool:
        return table in self._tables

    def columns(self, table: str) -> dict:
        if not self.table_exists(table):
            return {}
        return {row[1]: row[2] for row in self.con.execute(f"PRAGMA table_info('{table}')").fetchall()}

    def has_column(self, table: str, column: str) -> bool:
        return column in self.columns(table)

    def skip(self, section, name, level, description, note):
        self.results.append(CheckResult(section, name, level, description, passed=True, note=note))

    def check(self, section, name, level, description, count_sql, sample_sql=None, total_sql=None, note=""):
        n_bad = self.con.execute(count_sql).fetchone()[0] or 0
        n_total = self.con.execute(total_sql).fetchone()[0] if total_sql else None
        sample = []
        if n_bad and sample_sql:
            rows = self.con.execute(f"{sample_sql} LIMIT {self.sample_size}").fetchall()
            cols = [d[0] for d in self.con.description]
            sample = [dict(zip(cols, r)) for r in rows]
        res = CheckResult(section, name, level, description, passed=(n_bad == 0),
                           n_bad=n_bad, n_total=n_total, sample=sample, note=note)
        self.results.append(res)
        return res

    def info_table(self, section, name, description, sql):
        """Non-pass/fail informational query (e.g. coverage summary)."""
        rows = self.con.execute(sql).fetchall()
        cols = [d[0] for d in self.con.description]
        data = [dict(zip(cols, r)) for r in rows]
        res = CheckResult(section, name, "info", description, passed=True,
                           n_bad=0, n_total=len(data), sample=data)
        self.results.append(res)
        return res


# ---------------------------------------------------------------------------
# Section A: Structure
# ---------------------------------------------------------------------------

def section_structure(v: Validator):
    required_tables = {
        "payers": ["payer_id", "reporting_entity_name", "plan_id", "version", "source_file"],
        "billing_codes": ["code_id", "billing_code", "billing_code_type", "name", "negotiation_arrangement"],
        "negotiated_rates": ["payer_id", "code_id", "negotiated_type", "negotiated_rate",
                              "billing_class", "setting", "provider_reference_ids", "source_file"],
        "providers": ["provider_reference_id", "npi", "tin_type", "tin_value",
                      "facility_name", "network_name", "group_key"],
    }
    for table, cols in required_tables.items():
        exists = v.table_exists(table)
        v.results.append(CheckResult(
            "A. Structure", f"table:{table}", "error",
            f"Table '{table}' exists", passed=exists,
            n_bad=0 if exists else 1,
            note="Run schema.sql / stream_parser.py first." if not exists else "",
        ))
        if not exists:
            continue
        have = set(v.columns(table))
        missing = [c for c in cols if c not in have]
        v.results.append(CheckResult(
            "A. Structure", f"columns:{table}", "warn",
            f"Table '{table}' has all columns from the current schema.sql",
            passed=not missing, n_bad=len(missing),
            note=(f"Missing: {', '.join(missing)}. Likely an older DB that hasn't had "
                  f"schema.sql's ALTER TABLE migrations applied — re-run stream_parser.py "
                  f"or runner.py, which execute schema.sql idempotently.") if missing else "",
        ))

    present = [t for t in required_tables if v.table_exists(t)]
    if present:
        union_sql = " UNION ALL ".join(f"SELECT '{t}' AS table_name, count(*) AS n_rows FROM {t}" for t in present)
        v.info_table("A. Structure", "row_counts", "Row counts per table", union_sql)


# ---------------------------------------------------------------------------
# Section B: Referential integrity
# ---------------------------------------------------------------------------

def section_referential(v: Validator):
    if not (v.table_exists("negotiated_rates") and v.table_exists("payers")):
        return
    v.check(
        "B. Referential", "orphan_payer_id", "error",
        "Every negotiated_rates.payer_id resolves to a row in payers",
        count_sql="""
            SELECT count(*) FROM negotiated_rates r
            WHERE r.payer_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM payers p WHERE p.payer_id = r.payer_id)
        """,
        sample_sql="""
            SELECT r.payer_id, r.source_file, r.negotiated_rate
            FROM negotiated_rates r
            WHERE r.payer_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM payers p WHERE p.payer_id = r.payer_id)
        """,
        total_sql="SELECT count(*) FROM negotiated_rates",
    )

    if v.table_exists("billing_codes"):
        v.check(
            "B. Referential", "orphan_code_id", "error",
            "Every negotiated_rates.code_id resolves to a row in billing_codes",
            count_sql="""
                SELECT count(*) FROM negotiated_rates r
                WHERE r.code_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM billing_codes c WHERE c.code_id = r.code_id)
            """,
            sample_sql="""
                SELECT r.code_id, r.source_file, r.negotiated_rate
                FROM negotiated_rates r
                WHERE r.code_id IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM billing_codes c WHERE c.code_id = r.code_id)
            """,
            total_sql="SELECT count(*) FROM negotiated_rates",
        )

    if v.fast:
        v.skip("B. Referential", "orphan_provider_reference_ids", "warn",
               "Every id in negotiated_rates.provider_reference_ids resolves to a row in providers",
               note="Skipped (--fast): requires exploding provider_reference_ids across the full table.")
    elif v.table_exists("providers") and v.has_column("negotiated_rates", "provider_reference_ids"):
        v.check(
            "B. Referential", "orphan_provider_reference_ids", "warn",
            "Every id in negotiated_rates.provider_reference_ids resolves to a row in providers",
            count_sql="""
                WITH exploded AS (
                    SELECT unnest(provider_reference_ids) AS provider_reference_id
                    FROM negotiated_rates
                    WHERE provider_reference_ids IS NOT NULL AND len(provider_reference_ids) > 0
                )
                SELECT count(*) FROM exploded e
                WHERE NOT EXISTS (SELECT 1 FROM providers p WHERE p.provider_reference_id = e.provider_reference_id)
            """,
            sample_sql="""
                WITH exploded AS (
                    SELECT unnest(provider_reference_ids) AS provider_reference_id
                    FROM negotiated_rates
                    WHERE provider_reference_ids IS NOT NULL AND len(provider_reference_ids) > 0
                )
                SELECT DISTINCT provider_reference_id FROM exploded e
                WHERE NOT EXISTS (SELECT 1 FROM providers p WHERE p.provider_reference_id = e.provider_reference_id)
            """,
            total_sql="""
                SELECT count(*) FROM negotiated_rates
                WHERE provider_reference_ids IS NOT NULL AND len(provider_reference_ids) > 0
            """,
            note="% is bad-ref-count over rows-with-refs (a rough denominator, since one row can "
                 "carry multiple provider_reference_ids) — treat it as an order of magnitude, not exact.",
        )


# ---------------------------------------------------------------------------
# Section C: Completeness
# ---------------------------------------------------------------------------

def section_completeness(v: Validator):
    if v.table_exists("billing_codes"):
        v.check(
            "C. Completeness", "billing_codes_missing_code", "error",
            "billing_codes.billing_code / billing_code_type are populated",
            count_sql="SELECT count(*) FROM billing_codes WHERE billing_code IS NULL OR billing_code_type IS NULL",
            sample_sql="SELECT * FROM billing_codes WHERE billing_code IS NULL OR billing_code_type IS NULL",
            total_sql="SELECT count(*) FROM billing_codes",
        )

    if v.table_exists("negotiated_rates"):
        v.check(
            "C. Completeness", "negotiated_missing_rate", "error",
            "Rows with negotiated_type = 'negotiated' have a non-null negotiated_rate",
            count_sql="SELECT count(*) FROM negotiated_rates WHERE negotiated_type = 'negotiated' AND negotiated_rate IS NULL",
            sample_sql="SELECT * FROM negotiated_rates WHERE negotiated_type = 'negotiated' AND negotiated_rate IS NULL",
            total_sql="SELECT count(*) FROM negotiated_rates WHERE negotiated_type = 'negotiated'",
        )
        v.check(
            "C. Completeness", "rates_missing_source_file", "warn",
            "negotiated_rates.source_file is populated (needed to trace provenance)",
            count_sql="SELECT count(*) FROM negotiated_rates WHERE source_file IS NULL OR source_file = ''",
            sample_sql="SELECT * FROM negotiated_rates WHERE source_file IS NULL OR source_file = ''",
            total_sql="SELECT count(*) FROM negotiated_rates",
        )

    if v.table_exists("payers"):
        v.check(
            "C. Completeness", "payers_missing_entity_name", "warn",
            "payers.reporting_entity_name is populated",
            count_sql="SELECT count(*) FROM payers WHERE reporting_entity_name IS NULL OR reporting_entity_name = ''",
            sample_sql="SELECT * FROM payers WHERE reporting_entity_name IS NULL OR reporting_entity_name = ''",
            total_sql="SELECT count(*) FROM payers",
        )

    if v.table_exists("billing_codes") and v.has_column("billing_codes", "description"):
        v.check(
            "C. Completeness", "codes_missing_description", "warn",
            "billing_codes.description is populated (blank descriptions make codes unreadable downstream)",
            count_sql="SELECT count(*) FROM billing_codes WHERE description IS NULL OR trim(description) = ''",
            sample_sql="SELECT * FROM billing_codes WHERE description IS NULL OR trim(description) = ''",
            total_sql="SELECT count(*) FROM billing_codes",
        )

    if v.table_exists("providers"):
        v.check(
            "C. Completeness", "providers_missing_identity", "warn",
            "providers rows have at least one of NPI, facility_name, or tin_value "
            "(rows with none of the three are unusable for identity resolution)",
            count_sql="""
                SELECT count(*) FROM providers
                WHERE npi IS NULL
                  AND (facility_name IS NULL OR trim(facility_name) = '')
                  AND (tin_value IS NULL OR trim(tin_value) = '')
            """,
            sample_sql="""
                SELECT * FROM providers
                WHERE npi IS NULL
                  AND (facility_name IS NULL OR trim(facility_name) = '')
                  AND (tin_value IS NULL OR trim(tin_value) = '')
            """,
            total_sql="SELECT count(*) FROM providers",
        )


# ---------------------------------------------------------------------------
# Section D: Domain / enumerated value checks
# ---------------------------------------------------------------------------

def _in_list_sql(col, allowed):
    quoted = ", ".join(f"'{x}'" for x in sorted(allowed))
    return f"{col} IS NOT NULL AND {col} NOT IN ({quoted})"


def section_domain(v: Validator):
    if not v.table_exists("negotiated_rates"):
        return

    v.check(
        "D. Domain", "billing_class_not_institutional", "warn",
        "billing_class = 'institutional' (this pipeline's stream_parser.py filters to institutional-only by design)",
        count_sql="SELECT count(*) FROM negotiated_rates WHERE billing_class IS DISTINCT FROM 'institutional'",
        sample_sql="SELECT DISTINCT billing_class, count(*) OVER (PARTITION BY billing_class) AS n "
                   "FROM negotiated_rates WHERE billing_class IS DISTINCT FROM 'institutional'",
        total_sql="SELECT count(*) FROM negotiated_rates",
        note="If this is non-zero, either the parser's institutional-only filter regressed, "
             "or you intentionally changed it — check stream_parser.py's `billing_class` check.",
    )

    v.check(
        "D. Domain", "negotiated_type_out_of_range", "error",
        f"negotiated_type is one of {sorted(ALLOWED_NEGOTIATED_TYPE)}",
        count_sql=f"SELECT count(*) FROM negotiated_rates WHERE {_in_list_sql('negotiated_type', ALLOWED_NEGOTIATED_TYPE)}",
        sample_sql=f"SELECT * FROM negotiated_rates WHERE {_in_list_sql('negotiated_type', ALLOWED_NEGOTIATED_TYPE)}",
        total_sql="SELECT count(*) FROM negotiated_rates",
    )

    if v.has_column("negotiated_rates", "setting"):
        v.check(
            "D. Domain", "setting_out_of_range", "warn",
            f"setting is NULL or one of {sorted(ALLOWED_SETTING)}",
            count_sql=f"SELECT count(*) FROM negotiated_rates WHERE {_in_list_sql('setting', ALLOWED_SETTING)}",
            sample_sql=f"SELECT * FROM negotiated_rates WHERE {_in_list_sql('setting', ALLOWED_SETTING)}",
            total_sql="SELECT count(*) FROM negotiated_rates",
        )

    v.check(
        "D. Domain", "dollar_rate_not_positive", "error",
        "negotiated_type IN ('negotiated','fee schedule','per diem') rows have negotiated_rate > 0 "
        "(a dollar-denominated rate of $0 or less is essentially always wrong)",
        count_sql="""
            SELECT count(*) FROM negotiated_rates
            WHERE negotiated_type IN ('negotiated','fee schedule','per diem')
              AND negotiated_rate IS NOT NULL AND negotiated_rate <= 0
        """,
        sample_sql="""
            SELECT * FROM negotiated_rates
            WHERE negotiated_type IN ('negotiated','fee schedule','per diem')
              AND negotiated_rate IS NOT NULL AND negotiated_rate <= 0
        """,
        total_sql="""
            SELECT count(*) FROM negotiated_rates
            WHERE negotiated_type IN ('negotiated','fee schedule','per diem') AND negotiated_rate IS NOT NULL
        """,
    )

    v.check(
        "D. Domain", "percentage_rate_zero_or_negative", "warn",
        "negotiated_type = 'percentage' rows have negotiated_rate > 0 "
        "(0% can be a legitimate value — e.g. fully covered under another arrangement — "
        "so this is a warn, not an error; worth spot-checking, not necessarily wrong)",
        count_sql="""
            SELECT count(*) FROM negotiated_rates
            WHERE negotiated_type = 'percentage' AND negotiated_rate IS NOT NULL AND negotiated_rate <= 0
        """,
        sample_sql="""
            SELECT * FROM negotiated_rates
            WHERE negotiated_type = 'percentage' AND negotiated_rate IS NOT NULL AND negotiated_rate <= 0
        """,
        total_sql="SELECT count(*) FROM negotiated_rates WHERE negotiated_type = 'percentage' AND negotiated_rate IS NOT NULL",
    )

    v.check(
        "D. Domain", "percentage_rate_implausible", "warn",
        f"negotiated_type = 'percentage' rows have negotiated_rate <= {PERCENTAGE_RATE_WARN_ABOVE} "
        f"(a value far above that usually means a dollar amount landed in a percentage row)",
        count_sql=f"SELECT count(*) FROM negotiated_rates "
                  f"WHERE negotiated_type = 'percentage' AND negotiated_rate > {PERCENTAGE_RATE_WARN_ABOVE}",
        sample_sql=f"SELECT * FROM negotiated_rates "
                   f"WHERE negotiated_type = 'percentage' AND negotiated_rate > {PERCENTAGE_RATE_WARN_ABOVE}",
        total_sql="SELECT count(*) FROM negotiated_rates WHERE negotiated_type = 'percentage'",
    )

    v.check(
        "D. Domain", "dollar_rate_outlier", "warn",
        f"negotiated_type IN ('negotiated','fee schedule','per diem') rows have negotiated_rate <= "
        f"${DOLLAR_RATE_WARN_ABOVE:,} (flagged as an outlier worth spot-checking, not necessarily wrong)",
        count_sql=f"SELECT count(*) FROM negotiated_rates "
                  f"WHERE negotiated_type IN ('negotiated','fee schedule','per diem') "
                  f"AND negotiated_rate > {DOLLAR_RATE_WARN_ABOVE}",
        sample_sql=f"SELECT * FROM negotiated_rates "
                   f"WHERE negotiated_type IN ('negotiated','fee schedule','per diem') "
                   f"AND negotiated_rate > {DOLLAR_RATE_WARN_ABOVE} ORDER BY negotiated_rate DESC",
        total_sql="SELECT count(*) FROM negotiated_rates WHERE negotiated_type IN ('negotiated','fee schedule','per diem')",
    )

    if v.table_exists("payers") and v.has_column("payers", "reporting_entity_type"):
        quoted = ", ".join(f"'{x}'" for x in sorted(ALLOWED_REPORTING_ENTITY_TYPE))
        v.check(
            "D. Domain", "reporting_entity_type_out_of_range", "warn",
            f"payers.reporting_entity_type (case-insensitive) is one of {sorted(ALLOWED_REPORTING_ENTITY_TYPE)}",
            count_sql=f"SELECT count(*) FROM payers WHERE reporting_entity_type IS NOT NULL "
                      f"AND LOWER(reporting_entity_type) NOT IN ({quoted})",
            sample_sql=f"SELECT * FROM payers WHERE reporting_entity_type IS NOT NULL "
                       f"AND LOWER(reporting_entity_type) NOT IN ({quoted})",
            total_sql="SELECT count(*) FROM payers WHERE reporting_entity_type IS NOT NULL",
        )

    if v.table_exists("payers") and v.has_column("payers", "plan_market_type"):
        quoted = ", ".join(f"'{x}'" for x in sorted(ALLOWED_PLAN_MARKET_TYPE))
        v.check(
            "D. Domain", "plan_market_type_out_of_range", "warn",
            f"payers.plan_market_type (case-insensitive) is one of {sorted(ALLOWED_PLAN_MARKET_TYPE)}",
            count_sql=f"SELECT count(*) FROM payers WHERE plan_market_type IS NOT NULL "
                      f"AND LOWER(plan_market_type) NOT IN ({quoted})",
            sample_sql=f"SELECT * FROM payers WHERE plan_market_type IS NOT NULL "
                       f"AND LOWER(plan_market_type) NOT IN ({quoted})",
            total_sql="SELECT count(*) FROM payers WHERE plan_market_type IS NOT NULL",
        )

    if v.table_exists("providers") and v.has_column("providers", "tin_type"):
        quoted = ", ".join(f"'{x}'" for x in sorted(ALLOWED_TIN_TYPE))
        v.check(
            "D. Domain", "tin_type_out_of_range", "warn",
            f"providers.tin_type (case-insensitive) is one of {sorted(ALLOWED_TIN_TYPE)}",
            count_sql=f"SELECT count(*) FROM providers WHERE tin_type IS NOT NULL "
                      f"AND LOWER(tin_type) NOT IN ({quoted})",
            sample_sql=f"SELECT * FROM providers WHERE tin_type IS NOT NULL "
                       f"AND LOWER(tin_type) NOT IN ({quoted})",
            total_sql="SELECT count(*) FROM providers WHERE tin_type IS NOT NULL",
        )


# ---------------------------------------------------------------------------
# Section E: Format checks
# ---------------------------------------------------------------------------

def section_format(v: Validator):
    if v.table_exists("billing_codes"):
        v.check(
            "E. Format", "code_has_padding_whitespace", "warn",
            "billing_code has no leading/trailing whitespace (padding causes silent join/lookup failures downstream)",
            count_sql="SELECT count(*) FROM billing_codes WHERE billing_code != trim(billing_code)",
            sample_sql="SELECT * FROM billing_codes WHERE billing_code != trim(billing_code)",
            total_sql="SELECT count(*) FROM billing_codes",
            note="Fix upstream in stream_parser.py by trimming billing_code on insert.",
        )
        for code_type, (pattern, desc) in CODE_TYPE_PATTERNS.items():
            v.check(
                "E. Format", f"code_format:{code_type}", "warn",
                f"billing_code (trimmed) matches expected {code_type} format ({desc})",
                count_sql=f"""
                    SELECT count(*) FROM billing_codes
                    WHERE billing_code_type = '{code_type}'
                      AND NOT regexp_matches(trim(billing_code), '{pattern}')
                """,
                sample_sql=f"""
                    SELECT * FROM billing_codes
                    WHERE billing_code_type = '{code_type}'
                      AND NOT regexp_matches(trim(billing_code), '{pattern}')
                """,
                total_sql=f"SELECT count(*) FROM billing_codes WHERE billing_code_type = '{code_type}'",
            )

    if v.table_exists("negotiated_rates") and v.has_column("negotiated_rates", "expiration_date"):
        v.check(
            "E. Format", "expiration_date_unparseable", "warn",
            "expiration_date is NULL, the CMS sentinel '9999-12-31', or a parseable ISO date",
            count_sql="""
                SELECT count(*) FROM negotiated_rates
                WHERE expiration_date IS NOT NULL
                  AND expiration_date != '9999-12-31'
                  AND TRY_CAST(expiration_date AS DATE) IS NULL
            """,
            sample_sql="""
                SELECT DISTINCT expiration_date FROM negotiated_rates
                WHERE expiration_date IS NOT NULL
                  AND expiration_date != '9999-12-31'
                  AND TRY_CAST(expiration_date AS DATE) IS NULL
            """,
            total_sql="SELECT count(*) FROM negotiated_rates WHERE expiration_date IS NOT NULL",
        )

    if v.table_exists("providers") and v.has_column("providers", "tin_value"):
        # tin_type = 'npi' rows intentionally hold an NPI in tin_value, not an EIN/SSN —
        # excluded here so they aren't double-flagged (see tin_type_out_of_range).
        v.check(
            "E. Format", "tin_value_format", "warn",
            "providers.tin_value looks like a 9-digit EIN/SSN (with or without dashes), for rows where tin_type isn't 'npi'",
            count_sql=f"""
                SELECT count(*) FROM providers
                WHERE tin_value IS NOT NULL AND trim(tin_value) != ''
                  AND tin_type IS DISTINCT FROM 'npi'
                  AND NOT regexp_matches(tin_value, '{TIN_PATTERN}')
            """,
            sample_sql=f"""
                SELECT * FROM providers
                WHERE tin_value IS NOT NULL AND trim(tin_value) != ''
                  AND tin_type IS DISTINCT FROM 'npi'
                  AND NOT regexp_matches(tin_value, '{TIN_PATTERN}')
            """,
            total_sql="""
                SELECT count(*) FROM providers
                WHERE tin_value IS NOT NULL AND trim(tin_value) != '' AND tin_type IS DISTINCT FROM 'npi'
            """,
            note="A short value like '172' or '60646813' often means a leading zero got "
                 "dropped somewhere upstream (e.g. an EIN parsed as an integer) rather than a truly invalid TIN.",
        )

    if v.table_exists("providers") and v.has_column("providers", "npi"):
        # NPI validation, in three escalating checks (CMS NPI Final Rule):
        #   1. exactly 10 digits
        #   2. leading digit is 1 (individual) or 2 (organizational)
        #   3. passes the ISO 7064 Mod-10 / Luhn check-digit algorithm with the
        #      implied "80840" prefix
        # Validated in Python over the bounded set of distinct NPIs.
        distinct_npis = [r[0] for r in v.con.execute(
            "SELECT DISTINCT npi FROM providers WHERE npi IS NOT NULL"
        ).fetchall()]
        bad = []
        for n in distinct_npis:
            issue = _npi_issue(n)
            if issue:
                bad.append({"npi": n, "issue": issue})
        total = len(distinct_npis)
        res = CheckResult(
            "E. Format", "npi_invalid", "warn",
            "Distinct providers.npi values are 10 digits, start with 1 or 2, and pass the CMS check-digit algorithm",
            passed=not bad, n_bad=len(bad), n_total=total,
            sample=bad[: v.sample_size],
        )
        v.results.append(res)


def _npi_issue(npi) -> Optional[str]:
    s = str(npi)
    if len(s) != 10 or not s.isdigit():
        return "not exactly 10 digits"
    if s[0] not in ("1", "2"):
        return "leading digit is not 1 (individual) or 2 (organizational)"
    if not _npi_checksum_valid(npi):
        return "fails CMS check-digit (Luhn) algorithm"
    return None


def _npi_checksum_valid(npi) -> bool:
    s = str(npi)
    if len(s) != 10 or not s.isdigit():
        return False
    payload = "80840" + s[:9]
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    check_digit = (10 - (total % 10)) % 10
    return check_digit == int(s[9])


# ---------------------------------------------------------------------------
# Section F: Duplicates
# ---------------------------------------------------------------------------

def section_duplicates(v: Validator):
    if v.table_exists("billing_codes"):
        v.check(
            "F. Duplicates", "duplicate_billing_codes", "warn",
            "No (billing_code, billing_code_type, negotiation_arrangement) triple appears in more than one billing_codes row",
            count_sql="""
                SELECT count(*) - count(DISTINCT (billing_code, billing_code_type, negotiation_arrangement))
                FROM billing_codes
            """,
            sample_sql="""
                SELECT billing_code, billing_code_type, negotiation_arrangement, count(*) AS n
                FROM billing_codes
                GROUP BY 1, 2, 3 HAVING count(*) > 1 ORDER BY n DESC
            """,
            total_sql="SELECT count(*) FROM billing_codes",
        )

    if v.table_exists("providers"):
        v.check(
            "F. Duplicates", "duplicate_provider_npi_rows", "warn",
            "No (provider_reference_id, npi) pair appears in more than one providers row",
            count_sql="""
                SELECT count(*) - count(DISTINCT (provider_reference_id, npi))
                FROM providers
            """,
            sample_sql="""
                SELECT provider_reference_id, npi, count(*) AS n
                FROM providers
                GROUP BY 1, 2 HAVING count(*) > 1 ORDER BY n DESC
            """,
            total_sql="SELECT count(*) FROM providers",
        )

    if v.fast:
        v.skip("F. Duplicates", "duplicate_negotiated_rate_rows", "warn",
               "No fully-duplicate row exists in negotiated_rates",
               note="Skipped (--fast): requires grouping by every column across the full fact table.")
    elif v.table_exists("negotiated_rates"):
        # NOTE: the sample must group by the SAME columns as the count below — grouping
        # by a narrower column set in the sample (e.g. dropping provider_reference_ids)
        # inflates the displayed group size relative to the true full-row duplicate count.
        dup_cols = ("payer_id, code_id, negotiation_arrangement, billing_class, setting, "
                    "negotiated_type, negotiated_rate, service_code, billing_code_modifier, "
                    "expiration_date, provider_reference_ids, source_file")
        v.check(
            "F. Duplicates", "duplicate_negotiated_rate_rows", "warn",
            "No fully-duplicate row (identical on every column but ingested_at) exists in negotiated_rates",
            count_sql=f"""
                WITH d AS (
                    SELECT {dup_cols}, count(*) AS n
                    FROM negotiated_rates
                    GROUP BY ALL HAVING count(*) > 1
                )
                SELECT COALESCE(sum(n - 1), 0) FROM d
            """,
            sample_sql=f"""
                SELECT payer_id, code_id, negotiated_type, negotiated_rate, source_file, count(*) AS n
                FROM negotiated_rates
                GROUP BY {dup_cols}
                HAVING count(*) > 1 ORDER BY n DESC
            """,
            total_sql="SELECT count(*) FROM negotiated_rates",
        )


# ---------------------------------------------------------------------------
# Section G: Coverage summary (informational)
# ---------------------------------------------------------------------------

def section_coverage(v: Validator):
    if not v.table_exists("negotiated_rates"):
        return
    v.info_table(
        "G. Coverage", "rows_by_source_file",
        "Row counts and rate range per ingested source file",
        """
        SELECT source_file,
               count(*) AS n_rows,
               count(DISTINCT payer_id) AS n_payers,
               count(DISTINCT code_id) AS n_codes,
               ROUND(MIN(negotiated_rate), 2) AS min_rate,
               ROUND(MAX(negotiated_rate), 2) AS max_rate,
               MAX(ingested_at) AS last_ingested_at
        FROM negotiated_rates
        GROUP BY source_file
        ORDER BY n_rows DESC
        """,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

ICON = {"error": "✗", "warn": "!", "info": "·"}


def render_report(results: list[CheckResult], sample_size: int, db_path: str) -> str:
    out = []

    def p(line=""):
        out.append(line)

    by_section: dict[str, list[CheckResult]] = {}
    for r in results:
        by_section.setdefault(r.section, []).append(r)

    n_error = sum(1 for r in results if r.level == "error" and not r.passed)
    n_warn = sum(1 for r in results if r.level == "warn" and not r.passed)
    n_ok = sum(1 for r in results if r.passed and r.level != "info")

    p("=" * 90)
    p(f"DATA VALIDATION REPORT — {db_path}")
    p("=" * 90)

    for section in sorted(by_section):
        p(f"\n{section}")
        p("-" * len(section))
        for r in by_section[section]:
            if r.level == "info":
                p(f"  · {r.name}: {r.description}")
                for row in r.sample[:sample_size]:
                    p(f"      {row}")
                continue
            status = "PASS" if r.passed else r.level.upper()
            icon = "✓" if r.passed else ICON[r.level]
            pct = f" ({r.pct_bad:.2f}% of {r.n_total:,})" if r.n_total else ""
            line = f"  {icon} [{status:5s}] {r.name}: {r.description}"
            line += f"\n         → {r.n_bad:,} bad{pct}"
            p(line)
            if r.note:
                p(f"         note: {r.note}")
            if not r.passed and r.sample:
                for row in r.sample[:sample_size]:
                    p(f"         e.g. {row}")

    # ---- Percent-bad summary table, worst first --------------------------
    quant = [r for r in results if r.level != "info" and r.n_total]
    quant.sort(key=lambda r: r.pct_bad, reverse=True)

    p("\n" + "=" * 90)
    p("SUMMARY TABLE — every quantifiable check, worst first")
    p("=" * 90)
    name_w = max((len(r.name) for r in quant), default=4)
    header = f"{'CHECK':<{name_w}}  {'LEVEL':<5}  {'STATUS':<6}  {'TOTAL':>10}  {'BAD':>10}  {'% BAD':>8}"
    p(header)
    p("-" * len(header))
    for r in quant:
        status = "PASS" if r.passed else r.level.upper()
        p(f"{r.name:<{name_w}}  {r.level:<5}  {status:<6}  {r.n_total:>10,}  {r.n_bad:>10,}  {r.pct_bad:>7.2f}%")

    p("\n" + "=" * 90)
    p(f"OVERALL: {n_ok} passed, {n_warn} warning(s), {n_error} error(s)")
    p("=" * 90)

    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="transparency.duckdb")
    ap.add_argument("--txt", default="validation_report.txt",
                     help="Path to write the plain-text report to (default: validation_report.txt; pass '' to skip)")
    ap.add_argument("--json", default=None, help="Also write the full report as JSON to this path")
    ap.add_argument("--sample", type=int, default=5, help="Max example rows to print/store per failing check")
    ap.add_argument("--fast", action="store_true", help="Skip checks that explode arrays or group over the full fact table")
    ap.add_argument("--fail-on", choices=["error", "warn"], default="error",
                     help="Minimum severity that causes a non-zero exit code (default: error)")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    v = Validator(con, sample_size=args.sample, fast=args.fast)

    section_structure(v)
    section_referential(v)
    section_completeness(v)
    section_domain(v)
    section_format(v)
    section_duplicates(v)
    section_coverage(v)

    report_text = render_report(v.results, args.sample, args.db)
    print(report_text)

    if args.txt:
        with open(args.txt, "w", encoding="utf-8") as f:
            f.write(report_text + "\n")
        print(f"\nText report written to {args.txt}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in v.results], f, indent=2, default=str)
        print(f"Full JSON report written to {args.json}")

    threshold = LEVEL_RANK[args.fail_on]
    failed = [r for r in v.results if not r.passed and LEVEL_RANK[r.level] >= threshold]
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()