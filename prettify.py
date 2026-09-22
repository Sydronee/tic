import json

with open("2026-09-01_cigna-health-life-insurance-company_index.json", "r") as f_in:
    data = json.load(f_in)

with open("cignaPretty.json", "w") as f_out:
    json.dump(data, f_out, indent=4)