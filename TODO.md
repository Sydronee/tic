Here's your to-do list:

**1. Fix ICD-10-PCS (0 codes)**
Run `probe_icd_xml.py` to see the exact XML structure, then paste the output back here so I can fix the parser. Or skip ICD for now — you only have 343 ICD codes in your DB, it's not blocking anything.

**2. Fix HCPCS (9.3% → ~80%)**
Download the free CMS HCPCS Level II Annual Release file:
- Go to cms.gov → Medicare → Coding → HCPCS
- Download `HCPC2026_ANWEB.xlsx`
- Put it in `reference_data/`
- `pip install openpyxl` then re-run `load_code_descriptions.py --hcpcs reference_data/HCPC2026_ANWEB.xlsx`

**3. Fix RC trailing spaces (34.2%)**
The MRF data has codes like `'771      '`. The fix for this is already baked into `build_benchmarks.py` via `LPAD(TRIM(...), 4, '0')` on the join — no action needed.

**4. Build the benchmarks**
Once code descriptions look good, from `TiC/`:
```bash
python build_benchmarks.py --drop-first
```
Takes 20–60s. Adds 5 new tables to `transparency.duckdb`.

**5. Export for the dashboard**
Re-run `export_for_dashboard.py` after the benchmarks build so the Parquet files pick up the new tables.

That's the full sequence. Start with step 2 since it's the biggest coverage win and requires no code changes.


Here are the two scripts ready to run:Run them in this order from `TiC/`:

1. `python probe_icd_xml.py reference_data/icd10pcs_tables_2027.xml`
2. `pip install openpyxl` then `python load_code_descriptions.py --msdrg reference_data/appendix_A.txt --rvu reference_data/PPRRVU2026_Jan_QPP.csv --icd reference_data/icd10pcs_tables_2027.xml --hcpcs reference_data/HCPC2026_ANWEB.xlsx`
3. `python build_benchmarks.py --drop-first`


Files.zip

1. CPT/HCPCS — CMS Physician Fee Schedule RVU file:

https://www.cms.gov/Medicare/Medicare-Fee-for-Service-Payment/PhysicianFeeSched/PFS-Relative-Value-Files

Download the current year's "ZIP file for January" → extract RVCPWRVU_*.txt

2. MS-DRG definitions:

https://www.cms.gov/Medicare/Medicare-Fee-for-Service-Payment/AcuteInpatientPPS/MS-DRG-Classifications-and-Software

Download "FY2025 Final Rule MS-DRG Definitions Manual" → extract the GROUPR*.txt or MDRGV*.txt file

3. ICD-10-PCS (procedure codes):

https://www.cms.gov/medicare/coding-billing/icd-10-codes

Download "ICD-10-PCS" → "Code Tables and Index" ZIP → extract icd10pcs_codes_YYYY.txt

Once you have those three files, tell me their actual filenames and I'll write load_code_descriptions.py that handles all five types (including the embedded RC list and the CSTM-ALL stub) in one pass. The RC codes I'll embed directly — all 713 standard UB-04 revenue codes fit in ~200 lines.