-- =====================================================================
-- STEP 2: POST-INGESTION OPTIMIZATION SCHEMA
-- Run ONCE after all rate files are finished processing.
-- =====================================================================

-- 1. Build secondary lookup indexes on the fully loaded database
CREATE INDEX IF NOT EXISTS idx_rates_code  ON negotiated_rates(code_id);
CREATE INDEX IF NOT EXISTS idx_rates_payer ON negotiated_rates(payer_id);

-- 2. Force ZSTD compression and flush the WAL file
PRAGMA force_compression='zstd';
CHECKPOINT;
-- python -c "import duckdb; con = duckdb.connect('transparency.duckdb'); con.execute('DROP INDEX IF EXISTS idx_rates_code; DROP INDEX IF EXISTS idx_rates_payer;'); con.close(); print('Indexes dropped! Ready to continue ingestion.')"