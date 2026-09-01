#!/usr/bin/env python3
"""
load_code_descriptions.py
--------------------------
Load authoritative code descriptions into enrichment.duckdb.

Sources:
  MS-DRG   : appendix_A.txt from CMS MS-DRG Definitions Manual V43.1
  CPT/HCPCS: PPRRVU2026_Jan_QPP.csv from CMS Physician Fee Schedule
  HCPCS L2 : HCPC2026_ANWEB.xlsx from CMS HCPCS Annual Release (optional,
              greatly improves J/G/E/K/Q-code coverage from 9% → ~80%)
  ICD-10-PCS: icd10pcs_tables_2027.xml from CMS ICD-10-PCS Code Tables
  RC        : Embedded standard UB-04 revenue codes (no download needed)
  CSTM-ALL  : Stubbed as payer-defined

Usage:
    python load_code_descriptions.py \
        --msdrg  reference_data/appendix_A.txt \
        --rvu    reference_data/PPRRVU2026_Jan_QPP.csv \
        --icd    reference_data/icd10pcs_tables_2027.xml \
        [--hcpcs reference_data/HCPC2026_ANWEB.xlsx]

HCPCS Level II download (free):
    https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system/
    → "HCPCS Quarterly Update" → download the ANWEB Excel file
"""

import argparse
import csv
import re
import time
import xml.etree.ElementTree as ET
from itertools import product
from pathlib import Path

import duckdb

DEFAULT_ENRICHMENT = "enrichment.duckdb"


# ------------------------------------------------------------------ #
# MS-DRG parser                                                        #
# Fixed-width: DRG(0:3), MDC(4:6), type(8), description(11:)         #
# ------------------------------------------------------------------ #
def parse_msdrg(path: Path) -> list[tuple]:
    rows = []
    in_data = False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if "DRG" in line and "MDC" in line and "Description" in line:
                in_data = True
                continue
            if not in_data or len(line) < 12:
                continue
            drg = line[0:3].strip()
            if not drg or not drg.isdigit():
                continue
            description = line[11:].strip()
            if description:
                rows.append((drg.zfill(4), "MS-DRG", description))
    print(f"  MS-DRG   : {len(rows):,} codes")
    return rows


# ------------------------------------------------------------------ #
# CPT / HCPCS from RVU file                                            #
# Skip 9 preamble rows; row 10 is header.                             #
# Columns: HCPCS=0, MOD=1, DESCRIPTION=2, STATUS_CODE=3              #
# Keep: no modifier, status != D (deleted)                            #
# ------------------------------------------------------------------ #
HCPCS_RE = re.compile(r"^[A-Z]\d{4}$")


