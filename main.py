# main.py
# Pipeline orchestrator for the PID Image Processor & Uploader.
# All heavy logic lives in the individual modules; this file wires them together.
#
# Execution order:
#   0. Boot checks (pywikibot credentials, infrastructure)
#   1. Scrape pressinform.gov.bd  →  work queue
#   2. For each image:
#       2a. Archive to Wayback Machine
#       2b. Download + separator detection + OCR  (image_processor)
#       2c. Translate Bengali  →  English  (translator)
#       2d. Generate Wikimedia filename  (translator)
#       2e. Upload to Commons  (uploader)
#       2f. Update Module:PIDDateData  (uploader)
#   3. Log results to Commons  (commons_log)

import concurrent.futures
import os
import sys
import threading
import traceback

from google import genai
from google.cloud import translate_v2 as translate

# ── Project modules ───────────────────────────────────────────────────────────
import config
import credentials
from src import run_state, wayback
from src.commons_log import log_to_commons
from src.image_processor import ImageProcessor
from src.scraper import scrape_data
from src.translator import (
    apply_translation_replacements,
    generate_title,
    load_translation_replacements,
    translate_text,
)
from src.uploader import (
    ensure_pid_infrastructure,
    initialize_pywikibot,
    batch_update_pid_date_data,
    upload_to_commons,
)

logger = config.logger


# ── Main pipeline ─────────────────────────────────────────────────────────────

def main():
    """Record the run, then hand off to the pipeline."""
    with run_state.record_run() as run:
        _pipeline(run)


