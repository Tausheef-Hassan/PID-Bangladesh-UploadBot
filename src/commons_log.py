# commons_log.py
# Writes per-run upload results to the bot's log page on Wikimedia Commons
# as a JSON file for easier tracking of failures and successes.

import json
from datetime import datetime

import pywikibot

from config import logger


# Rebuilt from the other fields at upload time, and by far the heaviest key —
# keeping it in the log is what pushes the page towards $wgMaxArticleSize.
_SKIP_FIELDS = ("wikitext_description",)


def log_to_commons(site, rows=None, success_count=0, failed_count=0, total_rows=0):
    """Append processing results to the bot's daily JSON log page on Wikimedia Commons.

    One page per day, not per month: every run rewrites the whole page, and a
    month of hourly runs overruns the 2 MB page limit — after which each save
    fails and the log silently stops updating.
    """
    if not rows and not total_rows:
        logger.info("Nothing to log to Commons this run.")
        return True

    try:
        current_date = datetime.now()
        page_title = f"User:PID-Bangladesh-UploadBot/Log/{current_date.strftime('%Y-%m-%d')}.json"

        page = pywikibot.Page(site, page_title)

        run_data = {
            "timestamp": current_date.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "total_processed": total_rows,
            "success_count": success_count,
            "failed_count": failed_count,
            "items": [
                {k: v for k, v in row.items() if k not in _SKIP_FIELDS}
                for row in (rows or [])
            ],
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
