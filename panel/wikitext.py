# wikitext.py
# Reading and rewriting the file description pages the bot uploads.
#
# The shape main.py produces:
#
#   {{bn|1=<Bengali OCR>}}{{en|1=<translation>{{Auto-translated PID English description}}}}
#
# The marker sits *inside* the en block, and either block can contain nested
# templates, so this counts braces rather than pattern-matching. Every function
# here refuses on anything it does not recognise: returning None and leaving the
# page alone is always better than writing a mangled description to Commons.

import re

MARKER = '{{Auto-translated PID English description}}'

# Added by the templates, not by hand: Date-PID and PD-BDGov-PID call
# Module:PIDCategoryHelper and file these themselves. Showing them as editable
# would invite someone to duplicate what is already automatic.
AUTOMATIC_CATEGORY = re.compile(
    r'^(Uploaded with pypan|PID-BD images from |Bangladesh photographs taken on |'
    r'Historic images from PID-BD)', re.I)

CATEGORY_LINE = re.compile(r'\[\[\s*Category\s*:\s*([^\]|]+?)\s*(\|[^\]]*)?\]\]', re.I)


class Unparseable(Exception):
    """The page is not in the shape the bot writes. Do not touch it."""


def _template_span(text, opening):
    """Span of a template starting with `opening`, brace-counted.

    Returns (start, end) with end just past the closing braces, or None.
    """
    start = text.find(opening)
    if start == -1:
        return None

    depth = 0
    i = start
    while i < len(text):
        if text.startswith('{{', i):
            depth += 1
            i += 2
        elif text.startswith('}}', i):
            depth -= 1
            i += 2
            if depth == 0:
                return start, i
        else:
            i += 1
    return None   # unbalanced; caller treats this as unparseable


def read_english(text):
    """Return (english_text, is_marked_auto_translated).

    The marker is stripped from the returned text — it is a flag, not prose.
    """
    span = _template_span(text, '{{en|1=')
    if span is None:
        raise Unparseable('no {{en|1=...}} block')
    start, end = span
    inner = text[start + len('{{en|1='):end - 2]

    marked = MARKER in inner
    return inner.replace(MARKER, '').strip(), marked


def write_english(text, english, mark_auto_translated):
    """Replace the English description, and set or clear the marker."""
    span = _template_span(text, '{{en|1=')
    if span is None:
        raise Unparseable('no {{en|1=...}} block')
    start, end = span

    english = english.strip()
    if not english:
        raise Unparseable('refusing to write an empty description')
    if '{{' in english or '}}' in english:
        # A stray brace would break the surrounding template and, worse, could
        # transclude something the editor did not intend.
        raise Unparseable('description may not contain template braces')

    body = english + (MARKER if mark_auto_translated else '')
    return text[:start] + '{{en|1=' + body + '}}' + text[end:]


def read_categories(text):
    """Topic categories on the page, in order, excluding the automatic ones."""
    return [name for name, _sort in CATEGORY_LINE.findall(text)
            if not AUTOMATIC_CATEGORY.match(name.strip())]


def write_categories(text, categories):
    """Replace the topic categories, leaving automatic ones untouched.

    New ones are appended at the end, which is where Commons convention and
    the bot's own template already put them.
    """
    cleaned = []
    for raw in categories:
        name = raw.strip().lstrip('[').rstrip(']').strip()
        name = re.sub(r'^\s*Category\s*:\s*', '', name, flags=re.I).strip()
        if not name or AUTOMATIC_CATEGORY.match(name):
            continue
        if any(c in name for c in '[]{}|'):
            raise Unparseable(f'category name contains markup: {name!r}')
        if name not in cleaned:
            cleaned.append(name)

    # Drop the existing topic categories wherever they sit.
    def drop(match):
        return '' if not AUTOMATIC_CATEGORY.match(match.group(1).strip()) else match.group(0)

    stripped = CATEGORY_LINE.sub(drop, text).rstrip()
    if not cleaned:
        return stripped + '\n'
    return stripped + '\n' + '\n'.join(f'[[Category:{c}]]' for c in cleaned) + '\n'