def _pipeline(run):
    print("=" * 60)
    print("PID Image Processor & Uploader")
    print("=" * 60)
    print()

    # ── 0a. Pywikibot config sanity check ─────────────────────────────────────
    if not os.path.exists(config.USER_CONFIG_PATH):
        print(f"ERROR: Config file not found: {config.USER_CONFIG_PATH}")
        print("Please create user-config.py in the same directory as this script")
        sys.exit(1)

    if not os.path.exists(config.PASSWORD_FILE_PATH):
        print(f"ERROR: Password file not found: {config.PASSWORD_FILE_PATH}")
        print("Please create user-password.py in the same directory as this script")
        sys.exit(1)

    # ── 0b. Pywikibot login + infrastructure (one login for the whole run) ────
    print("\nInitializing Pywikibot...")
    site = initialize_pywikibot()
    if site is None:
        print("Error: Failed to initialize Pywikibot")
        sys.exit(1)

    print("\nChecking and creating categories/modules before scraping...")
    ensure_pid_infrastructure(site)

    # ── 0c. Pre-translation replacement table ─────────────────────────────────
    _translation_replacements = load_translation_replacements()

    # ── 0d. Internet Archive keys + pending Wayback queue ─────────────────────
    print("\nLoading Internet Archive S3 keys for Save Page Now...")
    credentials.load_ia_keys()

    print("\nChecking Wayback Machine pending queue in background...")
    threading.Thread(target=wayback.retry_wayback_queue, daemon=False).start()

    # ── Step 1: Scrape ────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("STEP 1: Scraping data from pressinform.gov.bd")
    print("=" * 60)
    scraped_data, wikimedia_checksums = scrape_data()

    # ── Load Google credentials ───────────────────────────────────────────────
    print("\nLoading Google credentials...")
    credentials.load_credentials()
    print("Setting up Google credentials...")
    creds_path = credentials.setup_credentials()

    try:
        # ── Initialise API clients ────────────────────────────────────────────
        print("Initializing Google Cloud clients...")
        image_processor = ImageProcessor()
        # Boot instance is the only one allowed to sweep orphaned Drive files:
        # workers spin up mid-run, when another worker's OCR file may be live.
        success, message = image_processor.initialize_vision_client(cleanup_orphans=True)
        if not success:
            print(f"Error: {message}")
            sys.exit(1)
        print(message)

        print("Loading Gemini AI Studio key (free primary account)...")
        gemini_api_key = credentials.load_gemini_api_key()
        genai_client = genai.Client(api_key=gemini_api_key)

        print("Loading Gemini Vertex AI client (paid fallback account)...")
        project_id = credentials._google_credentials.get(
            "project_id") if credentials._google_credentials else os.environ.get("GOOGLE_CLOUD_PROJECT")
        vertex_client = genai.Client(
            vertexai=True, project=project_id, location=config.VERTEX_LOCATION)

        translate_client = translate.Client()
        print("Gemini (AI Studio primary, Vertex fallback) and Translate clients initialized")

        # ── No new images? ────────────────────────────────────────────────────
        if scraped_data is None:
            print("\nNo new images found. Nothing to log.")
            return

        # ── Load work queue ───────────────────────────────────────────────────
        print(f"\nProcessing {len(scraped_data)} new images in memory...")
        rows = [
            {
                "unique_id": unique_id or f"image_{i}",
                "date": date,
                "image_url": image_url,
                "detail_url": detail_url,
                "ocr_text": "",
                "ocr_status": "",
                "translation": "",
                "translation_status": "",
                "filename": "",
                "filename_status": "",
                "pid_date_data_info": "",
                "pid_date_data_status": "",
                "wikitext_description": "",
                "upload_status": "",
            }
            for i, (unique_id, date, image_url, detail_url) in enumerate(scraped_data)
        ]
        total_rows = len(rows)
        print(f"Total rows to process: {total_rows}")

        success_count = 0
        failed_count = 0

        # Every URL that must never be retried — uploaded, duplicate, or already
        # on Commons — collected here for one batch edit instead of one per row.
        pid_updates = []

        # ── Per-image loop ────────────────────────────────────────────────────
        wayback_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        counter_lock = threading.Lock()
        upload_lock = threading.Lock()

        thread_local = threading.local()

        def get_image_processor():
            if not hasattr(thread_local, "image_processor"):
                ip = ImageProcessor()
                ip.initialize_vision_client()
                thread_local.image_processor = ip
            return thread_local.image_processor

        def process_row(idx):
            nonlocal success_count, failed_count
            row = rows[idx]
            print(f"\n{'='*60}")
            print(f"Processing row {idx + 1}/{total_rows}")
            print(f"{'='*60}")

            try:
                unique_id = row["unique_id"]
                date_str = row["date"]
                image_url = row["image_url"]
                detail_url = row["detail_url"]

                if not image_url or image_url == 'nan':
                    print(f"Row {idx + 1}: No URL, skipping")
                    row["ocr_status"] = "No URL"
                    return

                # Step 1.5 — Archive source URLs to Wayback Machine (Async)
                print(f"\nSTEP 1.5: Archiving to Wayback Machine (Async)...")
                # confirm=False: submit and move on. The queue-confirmation pass
                # at the start of the next run checks whether the capture landed.
                wayback_executor.submit(
                    wayback.archive_to_wayback, image_url, confirm=False)
                if detail_url:
                    wayback_executor.submit(
                        wayback.archive_to_wayback, detail_url, confirm=False)

                # Step 2 — Download + OCR
                print(f"\nSTEP 2: Processing image...")
                local_processor = get_image_processor()
                result = local_processor.process_image(
                    idx + 1, image_url, wikimedia_checksums)

                row["ocr_text"] = result['ocr_text']
                row["ocr_status"] = result['status']

                # Checksum duplicate — already on Commons under a different URL
                if result.get('is_duplicate'):
                    print(
                        f"Row {idx + 1}: Checksum match — registering URL in module, skipping upload")
                    dup_checksum = result.get('checksum', '')
                    row["pid_date_data_info"] = f"JSON Tabular: {image_url} | {date_str} | {dup_checksum}"
                    row["upload_status"] = "Skipped (checksum duplicate)"
                    row["pid_date_data_status"] = "Success (queued, dup)"
                    with counter_lock:
                        pid_updates.append(
                            (image_url, date_str, dup_checksum, unique_id, ""))
                        success_count += 1
                    return

                if result['image'] is None or result['status'].startswith('Error') or result['status'].startswith('OCR failed'):
                    print(f"Row {idx + 1}: Image processing failed")
                    with counter_lock:
                        failed_count += 1
                    return

                img_format = result.get('format', 'jpg')

                # Step 3 — Translate
                print(f"\nSTEP 3: Translating text...")
                bengali_text_raw = result['ocr_text']
                print(f"Sanitized OCR Data: {bengali_text_raw}")
                bengali_text = apply_translation_replacements(
                    bengali_text_raw, _translation_replacements)
                print(f"After pre-translation replacements: {bengali_text}")
                translation, trans_status = translate_text(
                    genai_client, vertex_client, translate_client, bengali_text, idx + 1)
                print(f"Translation Data: {translation}")

                row["translation"] = translation
                row["translation_status"] = trans_status

                if trans_status != "Success":
                    print(f"Row {idx + 1}: Translation failed")
                    with counter_lock:
                        failed_count += 1
                    return

                # Step 4 — Generate filename
                print(f"\nSTEP 4: Generating title...")
                title, title_status = generate_title(
                    genai_client, vertex_client, translation, date_str, idx + 1, img_format)
                print(f"Full Title Data (with extension): {title}")

                row["filename"] = title
                row["filename_status"] = title_status

                if title_status != "Success":
                    print(f"Row {idx + 1}: Title generation failed")
                    with counter_lock:
                        failed_count += 1
                    return

                # Step 5 — Prepare description
                print(f"\nSTEP 5: Preparing metadata...")
                img_checksum = result.get('checksum', '')
                row["pid_date_data_info"] = f"JSON Tabular: {image_url} | {date_str} | {img_checksum}"

                description = f'''=={{{{int:filedesc}}}}==
{{{{Information
 |description = {{{{bn|1={bengali_text_raw.strip().lstrip('﻿').strip()}}}}}{{{{en|1={translation.strip()}{{{{Auto-translated PID English description}}}}}}}}
 |date = {{{{Date-PID|{date_str}}}}}
 |source = {{{{Source-PID | url={image_url}}}}}
 |author = {{{{Institution:Press Information Department}}}}
 |permission =
 |other versions =
}}}}
=={{{{int:license-header}}}}==
{{{{PD-BDGov-PID}}}}
[[Category: Uploaded with pypan]]'''

                row["wikitext_description"] = description

                # Step 6 — Upload
                print(f"\nSTEP 6: Uploading to Wikimedia Commons...")
                with upload_lock:
                    upload_success, upload_error = upload_to_commons(
                        site, result['image'], title, img_format,
                        result.get('exif'), description
                    )

                with counter_lock:
                    if upload_success:
                        row["upload_status"] = "Success"
                        row["pid_date_data_status"] = "Success (queued)"
                        success_count += 1
                        print(f"Row {idx + 1}: Upload successful")
                        pid_updates.append(
                            (image_url, date_str, img_checksum, unique_id, title))
                    else:
                        row["upload_status"] = f"Failed: {upload_error}"
                        failed_count += 1
                        print(f"Row {idx + 1}: Upload failed - {upload_error}")
                        if "already exists" in str(upload_error).lower():
                            # Register it anyway so the next run stops retrying.
                            row["pid_date_data_status"] = "Success (queued, exists)"
                            pid_updates.append(
                                (image_url, date_str, img_checksum, unique_id, title))
                            print(
                                f"Row {idx + 1}: File already exists — queued for PIDDateData")

            except Exception as e:
                traceback.print_exc()
                print(f"Row {idx + 1}: Unhandled exception: {e}")
                row["upload_status"] = f"Exception: {str(e)}"
                with counter_lock:
                    failed_count += 1

        # Run process_row for all rows concurrently (AI is parallel, Uploads are thread-locked)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(process_row, idx)
                       for idx in range(total_rows)]
            concurrent.futures.wait(futures)

        # ── Final save + Commons log ──────────────────────────────────────────

        if pid_updates:
            print(
                f"\nBatch updating {len(pid_updates)} records to PIDDateData...")
            batch_update_pid_date_data(site, pid_updates)

        print("\nLogging results to Wikimedia Commons...")
        if log_to_commons(site, rows, success_count, failed_count, total_rows):
            print("Successfully logged to Commons.")

        print("\n" + "=" * 60)
        print("PROCESSING COMPLETED")
        print("=" * 60)
        run["scraped"] = total_rows
        run["uploaded"] = success_count
        run["failed"] = failed_count
        run["duplicates"] = sum(
            1 for r in rows if r["upload_status"].startswith("Skipped (checksum duplicate)"))

        print(f"Total rows processed: {total_rows}")
        print(f"Successful uploads:   {success_count}")
        print(f"Failed uploads:       {failed_count}")
        print("=" * 60)

        print("\nBackground archiving may still be running. The script will wait automatically before exiting.")
        wayback_executor.shutdown(wait=False)

    finally:
        # Always clean up the temp credentials file
        try:
            os.unlink(creds_path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
