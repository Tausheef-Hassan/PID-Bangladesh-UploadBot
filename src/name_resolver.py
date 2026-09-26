# name_resolver.py
# Replaces people's names in Bengali OCR text with their English Wikidata
# label before translation, so Gemini never has to recall who a person is —
# left to itself it swaps in whoever it remembers holding the post.
#
# Reads the Toolforge Wikidata replica. Fails open: if the replica cannot be
# reached the text passes through unchanged and translation runs as before.

import os
import re
import threading
import unicodedata

import pymysql

from config import logger

MIN_WORDS, MAX_WORDS = 2, 5     # one-word labels collide with ordinary words
# Case endings Bengali glues onto the last word of a name (ইউনূসের, ইউনূসকে).
SUFFIXES = ('দের', 'এর', 'ের', 'কে', 'রা', 'র')
_TOKEN = re.compile(r'[^\s,;:।!?()"\'‘’“”/]+')

_local = threading.local()
_disabled_logged = False

_CANDIDATES_SQL = """
SELECT x.wbx_text, it.wbit_item_id
FROM wbt_text x
JOIN wbt_text_in_lang xl ON xl.wbxl_text_id = x.wbx_id AND xl.wbxl_language = 'bn'
JOIN wbt_term_in_lang tl ON tl.wbtl_text_in_lang_id = xl.wbxl_id
JOIN wbt_type ty ON ty.wby_id = tl.wbtl_type_id AND ty.wby_name IN ('label', 'alias')
JOIN wbt_item_terms it ON it.wbit_term_in_lang_id = tl.wbtl_id
JOIN page p ON p.page_namespace = 0 AND p.page_title = CONCAT('Q', it.wbit_item_id)
JOIN pagelinks pl ON pl.pl_from = p.page_id
JOIN linktarget lt ON lt.lt_id = pl.pl_target_id
                  AND lt.lt_namespace = 0 AND lt.lt_title = 'Q5'
WHERE x.wbx_text IN %s
"""
# The Q5 join is the "instance of: human" filter: the replica has no
# statements table, but every item pagelinks the entities its claims name.

_ENGLISH_SQL = """
SELECT it.wbit_item_id, x.wbx_text, pp.pp_value
FROM wbt_item_terms it
JOIN wbt_term_in_lang tl ON tl.wbtl_id = it.wbit_term_in_lang_id
JOIN wbt_type ty ON ty.wby_id = tl.wbtl_type_id AND ty.wby_name = 'label'
JOIN wbt_text_in_lang xl ON xl.wbxl_id = tl.wbtl_text_in_lang_id AND xl.wbxl_language = 'en'
JOIN wbt_text x ON x.wbx_id = xl.wbxl_text_id
LEFT JOIN page p ON p.page_namespace = 0 AND p.page_title = CONCAT('Q', it.wbit_item_id)
LEFT JOIN page_props pp ON pp.pp_page = p.page_id AND pp.pp_propname = 'wb-sitelinks'
WHERE it.wbit_item_id IN %s
"""


def _connection():
    """One replica connection per worker thread, revived if it dropped."""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        conn = pymysql.connect(
            host=os.environ.get('REPLICA_HOST', 'wikidatawiki.analytics.db.svc.wikimedia.cloud'),
            port=int(os.environ.get('REPLICA_PORT', 3306)),
            user=os.environ['TOOL_REPLICA_USER'],
            password=os.environ['TOOL_REPLICA_PASSWORD'],
            database='wikidatawiki_p',
            charset='utf8mb4',
            connect_timeout=min(10, getattr(_local, 'timeout', None) or 10),
            read_timeout=getattr(_local, 'timeout', None) or 60,
        )
        _local.conn = conn
    else:
        conn.ping(reconnect=True)
    return conn


def _text(v):
    return v.decode('utf-8') if isinstance(v, (bytes, bytearray)) else v


def _lookup(keys):
    """Query the replica. Returns rows of (bengali, qid, english, sitelinks)."""
    with _connection().cursor() as cur:
        cur.execute(_CANDIDATES_SQL, (tuple(keys),))
        found = [(_text(bn), int(item)) for bn, item in cur.fetchall()]
        if not found:
            return []
        cur.execute(_ENGLISH_SQL, (tuple({item for _, item in found}),))
        english = {int(item): (_text(en), int(_text(sl) or 0))
                   for item, en, sl in cur.fetchall()}
    return [(bn, f"Q{item}", *english[item]) for bn, item in found if item in english]


def _choose(rows):
    """{bengali: (english, qid)}. Several people behind one Bengali string:
    agree on the English → use it; otherwise the most-sitelinked wins."""
    by_bn = {}
    for bn, qid, en, sitelinks in rows:
        by_bn.setdefault(bn, []).append((sitelinks, en, qid))
    return {bn: max(c)[1:] for bn, c in by_bn.items()}


def _candidates(text):
    """{key: [(start, end)]} for every 2–5 word window, plus suffix-stripped
    variants whose span stops before the suffix. Windows never cross
    punctuation: the key must be exactly the text it spans."""
    tokens = [(m.start(), m.end()) for m in _TOKEN.finditer(text)]
    out = {}
    for i in range(len(tokens)):
        for n in range(MIN_WORDS, MAX_WORDS + 1):
            if i + n > len(tokens):
                break
            start, end = tokens[i][0], tokens[i + n - 1][1]
            words = [text[s:e] for s, e in tokens[i:i + n]]
            key = ' '.join(words)
            if text[start:end] != key:
                break               # punctuation or odd spacing inside
            out.setdefault(key, []).append((start, end))
            for suf in SUFFIXES:
                last = words[-1]
                if last.endswith(suf) and len(last) > len(suf):
                    stem = ' '.join(words[:-1] + [last[:-len(suf)]])
                    out.setdefault(stem, []).append((start, end - len(suf)))
    return out


def resolve_names(text, timeout=None):
    """Return (text with names replaced, [(bengali, english, qid), ...]).

    `timeout` (seconds) caps the replica connection for callers that can't
    wait, like the panel rendering a page; the bot takes the defaults.
    """
    global _disabled_logged
    if not text:
        return text, []
    text = unicodedata.normalize('NFC', text)   # Wikidata stores NFC
    cands = _candidates(text)
    if not cands:
        return text, []

    if 'TOOL_REPLICA_USER' not in os.environ:
        if not _disabled_logged:
            logger.warning("Wikidata name resolution off: no replica credentials in the environment")
            _disabled_logged = True
        return text, []
    _local.timeout = timeout
    try:
        names = _choose(_lookup(cands))
    except Exception as e:
        logger.warning(f"Wikidata name resolution skipped: {e}")
        _local.conn = None
        return text, []

    # Longest span first; drop anything overlapping a span already taken.
    spans = sorted(((s, e, bn) for bn in names for s, e in cands[bn]),
                   key=lambda t: (t[0] - t[1], t[0]))
    taken, matches = [], []
    for s, e, bn in spans:
        if all(e <= ts or s >= te for ts, te, _ in taken):
            taken.append((s, e, bn))
    for s, e, bn in sorted(taken, reverse=True):
        en, qid = names[bn]
        text = text[:s] + en + text[e:]
        matches.append((bn, en, qid))
    return text, matches[::-1]
