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
from datetime import date

MARKER = '{{Auto-translated PID English description}}'

# Where a mild copyright flag files the page. Defined up here because the
# category editor has to know to leave it alone.
CONCERNS_CATEGORY = 'PID files with copyright concerns'

# The one category the box will not touch. A copyright flag is a verdict with
# its own control, so it must not come off as a side effect of tidying
# categories — but it is the *only* exception. Everything else written on the
# page is the editor's to change, including what the bot itself put there.
#
# The date and PID-BD categories need no rule: Module:PIDCategoryHelper adds
# them at render time, so they never appear in the wikitext for this to match.
PROTECTED_CATEGORY = re.compile('^' + re.escape(CONCERNS_CATEGORY), re.I)

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


def read_bengali(text):
    """The Bengali OCR text, or ''.

    Lenient where read_english is strict: this is the thing you check the
    translation against, so a page that has it should show it even when the
    English half is in a shape the editor cannot parse. No Bengali at all is a
    fact about the file, not an error.
    """
    span = _template_span(text or '', '{{bn|1=')
    if span is None:
        return ''
    start, end = span
    return text[start + len('{{bn|1='):end - 2].strip()


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
    """Editable categories on the page, in order.

    Everything written in the wikitext except the copyright flag — if it is on
    the page, a person put it there and a person can take it off.
    """
    return [name for name, _sort in CATEGORY_LINE.findall(text)
            if not PROTECTED_CATEGORY.match(name.strip())]


def write_categories(text, categories):
    """Replace the editable categories, leaving the copyright flag untouched.

    New ones are appended at the end, which is where Commons convention and
    the bot's own template already put them.
    """
    cleaned = []
    for raw in categories:
        name = raw.strip().lstrip('[').rstrip(']').strip()
        name = re.sub(r'^\s*Category\s*:\s*', '', name, flags=re.I).strip()
        if not name or PROTECTED_CATEGORY.match(name):
            continue
        if any(c in name for c in '[]{}|'):
            raise Unparseable(f'category name contains markup: {name!r}')
        if name not in cleaned:
            cleaned.append(name)

    # Drop the existing topic categories wherever they sit.
    def drop(match):
        return '' if not PROTECTED_CATEGORY.match(match.group(1).strip()) else match.group(0)

    stripped = CATEGORY_LINE.sub(drop, text).rstrip()
    if not cleaned:
        return stripped + '\n'
    return stripped + '\n' + '\n'.join(f'[[Category:{c}]]' for c in cleaned) + '\n'


SOURCE_URL = re.compile(r'\{\{\s*Source-PID\s*\|[^}]*?url\s*=\s*([^|}\s]+)', re.I)


def read_source_url(text):
    """The original PID url from {{Source-PID|url=...}}, or ''.

    Lets the side-by-side comparison work for any PID file on Commons, not just
    the ones this bot recorded in PIDDateData.
    """
    match = SOURCE_URL.search(text or '')
    return match.group(1).strip() if match else ''


# ── Copyright flags ───────────────────────────────────────────────────────────
#
# The same tags Commons' own QuickDelete gadget writes, in two tiers.
#
# Mild reasons only file the page into a review category: nothing enters an
# administrator's queue, so a misclick while working through a backlog of
# thousands costs one category edit. Severe reasons are real maintenance
# templates with real consequences, which is why the two that are judgement
# calls rather than facts refuse to be applied without a written reason.

# Spelled out rather than taken from strftime/calendar, both of which follow the
# process locale. Commons wants English month names whatever the server thinks.
MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July',
          'August', 'September', 'October', 'November', 'December')

# key -> (label, template, needs_note). An empty template means the mild tier.
REASONS = {
    'not-government': ('Not a Bangladesh government work', '', False),
    'derivative':     ('Photograph of copyrighted art, a logo or a screenshot', '', False),
    'unclear-source': ('Source unclear, needs checking', '', False),
    'no-permission':  ('No evidence of permission', 'No permission since', False),
    'no-license':     ('No licence tag', 'No license since', False),
    'no-source':      ('No source given', 'No source since', False),
    'wrong-license':  ('Wrong licence tag', 'Wrong license', True),
    'copyvio':        ('Clear copyright violation', 'Copyvio', True),
}

# Dated templates take month/day/year; the rest take the reason as parameter 1.
DATED = ('No permission since', 'No license since', 'No source since')

# The one flag that reaches an administrator within hours rather than days.
# The panel makes you type the filename before it will apply this.
NEEDS_CONFIRMATION = 'copyvio'


def is_mild(reason):
    return reason in REASONS and not REASONS[reason][1]


def read_copyright_tag(text):
    """Label of the copyright flag already on this page, or ''.

    A page can only carry one flag from this tool: the second one would stack
    templates and re-notify, so `tag_copyright` refuses when this returns
    anything. The mild tier is one shared category, so all three mild reasons
    read back as the same label — the reason itself lives in the edit summary.
    """
    for label, template, _needs in REASONS.values():
        if template and re.search(r'\{\{\s*%s\s*[|}]' % re.escape(template),
                                  text or '', re.I):
            return label
    if re.search(r'\[\[\s*Category\s*:\s*%s\s*[\]|]' % re.escape(CONCERNS_CATEGORY),
                 text or '', re.I):
        return 'Flagged for copyright review'
    return ''


def tag_copyright(text, reason, note='', today=None):
    """Return `text` with a copyright flag applied.

    Refuses rather than writing anything questionable to Commons: an unknown
    reason, an already-flagged page, a missing reason where one is required, or
    a note carrying markup that would break out of the template.
    """
    if reason not in REASONS:
        raise Unparseable(f'unknown reason: {reason!r}')

    already = read_copyright_tag(text)
    if already:
        raise Unparseable(f'already flagged: {already}')

    label, template, needs_note = REASONS[reason]
    note = (note or '').strip()
    if needs_note and not note:
        raise Unparseable(f'"{label}" needs a reason written down')
    if any(c in note for c in '{}|[]<>'):
        raise Unparseable('the reason may not contain wiki markup')

    # ponytail: the mild tier records its specific reason only in the edit
    # summary, not on the page. If reading history per file proves too slow,
    # upgrade path is a {{PID copyright concern|reason=|note=}} template, which
    # means creating and maintaining a template page on Commons.
    if not template:
        return text.rstrip() + f'\n[[Category:{CONCERNS_CATEGORY}]]\n'

    today = today or date.today()
    if template in DATED:
        tag = '{{%s|month=%s|day=%d|year=%d}}' % (
            template, MONTHS[today.month - 1], today.day, today.year)
    else:
        tag = '{{%s|1=%s}}' % (template, note)

    # Maintenance templates go at the very top, where Commons convention and the
    # gadget both put them, so the notice is the first thing a viewer sees.
    return tag + '\n' + text


def flag_summary(reason, note=''):
    """Edit summary for a flag, carrying the reason into the page history."""
    label = REASONS[reason][0]
    note = (note or '').strip()
    return ('Flagged via the PID control panel: %s%s'
            % (label, ' — ' + note if note else ''))
