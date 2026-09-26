# stats_view.py
# Turns the bot's daily totals and the upload registry into the series the
# Stats page draws, plus the one-sentence summary above each chart. Pure: no
# Flask, no network, so every number on the page is testable.

from datetime import date, timedelta

from src.stats import coerce_day

CAUSES = ('download', 'ocr', 'translation', 'title', 'upload', 'other')
RANGES = {'7d': 7, '30d': 30, '12m': 12}
CAUSE_LABELS = {'download': 'download', 'ocr': 'OCR', 'translation': 'translation',
                'title': 'title', 'upload': 'upload', 'other': 'other'}
PERIOD_WORDS = {'7d': 'the last 7 days', '30d': 'the last 30 days', '12m': 'the last 12 months'}


def _day(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _buckets(range_key, today):
    """(labels, bucket label for a day or None, is-in-previous-period test)."""
    if range_key == '12m':
        months, y, m = [], today.year, today.month
        for _ in range(12):
            months.append(f'{y:04d}-{m:02d}')
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        labels = months[::-1]
        first = labels[0]
        # The 11 months before the window: compared with its 11 complete ones.
        y, m = int(first[:4]), int(first[5:]) - 11
        while m < 1:
            y, m = y - 1, m + 12
        prev_first = f'{y:04d}-{m:02d}'
        return (labels,
                lambda d: d.strftime('%Y-%m') if d.strftime('%Y-%m') in labels else None,
                lambda d: prev_first <= d.strftime('%Y-%m') < first)
    n = RANGES[range_key]
    start = today - timedelta(days=n - 1)
    labels = [(start + timedelta(days=i)).isoformat() for i in range(n)]
    return (labels,
            lambda d: d.isoformat() if start <= d <= today else None,
            lambda d: start - timedelta(days=n - 1) <= d < start)


def build(daily, uploads, range_key, today):
    range_key = range_key if range_key in RANGES else '30d'
    labels, key, in_prev = _buckets(range_key, today)
    idx = {label: i for i, label in enumerate(labels)}
    zero = lambda: [0] * len(labels)
    words = PERIOD_WORDS[range_key]

    # Uploads, from the registry: only rows that name a file of our own.
    up, prev_total = zero(), 0
    for r in uploads:
        d = _day(r.get('date'))
        if not d or not r.get('filename'):
            continue
        k = key(d)
        if k is not None:
            up[idx[k]] += 1
        elif in_prev(d):
            prev_total += 1
    total = sum(up)
    # Today (or this month) is unfinished, so the comparison uses the complete
    # buckets only, against the same number just before them.
    complete = sum(up[:-1])
    if prev_total:
        diff = complete - prev_total
        pct = round(abs(diff) * 100 / prev_total)
        trend = (f', {pct}% {"more" if diff > 0 else "fewer"} than the period before'
                 if diff else ', the same as the period before')
    else:
        trend = ''
    uploads_summary = f'{total:,} uploaded in {words}{trend}.'

    # Daily totals from the bot.
    days = sorted(((d, coerce_day(v)) for d, v in ((_day(k), v) for k, v in daily.items()) if d),
                  key=lambda dv: dv[0])
    since = days[0][0].isoformat() if days else None
    failures = {c: zero() for c in CAUSES}
    free, paid, limit = zero(), zero(), zero()
    review, uncat = [None] * len(labels), [None] * len(labels)
    for d, v in days:
        k = key(d)
        if k is None:
            continue
        i = idx[k]
        for c in CAUSES:
            failures[c][i] += v['failed'][c]
        free[i] += v['gemini']['free']
        paid[i] += v['gemini']['paid']
        limit[i] += v['gemini']['free_limit']
        if v['backlog']:                           # days are sorted: the latest wins
            review[i], uncat[i] = v['backlog']['review'], v['backlog']['uncategorised']

    def collecting(sentence):
        if since is None:
            return 'No data yet: the first run after this update starts it.'
        return sentence + (f' Collecting since {since}.' if since > labels[0] else '')

    fail_total = sum(sum(v) for v in failures.values())
    top = max(CAUSES, key=lambda c: sum(failures[c]))
    fail_summary = collecting(
        f'{fail_total:,} failed in {words}; most were {CAUSE_LABELS[top]} ({sum(failures[top]):,}).'
        if fail_total else f'Nothing failed in {words}.')

    known = [x for x in review if x is not None]
    if known:
        change = known[-1] - known[0]
        moved = f' ({"up" if change > 0 else "down"} {abs(change):,} this period)' if change else ''
        backlog_summary = collecting(f'{known[-1]:,} photos still need the English checked{moved}.')
    else:
        backlog_summary = collecting('No backlog counts recorded in this period.')

    calls = sum(free) + sum(paid)
    gemini_summary = collecting(
        f'{round(sum(paid) * 100 / calls)}% of {calls:,} calls used the paid tier; '
        f'the free tier hit its limit {sum(limit):,} times.' if calls
        else f'No Gemini calls in {words}.')

    return {
        'range': range_key,
        'labels': labels,
        'uploads': {'values': up, 'summary': uploads_summary},
        'failures': {'series': failures, 'summary': fail_summary, 'since': since},
        'backlog': {'review': review, 'uncategorised': uncat,
                    'summary': backlog_summary, 'since': since},
        'gemini': {'free': free, 'paid': paid, 'free_limit': limit,
                   'summary': gemini_summary, 'since': since},
    }
