# Project TODO

## Data quality

- Add representative CMS reference files under `reference_data/` when
  benchmarking code-description coverage:
  - HCPCS annual release workbook
  - MS-DRG definitions
  - Physician fee schedule RVU file
  - ICD-10-PCS code tables
- Run `load_code_descriptions.py` with the available reference files and record
  coverage changes in the validation report.
- Run `build_benchmarks.py --drop-first` after refreshing reference data.
- Re-export dashboard files with `export_for_dashboard.py`.

## Engineering

- Keep large databases, downloaded archives, reports, and dashboard exports out
  of version control.
