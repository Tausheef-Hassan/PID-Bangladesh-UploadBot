# uploader.py
# Pywikibot-based upload pipeline:
#   - Pywikibot initialisation and login
#   - Image upload to Wikimedia Commons
#   - Module:PIDDateData update
#   - Pre-upload category / module infrastructure check

import json
import os
import tempfile
import traceback
from datetime import datetime
from time import sleep

import cv2
import pywikibot
from PIL import Image
from pywikibot import FilePage
from pywikibot.exceptions import UploadError

from config import logger
import config


def initialize_pywikibot():
    """Initialise Pywikibot and log in to Wikimedia Commons.
    Returns (site, FilePage) on success, None on failure.
    """
    try:
        if not os.path.exists(config.USER_CONFIG_PATH):
            logger.error(f"Config file not found: {config.USER_CONFIG_PATH}")
            return None
        if not os.path.exists(config.PASSWORD_FILE_PATH):
            logger.error(f"Password file not found: {config.PASSWORD_FILE_PATH}")
            return None

        site = pywikibot.Site('commons', 'commons')
        site.login()

        logger.info("Successfully logged in to Wikimedia Commons")
        return site, FilePage

    except Exception as e:
        logger.error(f"Failed to initialize Pywikibot: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None


def upload_to_commons(site, FilePage, image, target_filename, img_format, exif_data, description, max_attempts=10):
    """Upload an OpenCV image to Wikimedia Commons.
    Returns (success: bool, error_message: str).
    """
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=f'.{img_format}')
    try:
        # Convert OpenCV BGR → PIL RGB, preserving EXIF
        img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        img_pil = Image.fromarray(img_rgb)

        save_kwargs = {}
        if exif_data:
            save_kwargs['exif'] = exif_data

        if img_format == 'png':
            img_pil.save(temp_file.name, 'PNG', optimize=True, **save_kwargs)
        elif img_format == 'jpg':
            img_pil.save(temp_file.name, 'JPEG', quality=95, **save_kwargs)
        else:
            img_pil.save(temp_file.name, **save_kwargs)

        for attempt in range(max_attempts):
            try:
                file_page = FilePage(site, f'File:{target_filename}')

                if file_page.exists():
                    logger.info(f"File already exists: {target_filename}")
                    return False, 'File already exists'

                logger.info(f"Uploading {target_filename} (attempt {attempt + 1}/{max_attempts})")

                success = file_page.upload(
                    source=temp_file.name,
                    comment="Pypan 0.1.1a0",
                    text=description,
                    ignore_warnings=True,
                )

                if success:
                    logger.info(f"Successfully uploaded {target_filename}")
                    return True, ''
                else:
                    logger.warning(f"Upload failed — server response for {target_filename}")

            except UploadError as e:
                logger.warning(f"Upload warning for {target_filename}: {str(e)}")

            except Exception as e:
                logger.error(f"Error uploading {target_filename}: {str(e)}")

            if attempt < max_attempts - 1:
                logger.info("Waiting 10 seconds before retry...")
                sleep(10)

        return False, 'Max attempts reached'

    finally:
        try:
            os.unlink(temp_file.name)
        except Exception:
            pass


def update_pid_date_data(site, url, date_str, checksum="", unique_id="", filename=""):
    """Append a single new entry to User:PID-Bangladesh-UploadBot/PIDDateData/{current_year}.json on Wikimedia Commons."""
    return batch_update_pid_date_data(site, [(url, date_str, checksum, unique_id, filename)])


