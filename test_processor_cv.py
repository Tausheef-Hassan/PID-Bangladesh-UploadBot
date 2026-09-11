# test_processor_cv.py
# Runs the real ImageProcessor.find_white_separator() against a sample of live
# PID archive images and writes annotated debug output + a summary report.
#
# Usage:
#   python test_processor_cv.py [--pages N] [--page-start P] [--out DIR]
#
# Options:
#   --pages N       Number of archive pages to scrape  (default: 3)
#   --page-start P  First archive page number to fetch (default: 665)
#   --out DIR       Output directory                   (default: test_output)

import argparse
import os
import time
import warnings
from datetime import datetime

import cv2
import numpy as np
import requests
from bs4 import BeautifulSoup

from src.image_processor import ImageProcessor
from src.scraper import normalize_url

warnings.filterwarnings("ignore")  # suppress SSL warnings


HEADERS = {"User-Agent": "Mozilla/5.0 (PID-test-script)"}
REQUEST_TIMEOUT = 15
RESULT_COLORS = {
    "primary":  (0, 200, 0),      # green   — column-scan separator
    "fallback": (0, 165, 255),    # orange  — whitespace-run fallback
    "failed":   (0, 0, 255),      # red     — no separator found
}
RESULT_LABELS = {
    "primary":  "Primary (column scan)",
    "fallback": "Fallback (whitespace run)",
    "failed":   "FAILED — no separator",
}


