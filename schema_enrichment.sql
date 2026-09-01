-- =====================================================================
-- ENRICHMENT DATABASE SCHEMA
-- Reference tables that augment transparency.duckdb with geo and
-- provider identity. Lives in a separate enrichment.duckdb so it can
-- be refreshed independently on its own schedule (NPPES monthly,
-- crosswalks rarely, code lists per CMS release).
--
-- To attach at query time:
--   ATTACH 'enrichment.duckdb' AS ref (READ_ONLY);
-- =====================================================================

-- ---------------------------------------------------------------------
-- NPPES Provider Registry
-- Source: CMS NPPES full replacement file (monthly)
-- One row per active NPI. Individual providers: name from first/last.
-- Organizations: name from legal business name.
-- zip5 is normalized to 5 digits for joining with zip_county.
-- taxonomy_code is the primary taxonomy (Healthcare Provider Taxonomy
-- Code_1 where Switch_1 = 'Y'). Most providers only have one taxonomy
-- so slot 1 is correct for ~95% of records.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nppes (
    npi               BIGINT PRIMARY KEY,
    entity_type       VARCHAR,   -- '1' = individual, '2' = organization
    provider_name     VARCHAR,   -- unified display name
    first_name        VARCHAR,   -- individuals only
    last_name         VARCHAR,   -- individuals only
    org_name          VARCHAR,   -- organizations only
    city              VARCHAR,
    state             VARCHAR,
    zip5              VARCHAR,   -- normalized 5-digit ZIP
    taxonomy_code     VARCHAR,   -- primary NUCC taxonomy code
    taxonomy_is_primary VARCHAR  -- 'Y' confirms slot 1 is primary
);

CREATE INDEX IF NOT EXISTS idx_nppes_npi   ON nppes(npi);
CREATE INDEX IF NOT EXISTS idx_nppes_state ON nppes(state);
CREATE INDEX IF NOT EXISTS idx_nppes_zip   ON nppes(zip5);

-- ---------------------------------------------------------------------
-- ZIP → County crosswalk
-- Source: HUD USPS ZIP-County crosswalk (quarterly)
-- One row per ZIP-county pair (ZIPs can span counties; we keep all).
-- tot_ratio is the fraction of addresses in that ZIP assigned to the
-- county — use it to pick the dominant county if you want one-to-one.
-- fips is the 5-digit FIPS code (2-digit state + 3-digit county).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS zip_county (
    zip5        VARCHAR,
    fips        VARCHAR,   -- 5-digit county FIPS
    county_name VARCHAR,
    state_abbr  VARCHAR,
    tot_ratio   DOUBLE     -- fraction of ZIP addresses in this county
);

CREATE INDEX IF NOT EXISTS idx_zip_county_zip  ON zip_county(zip5);
CREATE INDEX IF NOT EXISTS idx_zip_county_fips ON zip_county(fips);

-- ---------------------------------------------------------------------
-- Authoritative code descriptions
-- Source: CMS HCPCS release, AMA CPT (where available), CMS DRG weights
-- Supplements the free-text description field in MRF billing_codes,
-- which is inconsistent across payers.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS code_descriptions (
    billing_code      VARCHAR,
    billing_code_type VARCHAR,   -- CPT, HCPCS, MS-DRG, APR-DRG, RC, etc.
    short_description VARCHAR,
    long_description  VARCHAR,
    effective_date    VARCHAR,
    PRIMARY KEY (billing_code, billing_code_type)
);

-- ---------------------------------------------------------------------
-- NUCC Taxonomy → human-readable specialty
-- Source: NUCC Health Care Provider Taxonomy code set (bi-annual)
-- Joins to nppes.taxonomy_code to give readable specialty names.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS taxonomy_descriptions (
    taxonomy_code VARCHAR PRIMARY KEY,
    grouping      VARCHAR,   -- e.g. 'Allopathic & Osteopathic Physicians'
    classification VARCHAR,  -- e.g. 'Internal Medicine'
    specialization VARCHAR,  -- e.g. 'Cardiovascular Disease'
    definition    VARCHAR
);