-- =====================================================================
-- Hospital Price Transparency Pipeline — DuckDB Schema
-- Optimized for: fast aggregation by procedure/payer/hospital,
-- bounded-memory streaming inserts, local-machine analytics.
-- =====================================================================

CREATE SEQUENCE IF NOT EXISTS seq_payer_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_code_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_rate_id START 1;

-- ---------------------------------------------------------------------
-- Dimension: payer / plan (one row per plan per source file)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payers (
    payer_id               BIGINT PRIMARY KEY DEFAULT nextval('seq_payer_id'),
    reporting_entity_name  VARCHAR,
    reporting_entity_type  VARCHAR,
    plan_name              VARCHAR,
    plan_id                VARCHAR,
    plan_id_type           VARCHAR,
    plan_market_type       VARCHAR,
    last_updated_on        VARCHAR,
    source_file             VARCHAR
);

-- ---------------------------------------------------------------------
-- Dimension: billing code / procedure (deduplicated across all files)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing_codes (
    code_id                    BIGINT PRIMARY KEY DEFAULT nextval('seq_code_id'),
    billing_code               VARCHAR NOT NULL,   -- e.g. "99213", "APR-DRG 194"
    billing_code_type          VARCHAR NOT NULL,   -- CPT | HCPCS | MS-DRG | ICD | ...
    billing_code_type_version  VARCHAR,
    description                VARCHAR,
    UNIQUE (billing_code, billing_code_type)
);

-- ---------------------------------------------------------------------
-- Fact table: one row per negotiated price entry.
-- Only "institutional" (facility/hospital, i.e. non-professional)
-- billing_class rows are ever loaded here — filtering happens upstream
-- in the parser so this table never carries physician/professional rows.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS negotiated_rates (
    rate_id                 BIGINT PRIMARY KEY DEFAULT nextval('seq_rate_id'),
    payer_id                BIGINT REFERENCES payers(payer_id),
    code_id                 BIGINT REFERENCES billing_codes(code_id),
    billing_class           VARCHAR,        -- always 'institutional' by construction
    negotiated_type         VARCHAR,        -- negotiated | percentage | derived | fee schedule
    negotiated_rate         DOUBLE,         -- dollar amount, OR percentage when negotiated_type='percentage'
    service_code            VARCHAR[],      -- place-of-service codes
    billing_code_modifier   VARCHAR[],
    expiration_date         VARCHAR,
    provider_reference_ids  BIGINT[],       -- raw provider_group/NPI ids — resolved in phase 2
    source_file              VARCHAR,
    ingested_at               TIMESTAMP DEFAULT current_timestamp
);

CREATE INDEX IF NOT EXISTS idx_rates_code  ON negotiated_rates(code_id);
CREATE INDEX IF NOT EXISTS idx_rates_payer ON negotiated_rates(payer_id);

-- ---------------------------------------------------------------------
-- Phase-2 (optional): resolved provider/facility identity.
-- The raw MRF only carries NPI/TIN — mapping that to a hospital *name*
-- requires either the payer's external provider-reference file or a
-- join against NPPES. Populate this table separately; negotiated_rates
-- works fine without it (aggregate by payer/procedure immediately).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS providers (
    provider_reference_id  BIGINT PRIMARY KEY,
    npi                    BIGINT,
    tin_type               VARCHAR,
    tin_value               VARCHAR,
    facility_name            VARCHAR
);

-- =====================================================================
-- Benchmarking / statistics views (requirement #4)
-- DuckDB's native MEDIAN() aggregate makes this a one-liner.
-- =====================================================================

CREATE OR REPLACE VIEW procedure_price_stats AS
SELECT
    bc.billing_code,
    bc.billing_code_type,
    bc.description,
    COUNT(*)                         AS n_rate_entries,
    COUNT(DISTINCT r.payer_id)       AS n_payers,
    MIN(r.negotiated_rate)           AS min_rate,
    MAX(r.negotiated_rate)           AS max_rate,
    ROUND(AVG(r.negotiated_rate), 2) AS avg_rate,
    MEDIAN(r.negotiated_rate)        AS median_rate
FROM negotiated_rates r
JOIN billing_codes bc USING (code_id)
WHERE r.negotiated_type = 'negotiated'   -- fixed-dollar only; % / derived rates need separate handling
GROUP BY 1, 2, 3;

CREATE OR REPLACE VIEW procedure_price_by_payer AS
SELECT
    bc.billing_code,
    p.reporting_entity_name          AS payer,
    COUNT(*)                         AS n_rate_entries,
    ROUND(AVG(r.negotiated_rate), 2) AS avg_rate,
    MEDIAN(r.negotiated_rate)        AS median_rate
FROM negotiated_rates r
JOIN billing_codes bc USING (code_id)
JOIN payers p USING (payer_id)
WHERE r.negotiated_type = 'negotiated'
GROUP BY 1, 2;