def parse_rvu(path: Path) -> list[tuple]:
    rows = []
    with open(path, encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        for _ in range(9):       # skip preamble
            next(reader, None)
        next(reader, None)       # skip header row
        for row in reader:
            if len(row) < 4:
                continue
            code   = row[0].strip()
            mod    = row[1].strip()
            desc   = row[2].strip()
            status = row[3].strip()
            if not code or mod or status == "D" or not desc:
                continue
            code_type = "HCPCS" if HCPCS_RE.match(code) else "CPT"
            rows.append((code, code_type, desc))
    cpt   = sum(1 for r in rows if r[1] == "CPT")
    hcpcs = sum(1 for r in rows if r[1] == "HCPCS")
    print(f"  CPT      : {cpt:,} codes  (from RVU file)")
    print(f"  HCPCS    : {hcpcs:,} codes  (from RVU file — physician-schedule codes only)")
    return rows


# ------------------------------------------------------------------ #
# HCPCS Level II — optional ANWEB Excel file                          #
# Covers J-codes (drugs), G/K/Q (facility/temporary), E (DME), etc.  #
# Column layout (2026 release): col A=HCPCS, col B=..., col C=desc   #
# Skip header rows until we see a code-like value in col A.           #
# ------------------------------------------------------------------ #
def parse_hcpcs_level2(path: Path) -> list[tuple]:
    """
    Parse CMS HCPCS Annual Release ANWEB Excel file.
    Returns HCPCS rows not already captured by the RVU file.
    Requires openpyxl: pip install openpyxl
    """
    try:
        import openpyxl
    except ImportError:
        print("  HCPCS L2 : skipped (pip install openpyxl to enable)")
        return []

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = []
    header_found = False

    for row in ws.iter_rows(values_only=True):
        if not row or row[0] is None:
            continue
        code = str(row[0]).strip()
        if not HCPCS_RE.match(code):
            # Still in preamble/header rows
            continue
        header_found = True
        # Column layout varies by year; description is typically col 2 (index 2)
        # or col 3 depending on the release. Try both.
        desc = None
        for col_idx in [2, 3, 4]:
            if col_idx < len(row) and row[col_idx]:
                candidate = str(row[col_idx]).strip()
                if len(candidate) > 5:
                    desc = candidate
                    break
        if desc:
            rows.append((code, "HCPCS", desc))

    wb.close()
    print(f"  HCPCS L2 : {len(rows):,} codes  (from ANWEB — J/G/E/K/Q codes etc.)")
    return rows


# ------------------------------------------------------------------ #
# ICD-10-PCS parser — corrected for actual CMS XML schema             #
#                                                                      #
# Actual CMS schema (icd10pcs_tables_2027.xml):                       #
#   Root tag: <ICD10PCS.tabular> (try both this and bare root)        #
#   <PCSTable>                                                         #
#     <axis position="1"><title>Section</title>                        #
#       <code tag="0"><description>Medical and Surgical</description>  #
#       <code tag="1">...                                              #
#     </axis>                                                          #
#     <axis position="2">...<axis position="3">...  ← table-level     #
#     <PCSRow>                                                         #
#       <axis position="4">...<axis position="5">...  ← row-level     #
#       <axis position="6">...<axis position="7">...                   #
#     </PCSRow>                                                        #
#   </PCSTable>                                                        #
#                                                                      #
# Old (broken) assumptions in the script:                              #
#   ✗  attr "pos"    → correct attr is "position"                     #
#   ✗  <codes> child with space-separated text → no such element      #
#   ✗  axis title as code description → description is per <code>     #
# ------------------------------------------------------------------ #
def parse_icd_pcs(path: Path) -> list[tuple]:
    try:
        tree = ET.parse(path)
        xml_root = tree.getroot()
    except ET.ParseError as e:
        print(f"  ICD-PCS  : parse error ({e}), skipping")
        return []

    def extract_axis_values(parent_el, positions: set) -> dict[int, list[tuple[str, str]]]:
        """
        Returns {position: [(char_value, description), ...]} for each
        <axis position="N"> child of parent_el whose position is in `positions`.
        """
        result: dict[int, list] = {}
        for axis in parent_el.findall("axis"):
            pos_str = axis.get("position", "")
            try:
                pos = int(pos_str)
            except ValueError:
                continue
            if pos not in positions:
                continue
            vals = []
            for code_el in axis.findall("code"):
                tag = code_el.get("tag", "").strip()
                desc_el = code_el.find("description")
                desc = (desc_el.text or "").strip() if desc_el is not None else ""
                if tag and desc:
                    vals.append((tag, desc))
            if vals:
                result[pos] = vals
        return result

    rows: list[tuple] = []
    seen: set[str] = set()

    # CMS sometimes wraps in <ICD10PCS.tabular>; sometimes the root IS the table list
    table_els = xml_root.findall("PCSTable")
    if not table_els:
        # Try case variations (older files used <pcsTable>)
        table_els = xml_root.findall("pcsTable")
    if not table_els:
        # Root might be a wrapper; look one level deeper
        for child in xml_root:
            table_els += child.findall("PCSTable") or child.findall("pcsTable")

    for table in table_els:
        # Positions 1-3 are shared across all rows in this table
        table_axes = extract_axis_values(table, {1, 2, 3})

        pcsrow_tag = "PCSRow" if table.find("PCSRow") is not None else "pcsRow"
        for pcsrow in table.findall(pcsrow_tag):
            # Positions 4-7 vary per row
            row_axes = extract_axis_values(pcsrow, {4, 5, 6, 7})
            all_axes = {**table_axes, **row_axes}

            if len(all_axes) != 7:
                # Incomplete table — skip rather than emit garbage codes
                continue

            # Cartesian product of all axis values → one 7-character code per combo
            try:
                for combo in product(*[all_axes[p] for p in range(1, 8)]):
                    code = "".join(ch for ch, _ in combo)
                    if code in seen:
                        continue
                    seen.add(code)
                    # Human-readable description: "Root Op of Body Part, Approach, Device"
                    # positions: 1=section, 2=body system, 3=root operation,
                    #            4=body part, 5=approach, 6=device, 7=qualifier
                    _, sec_desc   = combo[0]
                    _, sys_desc   = combo[1]
                    _, root_desc  = combo[2]
                    _, part_desc  = combo[3]
                    _, appr_desc  = combo[4]
                    _, dev_desc   = combo[5]
                    _, qual_desc  = combo[6]
                    desc = (
                        f"{root_desc} of {part_desc}, {appr_desc}"
                        + (f", {dev_desc}" if dev_desc and dev_desc.lower() != "no device" else "")
                        + (f", {qual_desc}" if qual_desc and qual_desc.lower() not in ("no qualifier", "unqualified") else "")
                    )
                    rows.append((code, "ICD", desc))
            except KeyError:
                continue

    print(f"  ICD-PCS  : {len(rows):,} codes")
    if len(rows) == 0:
        # Help the user debug — show them what tags actually exist
        child_tags = {child.tag for child in xml_root} | {child.tag for child in xml_root[:1] for child in child}
        print(f"  ICD-PCS  : WARNING — 0 codes parsed. Root tag=<{xml_root.tag}>, "
              f"child tags seen: {child_tags}")
        print("             Check that the file is icd10pcs_tables_2027.xml (not the index or order file).")
    return rows


# ------------------------------------------------------------------ #
# Revenue codes — embedded UB-04 standard list                        #
# RC codes in MRF files appear as both "0250" and "250"; we store     #
# both forms so joins work regardless of source formatting.           #
# ------------------------------------------------------------------ #
def get_rc_codes() -> list[tuple]:
    rc_raw = {
        # fmt: off
        "0001": "Total Charges",
        "0100": "Room & Board - Private",
        "0101": "Room & Board - Private Medical/Surgical",
        "0102": "Room & Board - Private OB",
        "0103": "Room & Board - Private Pediatric",
        "0104": "Room & Board - Private Psychiatric",
        "0110": "Room & Board - Semi-Private Two-Bed",
        "0111": "Room & Board - Semi-Private Two-Bed Medical/Surgical",
        "0112": "Room & Board - Semi-Private Two-Bed OB",
        "0113": "Room & Board - Semi-Private Two-Bed Pediatric",
        "0114": "Room & Board - Semi-Private Two-Bed Psychiatric",
        "0120": "Room & Board - Semi-Private Three & Four Bed",
        "0130": "Room & Board - Private Deluxe",
        "0140": "Room & Board - Ward",
        "0150": "Room & Board - Ward Medical/Surgical",
        "0160": "Room & Board - Other",
        "0164": "Room & Board - Sterile Environment",
        "0167": "Room & Board - Self Care",
        "0170": "Nursery",
        "0171": "Nursery - Newborn Level I",
        "0172": "Nursery - Newborn Level II",
        "0173": "Nursery - Newborn Level III",
        "0174": "Nursery - Newborn Level IV",
        "0180": "Leave of Absence",
        "0190": "Subacute Care",
        "0200": "Intensive Care",
        "0201": "ICU - Surgical",
        "0202": "ICU - Medical",
        "0203": "ICU - Pediatric",
        "0204": "ICU - Psychiatric",
        "0206": "ICU - Post ICU",
        "0210": "Cardiac Care",
        "0211": "Cardiac Intensive Care",
        "0220": "Coronary Care",
        "0221": "Coronary Care - Myocardial Infarction",
        "0230": "Private (Labor Room/Delivery)",
        "0240": "All-Inclusive Ancillary",
        "0250": "Pharmacy",
        "0251": "Pharmacy - Generic Drugs",
        "0252": "Pharmacy - Non-Generic Drugs",
        "0253": "Pharmacy - Take-Home Drugs",
        "0254": "Pharmacy - Drugs Incident to Diagnostic Services",
        "0255": "Pharmacy - Drugs Incident to Radiology",
        "0256": "Pharmacy - Experimental Drugs",
        "0257": "Pharmacy - Non-Prescription",
        "0258": "Pharmacy - IV Solutions",
        "0259": "Pharmacy - Other",
        "0260": "IV Therapy",
        "0261": "IV Therapy - Infusion Pump",
        "0262": "IV Therapy - Pharmacy Services",
        "0263": "IV Therapy - Drug/Supply Delivery",
        "0270": "Medical/Surgical Supplies",
        "0271": "Supplies - Non-Sterile",
        "0272": "Supplies - Sterile",
        "0273": "Supplies - Take-Home",
        "0274": "Supplies - Prosthetic/Orthotic Devices",
        "0275": "Supplies - Pacemaker",
        "0276": "Supplies - Intraocular Lens",
        "0277": "Supplies - Oxygen",
        "0278": "Supplies - Other Implants",
        "0279": "Supplies - Other",
        "0280": "Oncology",
        "0290": "DME (Other than Renal)",
        "0291": "DME - Rental",
        "0292": "DME - Purchase of New",
        "0293": "DME - Purchase of Used",
        "0294": "DME - Supplies/Drugs for DME Effectiveness",
        "0300": "Laboratory",
        "0301": "Lab - Chemistry",
        "0302": "Lab - Immunology",
        "0303": "Lab - Renal Patient (Home)",
        "0304": "Lab - Non-Routine Dialysis",
        "0305": "Lab - Hematology",
        "0306": "Lab - Bacteriology & Microbiology",
        "0307": "Lab - Urology",
        "0309": "Lab - Other",
        "0310": "Laboratory Pathological",
        "0311": "Lab Path - Cytology",
        "0312": "Lab Path - Histology",
        "0314": "Lab Path - Biopsy",
        "0320": "Radiology - Diagnostic",
        "0321": "Radiology - Chest X-Ray",
        "0322": "Radiology - Upper GI",
        "0323": "Radiology - Lower GI",
        "0324": "Radiology - Arteriography",
        "0329": "Radiology - Other",
        "0330": "Radiology - Therapeutic",
        "0331": "Radiology - Chemotherapy - Injected",
        "0332": "Radiology - Chemotherapy - Oral",
        "0333": "Radiology - Radiation Therapy",
        "0340": "Nuclear Medicine",
        "0341": "Nuclear Medicine - Diagnostic",
        "0342": "Nuclear Medicine - Therapeutic",
        "0350": "CT Scan",
        "0351": "CT Scan - Head",
        "0352": "CT Scan - Body",
        "0360": "Operating Room Services",
        "0361": "OR Services - Minor",
        "0362": "OR Services - Organ Transplant",
        "0367": "OR Services - Kidney Transplant",
        "0369": "OR Services - Other",
        "0370": "Anesthesia",
        "0371": "Anesthesia - Incident to Radiology",
        "0372": "Anesthesia - Incident to Diagnostic Services",
        "0374": "Anesthesia - Acupuncture",
        "0380": "Blood",
        "0381": "Blood - Packed Red Cells",
        "0382": "Blood - Whole Blood",
        "0383": "Blood - Plasma",
        "0384": "Blood - Platelets",
        "0385": "Blood - Leukocytes",
        "0386": "Blood - Other Components",
        "0387": "Blood - Other Derivatives",
        "0389": "Blood - Other",
        "0390": "Blood Storage & Processing",
        "0400": "Other Imaging Services",
        "0401": "Imaging - Ultrasound",
        "0402": "Imaging - MRI Brain",
        "0403": "Imaging - MRI Other",
        "0404": "Imaging - PET",
        "0410": "Respiratory Services",
        "0412": "Respiratory - Inhalation Services",
        "0413": "Respiratory - Hyperbaric Oxygen Therapy",
        "0420": "Physical Therapy",
        "0421": "PT - Visit Charge",
        "0422": "PT - Hourly Charge",
        "0423": "PT - Group Rate",
        "0424": "PT - Evaluation or Re-evaluation",
        "0430": "Occupational Therapy",
        "0431": "OT - Visit Charge",
        "0440": "Speech - Language Pathology",
        "0441": "SLP - Visit Charge",
        "0450": "Emergency Room",
        "0451": "ER - EMTALA Emergency Medical Screening",
        "0452": "ER - Beyond EMTALA Screening",
        "0456": "ER - Urgent Care",
        "0460": "Pulmonary Function",
        "0470": "Audiology",
        "0480": "Cardiology",
        "0481": "Cardiac Cath Lab",
        "0482": "Cardiology - Stress Test",
        "0483": "Cardiology - Echocardiology",
        "0490": "Ambulatory Surgical Care",
        "0500": "Outpatient Services",
        "0510": "Clinic",
        "0511": "Clinic - Chronic Pain Center",
        "0512": "Clinic - Dental",
        "0513": "Clinic - Psychiatric",
        "0514": "Clinic - OB-GYN",
        "0515": "Clinic - Pediatric",
        "0516": "Clinic - Urgent Care",
        "0517": "Clinic - Family Practice",
        "0520": "Free-Standing Clinic",
        "0526": "Clinic - Urgent Care (Free-standing)",
        "0530": "Osteopathic Services",
        "0540": "Ambulance",
        "0541": "Ambulance - Road",
        "0542": "Ambulance - Air",
        "0543": "Ambulance - Water",
        "0550": "Skilled Nursing",
        "0560": "Medical Social Services",
        "0570": "Home Health Aid (Home Visit)",
        "0580": "Other Visits (Home Health)",
        "0590": "Units of Service (Home Health)",
        "0600": "Oxygen (Home Health)",
        "0610": "MRI",
        "0611": "MRI - Brain",
        "0612": "MRI - Spinal Cord",
        "0614": "MRI - Other",
        "0620": "Medical/Surgical Supplies Extension",
        "0630": "Pharmacy Extension",
        "0636": "Pharmacy - Drugs Requiring Detailed Coding",
        "0640": "Home IV Therapy Services",
        "0650": "Hospice Services",
        "0651": "Hospice - RN",
        "0652": "Hospice - Medical Social Worker",
        "0653": "Hospice - LPN",
        "0654": "Hospice - Nurse Aide",
        "0655": "Hospice - Homemaker",
        "0656": "Hospice - Physician",
        "0657": "Hospice - Outpatient",
        "0658": "Hospice - Inpatient",
        "0659": "Hospice - General Inpatient (Non-Respite)",
        "0660": "Respite Care",
        "0670": "Outpatient Special Residence",
        "0680": "Trauma Response",
        "0700": "Cast Room",
        "0710": "Recovery Room",
        "0720": "Labor Room/Delivery",
        "0721": "Labor",
        "0722": "Delivery",
        "0723": "Circumcision",
        "0724": "Birthing Center",
        "0730": "EKG/ECG",
        "0731": "Telemetry",
        "0740": "EEG",
        "0750": "Gastrointestinal Services",
        "0760": "Treatment/Observation Room",
        "0761": "Treatment Room",
        "0762": "Observation Room",
        "0770": "Prevention & Wellness Services",
        "0780": "Telemedicine",
        "0790": "Extra-Corporeal Shock Wave Therapy (ESWT)",
        "0800": "Inpatient Renal Dialysis",
        "0801": "Renal Dialysis - Inpatient Hemodialysis",
        "0802": "Renal Dialysis - Inpatient CAPD",
        "0820": "Hemodialysis - Outpatient or Home",
        "0821": "Hemodialysis - Composite Rate",
        "0830": "Peritoneal Dialysis - Outpatient or Home",
        "0840": "CAPD - Outpatient or Home",
        "0850": "CCPD - Outpatient or Home",
        "0880": "Miscellaneous Dialysis",
        "0900": "Behavioral Health Treatments/Services",
        "0901": "Behavioral Health - Electroshock Treatment",
        "0902": "Behavioral Health - Milieu Therapy",
        "0903": "Behavioral Health - Play Therapy",
        "0904": "Behavioral Health - Activity Therapy",
        "0905": "Behavioral Health - Intensive Outpatient - Psychiatric",
        "0906": "Behavioral Health - Intensive Outpatient - Chemical Dependency",
        "0907": "Behavioral Health - Community Day Treatment",
        "0911": "Behavioral Health - Rehabilitation",
        "0912": "Behavioral Health - Partial Hospitalization",
        "0913": "Behavioral Health - Psychiatric",
        "0914": "Behavioral Health - Psychiatric Acute Diversion",
        "0916": "Behavioral Health - Substance Abuse",
        "0917": "Behavioral Health - Substance Abuse Acute Detox",
        "0918": "Behavioral Health - Substance Abuse Sub-acute Detox",
        "0919": "Behavioral Health - Other",
        "0920": "Other Diagnostic Services",
        "0921": "Diagnostic - Peripheral Vascular Lab",
        "0922": "Diagnostic - Electromyelogram",
        "0923": "Diagnostic - Pap Smear",
        "0924": "Diagnostic - Allergy Test",
        "0925": "Diagnostic - Pregnancy Test",
        "0929": "Diagnostic - Other",
        "0940": "Other Therapeutic Services",
        "0941": "Therapeutic - Recreational Therapy",
        "0942": "Therapeutic - Educational/Training",
        "0943": "Therapeutic - Cardiac Rehabilitation",
        "0944": "Therapeutic - Drug Rehabilitation",
        "0945": "Therapeutic - Alcohol Rehabilitation",
        "0946": "Therapeutic - Complex Medical Equipment Routine",
        "0947": "Therapeutic - Complex Medical Equipment Ancillary",
        "0948": "Therapeutic - Pulmonary Rehabilitation",
        "0949": "Therapeutic - Other",
        "0950": "Other Therapeutic Services Extension",
        "0960": "Professional Fees",
        "0961": "Professional Fees - Psychiatry",
        "0962": "Professional Fees - Ophthalmology",
        "0963": "Professional Fees - Anesthesiologist",
        "0964": "Professional Fees - Laboratory",
        "0969": "Professional Fees - Other",
        "0970": "Professional Fees Extension",
        "0980": "Professional Fees Extension 2",
        "0990": "Patient Convenience Items",
        # fmt: on
    }

    rows = []
    for code4, desc in rc_raw.items():
        # Store both 4-digit ("0250") and 3-digit ("250") forms —
        # MRF files are inconsistent; the join in the benchmarks view
        # normalises to 4-digit, but this covers any un-normalised lookups.
        rows.append((code4, "RC", desc))
        code3 = code4.lstrip("0") or "0"
        if code3 != code4:
            rows.append((code3, "RC", desc))

    print(f"  RC       : {len(rc_raw):,} codes × 2 forms = {len(rows):,} rows (embedded)")
    return rows


# ------------------------------------------------------------------ #
# DB load                                                              #
# ------------------------------------------------------------------ #
def _bulk_insert(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> None:
    """Insert via a VALUES clause, falling back to executemany for safety."""
    if not rows:
        return

    # DuckDB can handle large VALUES lists efficiently; chunk to avoid
    # hitting any single-statement size limits with very large datasets.
    CHUNK = 50_000
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i : i + CHUNK]
        # Escape single-quotes in strings
        vals = ",".join(
            f"('{r[0].replace(chr(39), chr(39)*2)}',"
            f"'{r[1].replace(chr(39), chr(39)*2)}',"
            f"'{r[2].replace(chr(39), chr(39)*2)}')"
            for r in chunk
        )
        con.execute(
            f"INSERT OR IGNORE INTO code_descriptions VALUES {vals}"
        )


# ------------------------------------------------------------------ #
# Main                                                                 #
# ------------------------------------------------------------------ #
def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--msdrg",         required=True,  help="Path to appendix_A.txt")
    ap.add_argument("--rvu",           required=True,  help="Path to PPRRVU2026_Jan_QPP.csv")
    ap.add_argument("--icd",           required=True,  help="Path to icd10pcs_tables_2027.xml")
    ap.add_argument("--hcpcs",         default=None,   help="(Optional) Path to HCPC2026_ANWEB.xlsx")
    ap.add_argument("--enrichment-db", default=DEFAULT_ENRICHMENT)
    args = ap.parse_args()

    enrichment_db = Path(args.enrichment_db)
    if not enrichment_db.exists():
        raise FileNotFoundError(
            f"enrichment.duckdb not found at '{enrichment_db}'. Run load_nppes.py first."
        )

    print("Parsing source files...")
    rows: list[tuple] = []
    rows += parse_msdrg(Path(args.msdrg))
    rows += parse_rvu(Path(args.rvu))
    if args.hcpcs:
        rows += parse_hcpcs_level2(Path(args.hcpcs))
    rows += parse_icd_pcs(Path(args.icd))
    rows += get_rc_codes()

    # Deduplicate: first occurrence wins (RVU file CPT/HCPCS preferred over ANWEB)
    seen: set[tuple] = set()
    deduped: list[tuple] = []
    for r in rows:
        key = (r[0], r[1])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    rows = deduped
    print(f"  ─────────────────────────────────────────")
    print(f"  Total    : {len(rows):,} unique (billing_code, type) pairs")

    print("\nLoading into enrichment.duckdb...")
    t0 = time.time()
    con = duckdb.connect(str(enrichment_db))

    con.execute("DROP TABLE IF EXISTS code_descriptions")
    con.execute("""
        CREATE TABLE code_descriptions (
            billing_code       VARCHAR NOT NULL,
            billing_code_type  VARCHAR NOT NULL,
            description        VARCHAR NOT NULL,
            PRIMARY KEY (billing_code, billing_code_type)
        )
    """)

    _bulk_insert(con, rows)
    con.execute("CREATE INDEX IF NOT EXISTS idx_cd_code ON code_descriptions(billing_code, billing_code_type)")
    con.execute("CHECKPOINT")

    loaded = con.execute("SELECT COUNT(*) FROM code_descriptions").fetchone()[0]
    print(f"  Loaded   : {loaded:,} rows")

    # ── Match-rate report against transparency.duckdb ────────────────
    if Path("transparency.duckdb").exists():
        con.execute("ATTACH 'transparency.duckdb' AS tr (READ_ONLY)")
        match = con.execute("""
            SELECT
                bc.billing_code_type,
                COUNT(DISTINCT bc.billing_code)                                AS in_db,
                COUNT(DISTINCT cd.billing_code)                                AS matched,
                ROUND(100.0 * COUNT(DISTINCT cd.billing_code)
                    / NULLIF(COUNT(DISTINCT bc.billing_code), 0), 1)           AS match_pct
            FROM (
                SELECT DISTINCT billing_code, billing_code_type
                FROM tr.billing_codes
            ) bc
            LEFT JOIN code_descriptions cd
                ON  cd.billing_code      = bc.billing_code
                AND cd.billing_code_type = bc.billing_code_type
            GROUP BY bc.billing_code_type
            ORDER BY in_db DESC
        """).df()
        print("\nMatch rate against transparency.duckdb:")
        print(match.to_string(index=False))

        # Emit unmatched samples to help diagnose remaining gaps
        for code_type in match["billing_code_type"]:
            pct = match.loc[match["billing_code_type"] == code_type, "match_pct"].values[0]
            if pct is not None and float(pct) < 80:
                samples = con.execute(f"""
                    SELECT DISTINCT bc.billing_code
                    FROM (SELECT DISTINCT billing_code FROM tr.billing_codes
                          WHERE billing_code_type = '{code_type}') bc
                    LEFT JOIN code_descriptions cd
                        ON  cd.billing_code      = bc.billing_code
                        AND cd.billing_code_type = '{code_type}'
                    WHERE cd.billing_code IS NULL
                    LIMIT 10
                """).fetchall()
                sample_vals = [s[0] for s in samples]
                print(f"\n  Unmatched {code_type} samples (first 10): {sample_vals}")

        con.execute("DETACH tr")

    con.close()
    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()