def fetch_image_urls(page: int) -> list[str]:
    """Return all thumbnail image URLs from one PID archive page."""
    url = (
        f"https://pressinform.gov.bd/pages/daily-photos"
        f"?archived=true&page={page}&page_size=100"
    )
    try:
        resp = requests.get(url, headers=HEADERS, verify=False, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        table = soup.find("table", id="noticeTable")
        if not table:
            print(f"  [page {page}] WARNING: no #noticeTable found")
            return []
        return [img["src"] for img in table.find_all("img") if img.get("src")]
    except Exception as exc:
        print(f"  [page {page}] ERROR fetching page: {exc}")
        return []


def download_cv2(url: str):
    """Download image bytes and decode with OpenCV. Returns (img_cv, error)."""
    try:
        resp = requests.get(url, headers=HEADERS, verify=False, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        arr = np.asarray(bytearray(resp.content), dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return None, "cv2.imdecode returned None"
        return img, None
    except Exception as exc:
        return None, str(exc)


def annotate_debug(image, sep_row: int, result: str, img_idx: int) -> np.ndarray:
    """Draw the separator line and a result label on a copy of the image."""
    debug = image.copy()
    h, w = debug.shape[:2]
    color = RESULT_COLORS[result]
    label = f"#{img_idx}  {RESULT_LABELS[result]}"

    if sep_row != -1:
        cv2.line(debug, (0, sep_row), (w, sep_row), color, 3)
        cv2.putText(
            debug, f"row {sep_row}", (10, max(sep_row - 8, 20)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA
        )

    # Semi-transparent banner at the top
    overlay = debug.copy()
    cv2.rectangle(overlay, (0, 0), (w, 36), (30, 30, 30), -1)
    cv2.addWeighted(overlay, 0.6, debug, 0.4, 0, debug)
    cv2.putText(
        debug, label, (8, 24),
        cv2.FONT_HERSHEY_SIMPLEX, 0.65, color, 2, cv2.LINE_AA
    )
    return debug


def demo():
    """Self-check: a synthetic photo band over a white text band is split correctly."""
    img = np.zeros((1000, 800, 3), dtype=np.uint8)
    img[:600] = (40, 90, 160)     # "photograph"
    img[600:] = (255, 255, 255)   # white text band

    sep_row, used_fallback, offset = ImageProcessor().find_white_separator(img)
    assert offset >= 2, offset
    assert 560 <= sep_row <= 600, f"separator {sep_row} not near the 600px band edge"

    photo, text = ImageProcessor().crop_image_sections(img, sep_row)
    assert photo.shape[0] == sep_row and text.shape[0] == 1000 - sep_row
    print(f"demo OK — separator at row {sep_row} (fallback={used_fallback}, offset={offset})")


def main():
    parser = argparse.ArgumentParser(description="Test PID separator detection")
    parser.add_argument("--pages",      type=int, default=3,             help="Number of archive pages to test")
    parser.add_argument("--page-start", type=int, default=665,           help="First archive page number")
    parser.add_argument("--out",        type=str, default="test_output", help="Output directory")
    parser.add_argument("--demo",       action="store_true",             help="Run the offline self-check and exit")
    args = parser.parse_args()

    if args.demo:
        demo()
        return

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    processor = ImageProcessor()

    print(f"\nFetching URLs from {args.pages} page(s) starting at page {args.page_start}...")
    all_urls: list[tuple[int, str]] = []   # (page_number, url)
    for p in range(args.page_start, args.page_start + args.pages):
        urls = fetch_image_urls(p)
        print(f"  Page {p}: {len(urls)} images found")
        all_urls.extend((p, u) for u in urls)
        time.sleep(0.3)   # be polite

    total = len(all_urls)
    print(f"\nTotal images to test: {total}\n{'='*60}")

    counters = {"primary": 0, "fallback": 0, "failed": 0, "download_error": 0}
    report_lines: list[str] = []

    for idx, (page, url) in enumerate(all_urls):
        print(f"[{idx+1:>3}/{total}] page={page}  {url}")

        img, err = download_cv2(normalize_url(url) if url.startswith("http") else url)
        if err:
            print(f"         x Download error: {err}")
            counters["download_error"] += 1
            report_lines.append(f"{idx+1:>3}  DOWNLOAD_ERROR  page={page}  {url}\n    {err}")
            continue

        h, w = img.shape[:2]
        t0 = time.perf_counter()
        sep_row, used_fallback, offset = processor.find_white_separator(img)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        result = "failed" if sep_row == -1 else ("fallback" if used_fallback else "primary")
        counters[result] += 1
        status = "x" if result == "failed" else "+"
        print(f"         {status} {RESULT_LABELS[result]}  row={sep_row}  size={w}x{h}  {elapsed_ms:.1f}ms")

        cv2.imwrite(os.path.join(out_dir, f"{idx+1:03d}_debug.jpg"),
                    annotate_debug(img, sep_row, result, idx + 1))

        # Write cropped sections through the same code path the bot uses
        photo, text = processor.crop_image_sections(img, sep_row, apply_side_crop=used_fallback)
        if photo is not None and photo.size > 0:
            cv2.imwrite(os.path.join(out_dir, f"{idx+1:03d}_photo.jpg"), photo)
        if text is not None and text.size > 0:
            cv2.imwrite(os.path.join(out_dir, f"{idx+1:03d}_text.jpg"), text)

        report_lines.append(
            f"{idx+1:>3}  {result:<8}  row={sep_row:<6}  offset={offset:<3}"
            f"  {elapsed_ms:>6.1f}ms  size={w}x{h}  page={page}  {url}"
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    processed = total - counters["download_error"]
    found = counters["primary"] + counters["fallback"]
    pct = lambda n: (n / processed * 100) if processed else 0

    summary = f"""
{'='*60}
PID Separator Detection — Test Report
Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Pages     : {args.page_start} -> {args.page_start + args.pages - 1}
{'='*60}
Total images   : {total}
Download errors: {counters['download_error']}
Successfully processed: {processed}

Detection results:
  Primary  (column scan)      : {counters['primary']:>4}  ({pct(counters['primary']):.1f}%)
  Fallback (whitespace run)   : {counters['fallback']:>4}  ({pct(counters['fallback']):.1f}%)
  Failed   (no separator)     : {counters['failed']:>4}  ({pct(counters['failed']):.1f}%)

Overall detection rate: {found}/{processed} = {pct(found):.1f}%
{'='*60}

Per-image log:
{'-'*60}
"""

    print(summary)

    report_path = os.path.join(out_dir, "test_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(summary)
        f.write("\n".join(report_lines))
        f.write("\n")

    print(f"Output saved to: {os.path.abspath(out_dir)}/")
    print(f"Report saved to: {os.path.abspath(report_path)}")


if __name__ == "__main__":
    main()
