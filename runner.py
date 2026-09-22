import os
import sys
import json
import argparse
import duckdb
from urllib.parse import urlparse

# Import stream_parser helper functions
try:
    import stream_parser
    from ingest_utils import append_run_log, download_file
except ImportError:
    print("Error: Could not import 'stream_parser.py'. Ensure stream_parser.py is in the same directory.")
    sys.exit(1)

# Configuration Constants
FILTERED_JSON_FILE = "in_network_rates_filtered.json"
PROGRESS_FILE = "processed_count.txt"
DB_FILE = "transparency.duckdb"
SCHEMA_FILE = "schema.sql"
DOWNLOAD_DIR = "./temp_downloads"
RUN_LOG_FILE = "ingestion_runs.jsonl"

def initialize_db(db_file, schema_file):
    """Initializes DuckDB schema idempotently and sets optimization flags."""
    if not os.path.exists(schema_file):
        print(f"Warning: {schema_file} not found. Skipping schema execution.")
        return
    con = duckdb.connect(db_file)
    try:
        # Performance tuning: speed up bulk inserts when insertion order isn't strict
        con.execute("SET preserve_insertion_order = false;")
        
        with open(schema_file, "r", encoding="utf-8") as f:
            con.execute(f.read())
    finally:
        con.close()

def get_processed_count(progress_file):
    """Reads the current processed count from the progress tracking text file."""
    if os.path.exists(progress_file):
        try:
            with open(progress_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return int(content) if content else 0
        except ValueError:
            return 0
    return 0

def update_processed_count(progress_file, count):
    """Atomically updates the progress text file with the latest count."""
    temp_progress = f"{progress_file}.tmp"
    with open(temp_progress, "w", encoding="utf-8") as f:
        f.write(f"{count}\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_progress, progress_file)

def main():
    parser = argparse.ArgumentParser(description="Batch process MRF rate files into DuckDB.")
    parser.add_argument(
        "-m", "--max-files",
        type=int,
        default=None,
        help="Maximum number of files to process in this run (e.g., --max-files 5)"
    )
    parser.add_argument("--manifest", default=FILTERED_JSON_FILE,
                        help=f"Input manifest JSON (default: {FILTERED_JSON_FILE})")
    parser.add_argument("--db", default=DB_FILE,
                        help=f"DuckDB output path (default: {DB_FILE})")
    parser.add_argument("--progress", default=PROGRESS_FILE,
                        help=f"Progress file (default: {PROGRESS_FILE})")
    parser.add_argument("--schema", default=SCHEMA_FILE,
                        help=f"DuckDB schema path (default: {SCHEMA_FILE})")
    parser.add_argument("--download-dir", default=DOWNLOAD_DIR,
                        help=f"Temporary download directory (default: {DOWNLOAD_DIR})")
    parser.add_argument("--run-log", default=RUN_LOG_FILE,
                        help=f"JSONL ingestion event log (default: {RUN_LOG_FILE})")
    args = parser.parse_args()

    # 1. Load the filtered JSON list
    if not os.path.exists(args.manifest):
        print(f"Error: Manifest '{args.manifest}' not found. Create it first.")
        sys.exit(1)

    with open(args.manifest, "r", encoding="utf-8") as f:
        data = json.load(f)

    blobs = data.get("blobs") or data.get("files", [])
    blobs.sort(key=lambda item: item.get("size_bytes", float("inf"))
               if isinstance(item, dict) else float("inf"))
    total_files = len(blobs)
    print(f"Found {total_files} total files in {args.manifest}.")

    # 2. Setup DuckDB schema & progress tracker
    initialize_db(args.db, args.schema)
    processed_count = get_processed_count(args.progress)
    print(f"Resuming processing from index {processed_count}/{total_files}...")

    if processed_count >= total_files:
        print("All files have already been processed!")
        return

    os.makedirs(args.download_dir, exist_ok=True)
    con = duckdb.connect(args.db)

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
                update_processed_count(args.progress, processed_count)
                continue

            local_filepath = os.path.join(args.download_dir, filename)

            print(f"\n[{index + 1}/{total_files}] (Run count: {processed_in_this_run + 1}/{args.max_files if args.max_files else '∞'}) Downloading: {filename}")
            try:
                # Step A: Download file to local storage
                metadata = item if isinstance(item, dict) else {}
                download_info = download_file(
                    download_url,
                    local_filepath,
                    expected_size=metadata.get("size_bytes"),
                )

                # Step B: Pass file path to stream_parser engine
                print(f"Processing and inserting into DuckDB...")
                stream_parser.process_file(con, local_filepath, filename)

            except Exception as e:
                append_run_log(args.run_log, status="failed", index=index, filename=filename,
                               url=download_url, error=str(e))
                print(f"Error processing {filename}: {e}")
                if os.path.exists(local_filepath):
                    os.remove(local_filepath)
                print("Stopping pipeline due to error. Fix issue and re-run to resume.")
                break
            else:
                append_run_log(args.run_log, status="complete", index=index, filename=filename,
                               url=download_url, **download_info)
                # Step C: Delete downloaded file upon successful processing
                if os.path.exists(local_filepath):
                    os.remove(local_filepath)
                    print(f"Cleaned up local file: {local_filepath}")

                # Step D: Update progress counter file & run limit counter
                processed_count += 1
                processed_in_this_run += 1
                update_processed_count(args.progress, processed_count)
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