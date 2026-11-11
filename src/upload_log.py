"""Durable local SHA-1 upload registry."""

import json
import os
from datetime import datetime

import config


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
        if record.get('sha1') == sha1 and record.get('status') in ('uploaded', 'commons_duplicate'):
            return record
    return None


# Statuses whose source URL is known to be on Commons already. title_conflict
# is left out: only the filename was taken, the image itself may be new.
_ON_COMMONS = ('uploaded', 'commons_duplicate', 'local_duplicate', 'imported')


def uploaded_urls():
    """Map each source URL already on Commons to its Commons filename."""
    try:
        with open(config.UPLOAD_LOG_PATH, encoding='utf-8') as log:
            records = [json.loads(line) for line in log if line.strip()]
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"could not read local upload log: {e}") from e
    urls = {}
    for rec in records:
        if rec.get('status') in _ON_COMMONS and rec.get('source_url'):
            urls[rec['source_url']] = rec.get('commons_filename') or rec.get('filename', '')
    return urls


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
