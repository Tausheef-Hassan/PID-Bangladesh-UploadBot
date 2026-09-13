# commons.py
# Read-only views of what the bot has published, for the control panel.
#
# Everything here comes from pages the bot already writes and that Commons
# serves publicly, so the panel needs no extra credentials:
#
#   PIDDateData/<year>.json   what was uploaded: source url, date, filename
#   Log/<YYYY-MM-DD>.json     per-image detail: Bengali OCR, translation, status
#
# Every fetch is cached for a couple of minutes. The dashboard polls every few
# seconds and none of this changes faster than the bot runs, so hitting Commons
# on each poll would be rude and pointless.

import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import quote

import config

BASE = 'https://commons.wikimedia.org'
USER = 'User:PID-Bangladesh-UploadBot'
CACHE_SECONDS = 120

session = config.http_session(retries=2)


def _bucket():
    """Cache key that rolls over every CACHE_SECONDS."""
    return int(time.time() // CACHE_SECONDS)


def thumb_url(filename, width=320):
    """Commons thumbnail for an uploaded file. Special:FilePath redirects to
    the real thumb host, so no API call is needed."""
    return f'{BASE}/wiki/Special:FilePath/{quote(filename)}?width={width}'


def file_page_url(filename):
    return f'{BASE}/wiki/File:{quote(filename)}'


def _fetch_json(title):
    try:
        r = session.get(f'{BASE}/w/index.php',
                        params={'title': title, 'action': 'raw'},
                        headers={'User-Agent': 'pid-bot-panel'}, timeout=10)
        if r.status_code != 200:
            return None
        return config.strip_syntaxhighlight(r.text)
    except Exception:
        return None


@lru_cache(maxsize=4)
def _uploads_raw(year, bucket):
    import json
    text = _fetch_json(f'{USER}/PIDDateData/{year}.json')
    if not text:
        return ()
    try:
        data = json.loads(text)
    except ValueError:
        return ()
    if isinstance(data, dict) and 'data' in data:   # legacy tabular rows
        data = [{'url': r[0] if len(r) > 0 else '',
                 'date': r[1] if len(r) > 1 else '',
                 'checksum': r[2] if len(r) > 2 else '',
                 'unique_id': r[3] if len(r) > 3 else '',
                 'filename': r[4] if len(r) > 4 else ''} for r in data['data']]
    if not isinstance(data, list):
        return ()
    # Tuples so lru_cache can hold them safely.
    return tuple(tuple(sorted(item.items())) for item in data if isinstance(item, dict))


def recent_uploads(limit=24):
    """Most recently registered uploads, newest first.

    Entries without a filename are checksum-duplicates registered to stop the
    bot retrying them — there is no Commons file of our own to show, so they
    are left out of the gallery.
    """
    year = datetime.now(timezone.utc).year
    rows = [dict(t) for t in _uploads_raw(year, _bucket())]
    if len(rows) < limit:
        rows = [dict(t) for t in _uploads_raw(year - 1, _bucket())] + rows
    named = [r for r in rows if r.get('filename')]
    return list(reversed(named[-limit:]))


@lru_cache(maxsize=8)
def _log_items(day, bucket):
    """Per-image rows the bot logged on one day, keyed by unique_id."""
    import json
    text = _fetch_json(f'{USER}/Log/{day}.json')
    if not text:
        return ()
    try:
        runs = json.loads(text)
    except ValueError:
        return ()
    items = {}
    for run in runs if isinstance(runs, list) else []:
        for item in run.get('items', []):
            if item.get('unique_id'):
                items[item['unique_id']] = item
    return tuple(sorted(items.items()))


def detail_for(unique_id, days_back=3):
    """Find the logged detail for one image: OCR text, translation, status."""
    today = datetime.now(timezone.utc).date()
    for offset in range(days_back):
        day = (today - timedelta(days=offset)).isoformat()
        for logged_id, item in _log_items(day, _bucket()):
            if logged_id == unique_id:
                return item
    return None


def find_upload(unique_id):
    for record in recent_uploads(limit=200):
        if record.get('unique_id') == unique_id:
            return record
    return None


# ── Browsing Commons itself ───────────────────────────────────────────────────
#
# The bot's own PIDDateData only knows what this bot uploaded recently. The
# category tree knows every PID image on Commons, including the backlog, so the
# work queues are built from Commons rather than from our own records.

PID_ROOT = 'Press Information Department images'
UNCATEGORISED = 'Press Information Department images without category'
REVIEW_SEARCH = 'hastemplate:"Auto-translated PID English description"'

MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
          'August', 'September', 'October', 'November', 'December')


def _api(**params):
    params.setdefault('format', 'json')
    params.setdefault('formatversion', 2)
    params['action'] = params.get('action', 'query')
    try:
        r = session.get(f'{BASE}/w/api.php', params=params, timeout=15,
                        headers={'User-Agent': 'pid-bot-panel'})
        return r.json()
    except Exception:
        return {}


@lru_cache(maxsize=64)
def category_page(title, cursor, bucket, limit=48):
    """One page of files in a category. Returns (titles, next_cursor)."""
    data = _api(list='categorymembers', cmtitle=f'Category:{title}',
                cmtype='file', cmlimit=limit, cmsort='timestamp', cmdir='desc',
                **({'cmcontinue': cursor} if cursor else {}))
    members = data.get('query', {}).get('categorymembers', [])
    return (tuple(m['title'] for m in members),
            data.get('continue', {}).get('cmcontinue', ''))


@lru_cache(maxsize=64)
def search_page(query, offset, bucket, limit=48):
    """One page of a CirrusSearch query over files. Returns (titles, total)."""
    data = _api(list='search', srsearch=query, srnamespace=6, srlimit=limit,
                sroffset=offset or 0, srinfo='totalhits', srprop='')
    result = data.get('query', {})
    return (tuple(r['title'] for r in result.get('search', [])),
            result.get('searchinfo', {}).get('totalhits', 0))


@lru_cache(maxsize=32)
def category_size(title, bucket):
    """How many files a category holds, for the queue counts."""
    pages = _api(prop='categoryinfo', titles=f'Category:{title}').get(
        'query', {}).get('pages', [{}])
    return pages[0].get('categoryinfo', {}).get('files', 0)


def month_categories(year):
    return [f'PID-BD images from {m} {year}' for m in MONTHS]


@lru_cache(maxsize=256)
def page_id(title, bucket):
    """Page id, which is also the MediaInfo entity id (M<pageid>)."""
    pages = _api(prop='info', titles=title).get('query', {}).get('pages', [{}])
    return pages[0].get('pageid')


@lru_cache(maxsize=256)
def caption(title, bucket, lang='en'):
    """The structured-data caption, or ''. PID files mostly have none."""
    pid = page_id(title, bucket)
    if not pid:
        return ''
    data = _api(action='wbgetentities', ids=f'M{pid}')
    entity = (data.get('entities') or {}).get(f'M{pid}', {})
    return (entity.get('labels', {}).get(lang, {}) or {}).get('value', '')
