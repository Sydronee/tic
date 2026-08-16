import os
import sys
import json
import argparse
import requests
import duckdb
from concurrent.futures import ThreadPoolExecutor
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
PREFETCH_COUNT = 4  # Number of upcoming files to prefetch in parallel

def initialize_db():
    """Initializes DuckDB schema idempotently."""
    if not os.path.exists(SCHEMA_FILE):
        print(f"Warning: {SCHEMA_FILE} not found. Skipping schema execution.")
        return
    con = duckdb.connect(DB_FILE)
    try:
        with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
            con.execute(f.read())
    finally:
        con.close()

def get_processed_count():
    """Reads current processed count from progress tracking file."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return int(content) if content else 0
        except ValueError:
            return 0
    return 0

def update_processed_count(count):
    """Atomically updates progress text file with the latest count."""
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        f.write(f"{count}\n")

def download_file(url, target_path):
    """Downloads a file if it doesn't already exist locally."""
    if os.path.exists(target_path):
        return target_path  # Already prefetched

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

    return target_path

def main():
    parser = argparse.ArgumentParser(description="Batch process MRF rate files into DuckDB with prefetched downloads.")
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

    # Determine window range to process
    end_index = total_files
    if args.max_files is not None:
        end_index = min(processed_count + args.max_files, total_files)

    # Background downloader for upcoming files. Managed explicitly (not via
    # `with`) so that on error we can shut down WITHOUT waiting for every
    # in-flight/queued prefetch to finish first — see the finally block below.
    executor = ThreadPoolExecutor(max_workers=PREFETCH_COUNT)
    futures = {}

    try:
        # Main processing loop
        for index in range(processed_count, end_index):
            item = blobs[index]
            
            download_url = item.get("downloadUrl") if isinstance(item, dict) else item
            filename = item.get("name") if isinstance(item, dict) else os.path.basename(urlparse(download_url).path)

            if not download_url:
                print(f"Skipping index {index}: missing downloadUrl.")
                processed_count += 1
                update_processed_count(processed_count)
                continue

            local_filepath = os.path.join(DOWNLOAD_DIR, filename)

            # Ensure background prefetching for the next items in queue
            prefetch_end = min(index + PREFETCH_COUNT + 1, end_index)
            for pf_idx in range(index, prefetch_end):
                if pf_idx not in futures:
                    pf_item = blobs[pf_idx]
                    pf_url = pf_item.get("downloadUrl") if isinstance(pf_item, dict) else pf_item
                    if not pf_url:
                        continue  # no URL to prefetch; the main loop will skip this index itself
                    pf_filename = pf_item.get("name") if isinstance(pf_item, dict) else os.path.basename(urlparse(pf_url).path)
                    pf_path = os.path.join(DOWNLOAD_DIR, pf_filename)

                    # Submit prefetch task
                    futures[pf_idx] = executor.submit(download_file, pf_url, pf_path)

            print(f"\n[{index + 1}/{total_files}] (Run count: {processed_in_this_run + 1}/{args.max_files if args.max_files else '∞'}) Fetching/Processing: {filename}")
            
            # Await download for the current file
            try:
                futures.pop(index).result()
            except Exception as e:
                print(f"Error downloading {filename}: {e}")
                break

            # Step B: Parse and insert into DuckDB
            try:
                print("Processing and inserting into DuckDB...")
                stream_parser.process_file(con, local_filepath, filename)
            except Exception as e:
                print(f"Error processing {filename}: {e}")
                if os.path.exists(local_filepath):
                    os.remove(local_filepath)
                print("Stopping pipeline due to error. Fix issue and re-run to resume.")
                break
            else:
                # Step C: Delete local file after successful insertion
                if os.path.exists(local_filepath):
                    os.remove(local_filepath)
                    print(f"Cleaned up local file: {local_filepath}")

                # Step D: Update progress counter file & run limit counter
                processed_count += 1
                processed_in_this_run += 1
                update_processed_count(processed_count)
                print(f"Progress updated: {processed_count}/{total_files} total completed.")

    finally:
        # Don't wait for queued/in-flight prefetches we no longer need — cancel
        # anything not yet started, and don't block the process on the rest.
        # (Files that were mid-download when we cancel will just leave a stray
        # .tmp file behind, cleaned up by download_file's own except-block on
        # its next attempt, or safe to delete manually from DOWNLOAD_DIR.)
        executor.shutdown(wait=False, cancel_futures=True)

        # Consolidate Write-Ahead Log (WAL) into transparency.duckdb before exit
        try:
            print("Flushing WAL to disk (CHECKPOINT)...")
            con.execute("CHECKPOINT;")
        except Exception as e:
            print(f"Warning: CHECKPOINT failed: {e}")
            
        con.close()
        print("DuckDB connection closed.")

if __name__ == "__main__":
    main()