def batch_update_pid_date_data(site, records):
    """Append multiple entries to User:PID-Bangladesh-UploadBot/PIDDateData/{current_year}.json on Wikimedia Commons."""
    if not records:
        return True

    try:
        current_year = datetime.now().year
        page_title = f"User:PID-Bangladesh-UploadBot/PIDDateData/{current_year}.json"
        page = pywikibot.Page(site, page_title)

        if not page.exists():
            logger.error(f"Page does not exist: {page_title}")
            return False

        try:
            content = page.text
            if content.startswith("<syntaxhighlight lang=\"json\">\n"):
                content = content.replace("<syntaxhighlight lang=\"json\">\n", "")
            if content.endswith("\n</syntaxhighlight>"):
                content = content.replace("\n</syntaxhighlight>", "")
            tab_data = json.loads(content)
            
            # Migrate Tabular Data format to Normal JSON format
            if isinstance(tab_data, dict) and 'data' in tab_data:
                new_list = []
                for row in tab_data['data']:
                    new_list.append({
                        "url": row[0] if len(row) > 0 else "",
                        "date": row[1] if len(row) > 1 else "",
                        "checksum": row[2] if len(row) > 2 else "",
                        "unique_id": row[3] if len(row) > 3 else "",
                        "filename": row[4] if len(row) > 4 else ""
                    })
                tab_data = new_list
            elif not isinstance(tab_data, list):
                tab_data = []
        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON in {page_title}")
            return False

        for url, date_str, checksum, unique_id, filename in records:
            tab_data.append({
                "url": url,
                "date": date_str,
                "checksum": checksum,
                "unique_id": unique_id,
                "filename": filename
            })

        json_str = json.dumps(tab_data, ensure_ascii=False, separators=(',', ':'))
        page.text = json_str
        page.save(summary=f"added {len(records)} images", bot=True)

        logger.info(f"Successfully batch updated {page_title} with {len(records)} records")
        return True

    except Exception as e:
        logger.error(f"Error batch updating PIDDateData: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False


def ensure_pid_infrastructure(site):
    """Ensure all required categories and User:PID-Bangladesh-UploadBot/PIDDateData/{year}.json exist for today."""
    now = datetime.now()
    year = now.year
    month = now.month

    y1 = str(year)[:3]                      # e.g. "202"
    y2 = str(year)[3:]                      # e.g. "6"
    month_padded = str(month).zfill(2)      # e.g. "03"
    month_name = now.strftime("%B")         # e.g. "March"
    date_str = now.strftime("%Y-%m-%d")     # e.g. "2026-03-09"

    pages_to_ensure = [
        (
            f"Category:Bangladesh photographs taken on {date_str}",
            "{{World photos}}"
        ),
        (
            f"Category:{month_name} {year} Bangladesh photographs",
            "{{Countryphotomonth}}"
        ),
        (
            f"Category:{month_name} {year} in Bangladesh",
            f"{{{{Monthbyyearbangladesh|{y1}|{y2}|{month}}}}}"
        ),
        (
            f"Category:{year} in Bangladesh",
            f"{{{{Bangladeshyear|{y1}|{y2}}}}}\n"
            f"{{{{Countries of Asia|prefix=:Category:{year} in }}}}}}\n"
            f"{{{{Wikidata Infobox}}}}"
        ),
        (
            f"Category:{month_name} {year} in Asia",
            f"{{{{Asiamonthyear|{year}|{month_name}}}}}\n{{{{Wikidata Infobox}}}}"
        ),
        (
            f"Category:{month_name} {year} by country",
            f"{{{{Monthbycountryyear|{y1}|{y2}|{month_padded}}}}}\n{{{{Wikidata Infobox}}}}"
        ),
        (
            f"Category:{year} photographs of Bangladesh",
            f"{{{{Bangladesh-photoyear|{y1}|{y2}}}}}"
        ),
        (
            f"Category:PID-BD images from {month_name} {year}",
            "{{PID-BD image category navigation}}"
        ),
        (
            f"Category:PID-BD images from {year}",
            f"[[Category:Press Information Department images|{year}]]\n"
            f"[[Category:{year} in Bangladesh]]"
        ),
    ]

    for title, content in pages_to_ensure:
        try:
            page = pywikibot.Page(site, title)
            if not page.exists():
                page.text = content
                page.save(summary="Creating category for PID uploads")
                logger.info(f"Created: {title}")
            else:
                logger.info(f"Already exists: {title}")
        except Exception as e:
            logger.error(f"Error creating {title}: {e}")

    # Ensure PIDDateData/{year}.json exists
    tab_title = f"User:PID-Bangladesh-UploadBot/PIDDateData/{year}.json"
    try:
        tab_page = pywikibot.Page(site, tab_title)
        if not tab_page.exists():
            tab_page.text = '[]'
            tab_page.save(summary="Creating Data schema for new year", bot=True)
            logger.info(f"Created: {tab_title}")
        else:
            logger.info(f"Already exists: {tab_title}")
    except Exception as e:
        logger.error(f"Error creating {tab_title}: {e}")
