import os
import sys
import json
import argparse
import requests
import duckdb
from urllib.parse import urlparse

# Import stream_parser helper functions
try:
    import stream_parser
except ImportError:
    print("Error: Could not import 'stream_parser.py'. Ensure stream_parser.py is in the same directory.")
    sys.exit(1)

# Configuration Constants
FILTERED_JSON_FILE = "in_network_rates_filtered.json"
PROGRESS_FILE = "processed_count.txt"
DB_FILE = "transparency.duckdb"
SCHEMA_FILE = "schema.sql"
DOWNLOAD_DIR = "./temp_downloads"

def initialize_db():
    """Initializes DuckDB schema idempotently and sets optimization flags."""
    if not os.path.exists(SCHEMA_FILE):
        print(f"Warning: {SCHEMA_FILE} not found. Skipping schema execution.")
        return
    con = duckdb.connect(DB_FILE)
    try:
        # Performance tuning: speed up bulk inserts when insertion order isn't strict
        con.execute("SET preserve_insertion_order = false;")
        
        with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
            con.execute(f.read())
    finally:
        con.close()

def get_processed_count():
    """Reads the current processed count from the progress tracking text file."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return int(content) if content else 0
        except ValueError:
            return 0
    return 0

def update_processed_count(count):
    """Atomically updates the progress text file with the latest count."""
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        f.write(f"{count}\n")

def download_file(url, target_path):
    """Downloads a file in streaming mode with atomic writing (.tmp extension)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    temp_target = target_path + ".tmp"
    try:
        with requests.get(url, headers=headers, stream=True, timeout=300) as response:
            response.raise_for_status()
            with open(temp_target, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                    if chunk:
                        f.write(chunk)
        os.rename(temp_target, target_path)
    except Exception as e:
        if os.path.exists(temp_target):
            os.remove(temp_target)
        raise e

def main():
    parser = argparse.ArgumentParser(description="Batch process MRF rate files into DuckDB.")
    parser.add_argument(
        "-m", "--max-files",
        type=int,
        default=None,
        help="Maximum number of files to process in this run (e.g., --max-files 5)"
    )
    args = parser.parse_args()

    # 1. Load the filtered JSON list
    if not os.path.exists(FILTERED_JSON_FILE):
        print(f"Error: Filtered file '{FILTERED_JSON_FILE}' not found. Run the extraction script first.")
        sys.exit(1)

    with open(FILTERED_JSON_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    blobs = data.get("blobs", [])
    total_files = len(blobs)
    print(f"Found {total_files} total files in {FILTERED_JSON_FILE}.")

    # 2. Setup DuckDB schema & progress tracker
    initialize_db()
    processed_count = get_processed_count()
    print(f"Resuming processing from index {processed_count}/{total_files}...")

    if processed_count >= total_files:
        print("All files have already been processed!")
        return

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    con = duckdb.connect(DB_FILE)

    processed_in_this_run = 0

    try:
        # Loop through items starting from where we left off
        for index in range(processed_count, total_files):
            # Check if we reached user-specified max limit
            if args.max_files is not None and processed_in_this_run >= args.max_files:
                print(f"\nReached batch limit of {args.max_files} file(s) for this run. Stopping.")
                break

            item = blobs[index]
            
            # Extract download URL and name
            download_url = item.get("downloadUrl") if isinstance(item, dict) else item
            filename = item.get("name") if isinstance(item, dict) else os.path.basename(urlparse(download_url).path)

            if not download_url:
                print(f"Skipping index {index}: missing downloadUrl.")
                processed_count += 1
                update_processed_count(processed_count)
                continue

            local_filepath = os.path.join(DOWNLOAD_DIR, filename)

            print(f"\n[{index + 1}/{total_files}] (Run count: {processed_in_this_run + 1}/{args.max_files if args.max_files else '∞'}) Downloading: {filename}")
            try:
                # Step A: Download file to local storage
                download_file(download_url, local_filepath)

                # Step B: Pass file path to stream_parser engine
                print(f"Processing and inserting into DuckDB...")
                stream_parser.process_file(con, local_filepath, filename)

            except Exception as e:
                print(f"Error processing {filename}: {e}")
                if os.path.exists(local_filepath):
                    os.remove(local_filepath)
                print("Stopping pipeline due to error. Fix issue and re-run to resume.")
                break
            else:
                # Step C: Delete downloaded file upon successful processing
                if os.path.exists(local_filepath):
                    os.remove(local_filepath)
                    print(f"Cleaned up local file: {local_filepath}")

                # Step D: Update progress counter file & run limit counter
                processed_count += 1
                processed_in_this_run += 1
                update_processed_count(processed_count)
                print(f"Progress updated: {processed_count}/{total_files} total completed.")

    finally:
        # Flush Write-Ahead Log (WAL) to main database file before exiting
        try:
            print("\nFlushing WAL to disk (CHECKPOINT)...")
            con.execute("CHECKPOINT;")
        except Exception as e:
            print(f"Warning: CHECKPOINT failed: {e}")

        con.close()
        print("DuckDB connection closed.")

if __name__ == "__main__":
    main()