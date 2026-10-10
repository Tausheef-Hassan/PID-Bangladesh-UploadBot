"""Durable local SHA-1 upload registry."""

import json
import os
from datetime import datetime

import config
from pywikibot import FilePage


def yearly_path(when=None):
    when = when or datetime.now()
    root, ext = os.path.splitext(config.UPLOAD_LOG_PATH)
    return f"{root}_{when.year}{ext}"


def find(sha1):
    """Return the most recent terminal record for this SHA-1, if any."""
    try:
        with open(config.UPLOAD_LOG_PATH, encoding='utf-8') as log:
            matches = [json.loads(line) for line in log if line.strip()]
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"could not read local upload log: {e}") from e
    for record in reversed(matches):
        if record.get('sha1') == sha1 and record.get('status') in (
                'uploaded', 'commons_duplicate', 'imported'):
            return record
    return None


def record(sha1, status, filename, source_url='', commons_filename=''):
    """Append the same SHA-1 event to the all-time and current-year logs."""
    entry = {
        'timestamp': datetime.now().isoformat(timespec='seconds'),
        'sha1': sha1,
        'status': status,
        'filename': filename,
        'source_url': source_url,
        'commons_filename': commons_filename,
    }
    line = json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n'
    for path in (config.UPLOAD_LOG_PATH, yearly_path()):
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'a', encoding='utf-8') as log:
            log.write(line)
    return entry


def import_commons_history(site):
    """Import SHA-1s from the bot's existing daily Commons JSON logs once."""
    try:
        with open(config.UPLOAD_LOG_PATH, encoding='utf-8') as log:
            if any(json.loads(line).get('status') == 'commons_import_complete'
                   for line in log if line.strip()):
                return 0
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"could not read local upload log: {e}") from e

    filenames = {}
    for page in site.allpages(namespace=2, prefix='PID-Bangladesh-UploadBot/Log/'):
        try:
            runs = json.loads(page.text)
        except (json.JSONDecodeError, OSError):
            continue
        for run in runs if isinstance(runs, list) else ():
            for item in run.get('items', ()) if isinstance(run, dict) else ():
                if item.get('upload_status') == 'Success' and item.get('filename'):
                    filenames[item['filename']] = item.get('image_url', '')

    imported = 0
    for filename, source_url in filenames.items():
        try:
            image_sha1 = FilePage(site, f'File:{filename}').latest_file_info.sha1
            if image_sha1 and not find(image_sha1):
                record(image_sha1, 'imported', filename, source_url, filename)
                imported += 1
        except Exception as e:
            config.logger.warning(f"Could not import SHA-1 for {filename}: {e}")

    record('', 'commons_import_complete', '', commons_filename=str(imported))
    return imported
