import json
import requests

URL = "https://transparency-in-coverage.uhc.com/api/v1/uhc/blobs"
RAW_FILE = "uhc_blobs_raw.json"
FILTERED_FILE = "in_network_rates_filtered.json"

def fetch_and_filter_blobs():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    print(f"Fetching data from {URL}...")
    try:
        response = requests.get(URL, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching data: {e}")
        return

    # 1. Save raw response
    with open(RAW_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Saved raw JSON data to {RAW_FILE}")

    # 2. Filter keeping original dictionary elements intact
    target_extensions = ("in-network-rates.json", "in-network-rates.json.gz")
    blobs = data.get("blobs", []) if isinstance(data, dict) else []

    filtered_blobs = [
        item for item in blobs
        if isinstance(item, dict) and (
            str(item.get("name", "")).lower().endswith(target_extensions) or
            str(item.get("downloadUrl", "")).lower().endswith(target_extensions)
        )
    ]
    filtered_blobs.sort(key=lambda x: x.get("size", 0), reverse=False)  # Sort by size ascending

    # 3. Save keeping original JSON format structure
    output_payload = {
        "blobs": filtered_blobs
    }

    with open(FILTERED_FILE, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2)

    print(f"Successfully saved {len(filtered_blobs)} items to {FILTERED_FILE}")

if __name__ == "__main__":
    fetch_and_filter_blobs()