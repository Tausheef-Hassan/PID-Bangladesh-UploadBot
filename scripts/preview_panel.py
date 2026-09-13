# preview_panel.py
# Runs the control panel locally against throwaway demo data, so the UI can be
# looked at without a Toolforge account.
#
#   python scripts/preview_panel.py
#   -> http://127.0.0.1:5173   (panel key: devkey)
#
# Everything it writes goes to a temp directory, so this never touches the real
# $TOOL_DATA_DIR, wayback queue, or credentials. The Jobs API is unreachable
# from a laptop, so the page exercises its degraded path: status falls back to
# the bot's own run records and the controls report that they can't act.

import datetime
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = tempfile.mkdtemp(prefix='pid-panel-preview-')
os.environ['TOOL_DATA_DIR'] = DATA
os.environ['TOOL_NAME'] = 'pid-bangladesh-uploadbot2'

import config  # noqa: E402  (must follow the env vars above)

PANEL_KEY = 'devkey'
UPLOAD_PATTERN = [0, 0, 3, 14, 9, 0, 21, 6, 11, 0, 2, 17]
FAILED_AT = {9, 34}


def seed_runs():
    now = datetime.datetime.now(datetime.timezone.utc)
    runs = []
    for hours_ago in range(71, 0, -1):
        started = now - datetime.timedelta(hours=hours_ago)
        uploaded = UPLOAD_PATTERN[hours_ago % len(UPLOAD_PATTERN)]
        failed = hours_ago in FAILED_AT
        runs.append({
            'started_at': started.isoformat(timespec='seconds'),
            'finished_at': (started + datetime.timedelta(minutes=4)).isoformat(timespec='seconds'),
            'status': 'failed' if failed else 'succeeded',
            'scraped': uploaded + 4,
            'uploaded': 0 if failed else uploaded,
            'duplicates': 2,
            'failed': 3 if failed else 0,
            'error': 'RuntimeError: All models failed — quota exhausted' if failed else '',
        })
    runs.append({
        'started_at': now.isoformat(timespec='seconds'), 'finished_at': None,
        'status': 'running', 'scraped': 18, 'uploaded': 7,
        'duplicates': 2, 'failed': 1, 'error': '',
    })
    pathlib.Path(config.RUN_STATE_PATH).write_text(
        json.dumps(runs), encoding='utf-8')


def seed_log():
    pathlib.Path(DATA, f'{config.JOB_NAME}.out').write_text("""\
============================================================
STEP 1: Scraping data from pressinform.gov.bd
============================================================
Loaded 4182 URLs from 2026, 3901 from 2025
Page 1: 7 new items added

STEP 2: Processing image...
Row 1: Downloading image...
Row 1: Finding separator...
Row 1: Performing OCR on text section (trimmed by 3px)...
Sanitized OCR Data: আজ ঢাকায় প্রধান উপদেষ্টার সভাকক্ষে অনুষ্ঠিত সভায় বক্তব্য রাখেন।
Row 1: Attempt 1 via AI Studio primary  (Free) (gemini-3.1-flash-lite)...
Row 1: Translation succeeded via AI Studio primary  (Free)
Row 1: Title generated (without extension): Chief Adviser speaks at a meeting in Dhaka 2026-09-13
Row 1: Upload successful
Row 2: Duplicate image detected via checksum — skipping OCR/AI
Row 3: Retryable error on AI Studio primary  (Free) (attempt 1): 429 RESOURCE_EXHAUSTED — retrying in 1.3s
Row 3: Translation succeeded via Vertex AI primary  (Paid)
Row 3: Upload successful
""", encoding='utf-8')


def seed_wayback():
    pathlib.Path(config.WAYBACK_QUEUE_PATH).write_text(json.dumps([
        'https://pressinform.gov.bd/pages/daily-photos/8821',
        'https://objectstorage.ap-dcc-gazipur-1.oraclecloud15.com/n/axvjbnqprylg/b/'
        'V2Ministry/o/office-pressinform/2026/09/2f9c4e1a.jpg',
    ]), encoding='utf-8')


def main():
    seed_runs()
    seed_log()
    seed_wayback()
    pathlib.Path(config.PANEL_KEY_PATH).write_text(PANEL_KEY, encoding='utf-8')

    from panel import app as panel_app
    print(f"\n  Panel preview   http://127.0.0.1:5173")
    print(f"  Panel key       {PANEL_KEY}")
    print(f"  Demo data       {DATA}\n")
    panel_app.app.run(host='127.0.0.1', port=5173,
                      debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
