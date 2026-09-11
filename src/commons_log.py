# commons_log.py
# Writes per-run upload results to the bot's log page on Wikimedia Commons
# as a JSON file for easier tracking of failures and successes.

import json
from datetime import datetime

import pywikibot

from config import logger


def log_to_commons(site, rows=None, success_count=0, failed_count=0, total_rows=0):
    """Append processing results to the bot's monthly JSON log page on Wikimedia Commons."""
    try:
        current_date = datetime.now()
        month_name = current_date.strftime("%B")   # e.g. "June"
        year = current_date.strftime("%Y")

        page_title = f"User:PID-Bangladesh-UploadBot/Log/{month_name}_{year}.json"

        page = pywikibot.Page(site, page_title)

        run_data = {
            "timestamp": current_date.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "total_processed": total_rows,
            "success_count": success_count,
            "failed_count": failed_count,
            "items": rows or [],
        }

        existing_data = []
        if page.exists():
            try:
                existing_data = json.loads(page.text)
                if not isinstance(existing_data, list):
                    existing_data = []
            except json.JSONDecodeError:
                existing_data = []

        existing_data.append(run_data)

        # .json pages automatically render as JSON, so no syntaxhighlight is needed or allowed
        page.text = json.dumps(existing_data, indent=4, ensure_ascii=False)
        page.save(summary="Bot log update (JSON format)", bot=True)

        logger.info(f"Successfully logged to {page_title}")
        return True

    except Exception as e:
        logger.error(f"Error logging to Commons: {str(e)}")
        return False
