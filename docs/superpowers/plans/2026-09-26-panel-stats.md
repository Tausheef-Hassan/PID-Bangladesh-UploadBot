# Panel part 3 — Stats and history Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Stats tab with four charts (uploads, failures by cause, review backlog, Gemini free vs paid), fed by a daily totals file the bot writes and by the existing upload registry.

**Architecture:** The bot gains `src/stats.py`, which classifies failures, records a run into `stats_daily.json` atomically, and fetches the backlog counts. `translator` counts Gemini acceptances and free-tier 429s. The panel gains `panel/stats_view.py`, which turns the daily file plus upload rows into chart series without Flask, and a `/stats` route and template drawn with a vendored Chart.js.

**Tech Stack:** Python 3, Flask/Jinja, Chart.js 4 (vendored), Codex tokens, `test_pipeline.py`.

**Spec:** `docs/superpowers/specs/2026-09-26-panel-stats-design.md`

## Global Constraints

- Nothing loads from outside Toolforge: Chart.js is copied into `panel/static/chartjs/`.
- `stats_daily.json` lives at `config.STATS_PATH = os.path.join(CREDS_DIR, 'stats_daily.json')`.
- A failed stats write or backlog fetch must never fail a bot run (log and continue).
- Failure causes, in this order: `download, ocr, translation, title, upload, other`.
- Ranges: `7d`, `30d` (default), `12m`. Daily buckets for 7d/30d, monthly for 12m. Dates are UTC.
- Tab order: Overview · Uploads · Stats · Name corrections · Queue · Access.
- Chart colours: the categorical palette from the dataviz skill's `references/palette.md`, in its listed order, one per failure cause. Uploads/free use the first colour, paid the second.

## Review Focus

- **Corrupt or hand-edited `stats_daily.json`** (not JSON, or a day entry missing keys) → the bot overwrites it cleanly and the page renders with what it can read. (Tests in Tasks 1 and 2.)
- **A range with zero data** → "No data yet" text, never a JS error or an empty canvas. (Test in Task 2.)
- **Uploads in PIDDateData with malformed dates** → skipped, not a 500. (Test in Task 2.)
- **Gemini counters with concurrent worker threads** → counts stay exact (lock). (Test in Task 1.)
- **The 12m range spanning a year boundary** → months in order, both years' registries read. (Test in Task 2.)

---

### Task 1: The bot records daily totals

**Files:**
- Create: `src/stats.py`
- Modify: `config.py` (add `STATS_PATH` next to `RUN_STATE_PATH`), `src/translator.py` (`_gemini_text`, new `usage`/`reset_usage`), `main.py` (reset at start, record at end)
- Test: `test_pipeline.py`

**Interfaces:**
- Produces:
  - `stats.failure_cause(row: dict) -> str | None`
  - `stats.record_run(uploaded: int, duplicates: int, failed: dict, gemini: dict, backlog: dict | None, path=None, today=None) -> None`
  - `stats.load(path=None) -> dict`
  - `stats.fetch_backlog() -> dict | None`
  - `translator.usage() -> dict(free, paid, free_limit)` and `translator.reset_usage()`

- [ ] **Step 1: Write the failing tests** (before `if __name__ == "__main__":`)

```python
# ── Stats ─────────────────────────────────────────────────────────────────────

def test_failures_are_sorted_into_causes():
    from src.stats import failure_cause
    ok = {"upload_status": "Success"}
    assert failure_cause(ok) is None
    assert failure_cause({"upload_status": "Skipped (checksum duplicate)"}) is None
    assert failure_cause({"ocr_status": "No URL"}) is None
    assert failure_cause({"ocr_status": "404 error - no snapshot"}) == "download"
    assert failure_cause({"ocr_status": "Download failed: timeout"}) == "download"
    assert failure_cause({"ocr_status": "OCR failed"}) == "ocr"
    assert failure_cause({"ocr_status": "No text detected"}) == "ocr"
    assert failure_cause({"ocr_status": "Success", "translation_status": "Error: all models failed"}) == "translation"
    assert failure_cause({"ocr_status": "Success", "translation_status": "Success",
                          "filename_status": "Error: x"}) == "title"
    assert failure_cause({"ocr_status": "Success", "translation_status": "Success",
                          "filename_status": "Success", "upload_status": "Failed: abuse filter"}) == "upload"
    assert failure_cause({"ocr_status": "Success", "upload_status": "Exception: boom"}) == "other"


def test_runs_add_up_per_day_and_backlog_is_the_latest():
    from src import stats
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "s.json")
        with open(p, "w") as f:
            f.write("not json")
        stats.record_run(3, 1, {"ocr": 1}, {"free": 5, "paid": 1, "free_limit": 2},
                         {"review": 100, "uncategorised": 50}, path=p, today="2026-09-26")
        stats.record_run(2, 0, {"ocr": 1, "upload": 1}, {"free": 4, "paid": 0, "free_limit": 0},
                         {"review": 98, "uncategorised": 51}, path=p, today="2026-09-26")
        stats.record_run(1, 0, {}, {"free": 1, "paid": 0, "free_limit": 0}, None,
                         path=p, today="2026-09-26")
        day = stats.load(p)["2026-09-26"]
        assert day["runs"] == 3 and day["uploaded"] == 6 and day["duplicates"] == 1
        assert day["failed"]["ocr"] == 2 and day["failed"]["upload"] == 1 and day["failed"]["title"] == 0
        assert day["gemini"] == {"free": 10, "paid": 1, "free_limit": 2}
        assert day["backlog"] == {"review": 98, "uncategorised": 51}, "a failed fetch erased the backlog"
        assert not os.path.exists(p + ".tmp")


def test_gemini_usage_counts_free_paid_and_free_limit_hits():
    translator.sleep = lambda s: None
    translator.reset_usage()

    class Resp:
        text = "ok"

    class Models:
        def __init__(self, fail_first_with=None):
            self.fail = fail_first_with
        def generate_content(self, **kw):
            if self.fail:
                e, self.fail = self.fail, None
                raise e
            return Resp()

    class Client:
        def __init__(self, fail=None):
            self.models = Models(fail)

    translator._gemini_text(Client(RuntimeError("429 RESOURCE_EXHAUSTED")), Client(), "p", 10, 1,
                            lambda t: t)
    translator._gemini_text(Client(RuntimeError("400 bad request")), Client(), "p", 10, 2,
                            lambda t: t)
    assert translator.usage() == {"free": 1, "paid": 1, "free_limit": 1}, translator.usage()
    translator.reset_usage()
    assert translator.usage() == {"free": 0, "paid": 0, "free_limit": 0}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -c "import test_pipeline as t; t.test_failures_are_sorted_into_causes()"`
Expected: `ModuleNotFoundError: No module named 'src.stats'`

- [ ] **Step 3: Create `src/stats.py`**

```python
# stats.py
# Daily totals for the panel's Stats page: what each run uploaded, why photos
# failed, how the Gemini tiers were used, and how big the review backlog is.
# Written by the bot at the end of every run into $TOOL_DATA_DIR, because the
# panel's other sources (run_state's last 200 runs, the Commons logs) either
# forget or never recorded these.

import json
import os
from datetime import datetime, timezone

import config
from config import logger

CAUSES = ('download', 'ocr', 'translation', 'title', 'upload', 'other')
COMMONS_API = 'https://commons.wikimedia.org/w/api.php'
REVIEW_SEARCH = 'hastemplate:"Auto-translated PID English description"'
UNCATEGORISED = 'Category:Press Information Department images without category'


def failure_cause(row):
    """Which step failed this row, or None if it didn't fail."""
    ocr = row.get('ocr_status', '') or ''
    upload = row.get('upload_status', '') or ''
    if upload == 'Success' or upload.startswith('Skipped') or ocr == 'No URL':
        return None
    low = ocr.lower()
    if '404' in low or 'download' in low or 'image processing error' in low:
        return 'download'
    if ocr.startswith('OCR failed') or ocr == 'No text detected':
        return 'ocr'
    if (row.get('translation_status') or '').startswith('Error'):
        return 'translation'
    if (row.get('filename_status') or '').startswith('Error'):
        return 'title'
    if upload.startswith('Failed'):
        return 'upload'
    return 'other'


def load(path=None):
    try:
        with open(path or config.STATS_PATH, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _empty_day():
    return {'runs': 0, 'uploaded': 0, 'duplicates': 0,
            'failed': {c: 0 for c in CAUSES},
            'gemini': {'free': 0, 'paid': 0, 'free_limit': 0},
            'backlog': None}


def record_run(uploaded, duplicates, failed, gemini, backlog, path=None, today=None):
    """Add one run to today's totals. Never raises: stats must not fail a run."""
    path = path or config.STATS_PATH
    today = today or datetime.now(timezone.utc).date().isoformat()
    try:
        data = load(path)
        day = _empty_day()
        old = data.get(today)
        if isinstance(old, dict):
            day.update({k: v for k, v in old.items() if k in day})
            day['failed'] = {c: int((old.get('failed') or {}).get(c, 0)) for c in CAUSES}
            day['gemini'] = {k: int((old.get('gemini') or {}).get(k, 0)) for k in day['gemini']}
        day['runs'] += 1
        day['uploaded'] += uploaded
        day['duplicates'] += duplicates
        for c, n in (failed or {}).items():
            day['failed'][c if c in CAUSES else 'other'] += n
        for k in day['gemini']:
            day['gemini'][k] += (gemini or {}).get(k, 0)
        if backlog:
            day['backlog'] = backlog
        data[today] = day
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"Could not record daily stats: {e}")


def fetch_backlog():
    """{'review': n, 'uncategorised': n} from Commons, or None if it can't say."""
    try:
        s = config.http_session(retries=2)
        headers = {'User-Agent': 'PID-Bangladesh-UploadBot stats'}
        review = s.get(COMMONS_API, timeout=15, headers=headers, params={
            'action': 'query', 'list': 'search', 'srsearch': REVIEW_SEARCH,
            'srnamespace': 6, 'srlimit': 1, 'srinfo': 'totalhits', 'format': 'json',
            'formatversion': 2}).json()['query']['searchinfo']['totalhits']
        pages = s.get(COMMONS_API, timeout=15, headers=headers, params={
            'action': 'query', 'prop': 'categoryinfo', 'titles': UNCATEGORISED,
            'format': 'json', 'formatversion': 2}).json()['query']['pages']
        return {'review': int(review),
                'uncategorised': int(pages[0].get('categoryinfo', {}).get('files', 0))}
    except Exception as e:
        logger.warning(f"Could not fetch review backlog: {e}")
        return None
```

- [ ] **Step 4: `config.py`** — below `RUN_STATE_PATH` add:

```python
STATS_PATH = os.path.join(CREDS_DIR, 'stats_daily.json')      # Daily totals, read by the panel's Stats page
```

- [ ] **Step 5: `src/translator.py`** — add `import threading` to the imports, and above `_gemini_text`:

```python
# Which tier answered, and how often the free one said "slow down" — counted
# for the panel's Stats page. Workers translate in parallel, hence the lock.
_usage_lock = threading.Lock()
_usage = {'free': 0, 'paid': 0, 'free_limit': 0}


def _count(key):
    with _usage_lock:
        _usage[key] += 1


def usage():
    with _usage_lock:
        return dict(_usage)


def reset_usage():
    with _usage_lock:
        for k in _usage:
            _usage[k] = 0
```

In `_gemini_text`, replace `return accept(text), source_name` with:

```python
                value = accept(text)
                _count('free' if client is genai_client else 'paid')
                return value, source_name
```

and in the `except` block, directly after `retryable = …` is computed, add:

```python
                if client is genai_client and '429' in str(e):
                    _count('free_limit')
```

- [ ] **Step 6: `main.py`** — add `from src import stats` and `from src.translator import reset_usage, usage` (extend the existing translator import). Right after `with run_state.record_run() as run:` add `reset_usage()`. After the `run["duplicates"] = …` statement add:

```python
        failed_by_cause = {}
        for r in rows:
            cause = stats.failure_cause(r)
            if cause:
                failed_by_cause[cause] = failed_by_cause.get(cause, 0) + 1
        stats.record_run(success_count - run["duplicates"], run["duplicates"],
                         failed_by_cause, usage(), stats.fetch_backlog())
```

(Note: `success_count` includes checksum duplicates, so uploads are `success_count - duplicates`.)

- [ ] **Step 7: Run the suite**

Run: `python test_pipeline.py 2>&1 | tail -1`
Expected: `N checks passed`

- [ ] **Step 8: Commit**

```bash
git add src/stats.py config.py src/translator.py main.py test_pipeline.py
git commit -m "Record daily totals for the Stats page: uploads, failure causes, Gemini tiers, backlog"
```

---

### Task 2: Chart series from the daily file and the upload registry

**Files:**
- Create: `panel/stats_view.py`
- Test: `test_pipeline.py`

**Interfaces:**
- Consumes: the `stats_daily.json` shape from Task 1; upload rows with `date` (`YYYY-MM-DD…`) and `filename`.
- Produces: `stats_view.build(daily: dict, uploads: list[dict], range_key: str, today: date) -> dict` with keys `labels` (list[str]), `uploads` {`values`, `summary`}, `failures` {`series`: {cause: values}, `summary`, `since`}, `backlog` {`review`, `uncategorised`, `summary`, `since`}, `gemini` {`free`, `paid`, `free_limit`, `summary`, `since`}, `range` (str). `since` is `None` when there is no data at all, or the ISO date of the first recorded day. Values are ints, or `None` for backlog days with no snapshot.

- [ ] **Step 1: Write the failing tests**

```python
def test_stats_buckets_days_and_compares_with_the_period_before():
    from datetime import date
    from panel import stats_view
    uploads = [{"date": "2026-09-25 10:00", "filename": "a.jpg"},
               {"date": "2026-09-26", "filename": "b.jpg"},
               {"date": "2026-09-26", "filename": ""},          # duplicate registration
               {"date": "garbage", "filename": "c.jpg"},        # malformed: skipped
               {"date": "2026-09-18", "filename": "d.jpg"}]     # previous 7-day period
    v = stats_view.build({}, uploads, "7d", date(2026, 9, 26))
    assert v["labels"][-1] == "2026-09-26" and len(v["labels"]) == 7
    assert v["uploads"]["values"][-2:] == [1, 1]
    assert "2 uploaded in the last 7 days" in v["uploads"]["summary"]
    assert "1 more than" in v["uploads"]["summary"] or "100%" in v["uploads"]["summary"], v["uploads"]["summary"]
    assert v["failures"]["since"] is None and "No data yet" in v["failures"]["summary"]


def test_stats_monthly_range_crosses_the_year_and_reads_the_daily_file():
    from datetime import date
    from panel import stats_view
    daily = {"2026-01-03": {"runs": 1, "uploaded": 2, "duplicates": 0,
                            "failed": {"ocr": 2, "translation": 1},
                            "gemini": {"free": 9, "paid": 1, "free_limit": 1},
                            "backlog": {"review": 500, "uncategorised": 40}},
             "2025-12-30": {"runs": 1},                                  # missing keys
             "not-a-date": {"runs": 1}}
    v = stats_view.build(daily, [], "12m", date(2026, 1, 15))
    assert v["labels"][0] == "2025-02" and v["labels"][-1] == "2026-01" and len(v["labels"]) == 12
    assert v["failures"]["series"]["ocr"][-1] == 2 and v["failures"]["series"]["upload"][-1] == 0
    assert v["gemini"]["paid"][-1] == 1 and v["gemini"]["free"][-1] == 9
    assert v["backlog"]["review"][-1] == 500 and v["backlog"]["review"][0] is None
    assert v["failures"]["since"] == "2025-12-30"
    assert "10%" in v["gemini"]["summary"], v["gemini"]["summary"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -c "import test_pipeline as t; t.test_stats_buckets_days_and_compares_with_the_period_before()"`
Expected: `ImportError: cannot import name 'stats_view'`

- [ ] **Step 3: Create `panel/stats_view.py`**

```python
# stats_view.py
# Turns the bot's daily totals and the upload registry into the series the
# Stats page draws, plus the one-sentence summary above each chart. Pure: no
# Flask, no network, so every number on the page is testable.

from datetime import date, timedelta

CAUSES = ('download', 'ocr', 'translation', 'title', 'upload', 'other')
RANGES = {'7d': 7, '30d': 30, '12m': 12}
CAUSE_LABELS = {'download': 'download', 'ocr': 'OCR', 'translation': 'translation',
                'title': 'title', 'upload': 'upload', 'other': 'other'}


def _day(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _buckets(range_key, today):
    """(labels, key(day) -> label or None, previous-period key)."""
    if range_key == '12m':
        months = []
        y, m = today.year, today.month
        for _ in range(12):
            months.append(f'{y:04d}-{m:02d}')
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        labels = months[::-1]
        first = labels[0]
        py, pm = int(first[:4]) - 1, int(first[5:])
        prev_first = f'{py:04d}-{pm:02d}'
        key = lambda d: d.strftime('%Y-%m') if d.strftime('%Y-%m') in labels else None
        prev = lambda d: prev_first <= d.strftime('%Y-%m') < first
        return labels, key, prev
    n = RANGES.get(range_key, 30)
    start = today - timedelta(days=n - 1)
    labels = [(start + timedelta(days=i)).isoformat() for i in range(n)]
    key = lambda d: d.isoformat() if start <= d <= today else None
    prev = lambda d: start - timedelta(days=n) <= d < start
    return labels, key, prev


def _period_words(range_key):
    return {'7d': 'the last 7 days', '30d': 'the last 30 days', '12m': 'the last 12 months'}.get(
        range_key, 'the last 30 days')


def build(daily, uploads, range_key, today):
    range_key = range_key if range_key in RANGES else '30d'
    labels, key, in_prev = _buckets(range_key, today)
    idx = {l: i for i, l in enumerate(labels)}
    zero = lambda: [0] * len(labels)
    words = _period_words(range_key)

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
    if prev_total:
        diff = total - prev_total
        pct = round(abs(diff) * 100 / prev_total)
        trend = (f', {pct}% {"more" if diff > 0 else "fewer"} than the period before'
                 if diff else ', the same as the period before')
    else:
        trend = f', {total} more than the period before' if total else ''
    uploads_summary = f'{total:,} uploaded in {words}{trend}.'

    # Daily totals from the bot.
    days = sorted((d, v) for d, v in ((_day(k), v) for k, v in daily.items())
                  if d and isinstance(v, dict))
    since = days[0][0].isoformat() if days else None
    failures = {c: zero() for c in CAUSES}
    free, paid, limit = zero(), zero(), zero()
    review = [None] * len(labels)
    uncat = [None] * len(labels)
    for d, v in days:
        k = key(d)
        if k is None:
            continue
        i = idx[k]
        for c in CAUSES:
            failures[c][i] += int((v.get('failed') or {}).get(c, 0) or 0)
        g = v.get('gemini') or {}
        free[i] += int(g.get('free', 0) or 0)
        paid[i] += int(g.get('paid', 0) or 0)
        limit[i] += int(g.get('free_limit', 0) or 0)
        b = v.get('backlog')
        if isinstance(b, dict):                    # later days overwrite: latest wins
            review[i] = b.get('review')
            uncat[i] = b.get('uncategorised')

    def collecting(sentence):
        if since is None:
            return 'No data yet: the first run after this update starts it.'
        return sentence + (f' Collecting since {since}.' if since > labels[0][:10] else '')

    fail_total = sum(sum(v) for v in failures.values())
    top = max(CAUSES, key=lambda c: sum(failures[c]))
    fail_summary = collecting(
        f'{fail_total:,} failed in {words}; most were {CAUSE_LABELS[top]} ({sum(failures[top]):,}).'
        if fail_total else f'Nothing failed in {words}.')

    known = [x for x in review if x is not None]
    if known:
        change = known[-1] - known[0]
        moved = (f' ({"up" if change > 0 else "down"} {abs(change):,} this period)' if change else '')
        backlog_summary = collecting(f'{known[-1]:,} photos still need the English checked{moved}.')
    else:
        backlog_summary = collecting('No backlog counts recorded in this period.')

    calls = sum(free) + sum(paid)
    gemini_summary = collecting(
        f'{round(sum(paid) * 100 / calls)}% of {calls:,} calls used the paid tier; '
        f'the free tier hit its limit {sum(limit):,} times.' if calls else f'No Gemini calls in {words}.')

    return {
        'range': range_key,
        'labels': labels,
        'uploads': {'values': up, 'summary': uploads_summary},
        'failures': {'series': failures, 'summary': fail_summary, 'since': since},
        'backlog': {'review': review, 'uncategorised': uncat, 'summary': backlog_summary, 'since': since},
        'gemini': {'free': free, 'paid': paid, 'free_limit': limit, 'summary': gemini_summary, 'since': since},
    }
```

- [ ] **Step 4: Run the suite**

Run: `python test_pipeline.py 2>&1 | tail -1`
Expected: `N checks passed`

- [ ] **Step 5: Commit**

```bash
git add panel/stats_view.py test_pipeline.py
git commit -m "Turn daily totals and the upload registry into the Stats page's series"
```

---

### Task 3: The Stats page

**Files:**
- Create: `panel/static/chartjs/chart.umd.min.js`, `panel/static/chartjs/VERSION`, `panel/templates/stats.html`
- Modify: `panel/app.py` (import `stats_view`, add `/stats` route), `panel/templates/base.html` (tab), `panel/static/panel.css`
- Test: `test_pipeline.py`

**Interfaces:**
- Consumes: `stats_view.build(...)`, `src.stats.load()`, `commons._uploads_raw(year, bucket)`.

- [ ] **Step 1: Write the failing test**

```python
def test_stats_page_renders_with_its_data_and_no_external_scripts():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp)
        config.STATS_PATH = os.path.join(tmp, "stats_daily.json")
        real = panel_app.commons._uploads_raw
        panel_app.commons._uploads_raw = lambda year, bucket: (
            (("date", "2026-09-26"), ("filename", "a.jpg")),)
        try:
            body = client.get("/stats?range=7d").get_data(as_text=True)
        finally:
            panel_app.commons._uploads_raw = real
        assert 'id="statsdata"' in body and 'chartjs/chart.umd.min.js' in body
        for heading in ("Uploads", "Failures", "Review backlog", "Gemini"):
            assert heading in body, heading
        assert "No data yet" in body
        assert 'src="http' not in body, "external script"
        assert ">Stats</a>" in body
```

Note: `_uploads_raw` returns tuples of item-tuples (it is `lru_cache`d, so rows are frozen). The route turns them into dicts with `dict(t)`, as `recent_uploads` does.

- [ ] **Step 2: Run to verify it fails**

Run: `python -c "import test_pipeline as t; t.test_stats_page_renders_with_its_data_and_no_external_scripts()"`
Expected: 404 → AssertionError

- [ ] **Step 3: Vendor Chart.js** (via `ctx_execute` or a Python script, not a heredoc):

```python
import json, urllib.request, os
root = r"D:\WikiTools\PID\pid-bangladesh-bot\panel\static\chartjs"
os.makedirs(root, exist_ok=True)
V = json.load(urllib.request.urlopen(
    "https://data.jsdelivr.com/v1/packages/npm/chart.js/resolved?specifier=4"))["version"]
data = urllib.request.urlopen(f"https://cdn.jsdelivr.net/npm/chart.js@{V}/dist/chart.umd.min.js").read()
open(os.path.join(root, "chart.umd.min.js"), "wb").write(data)
open(os.path.join(root, "VERSION"), "w").write(
    f"Copied from npm, served locally (Toolforge tools must not load third-party URLs).\nchart.js {V}\n")
```

Then check that it holds no `https://` script loads: `grep -c "sourceMappingURL" panel/static/chartjs/chart.umd.min.js` (a source-map comment is fine; strip it if present so the browser doesn't request it).

- [ ] **Step 4: Route in `panel/app.py`** — extend `from panel import …` with `stats_view`, add `from src import stats as bot_stats`, and add below the uploads routes:

```python
# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get('/stats')
def stats_page():
    range_key = request.args.get('range', '30d')
    today = datetime.now(timezone.utc).date()
    bucket = commons._bucket()
    rows = []
    for year in {today.year, today.year - 1}:
        rows += [dict(t) for t in commons._uploads_raw(year, bucket)]
    view = stats_view.build(bot_stats.load(), rows, range_key, today)
    return render_template('stats.html', view=view, ranges=[
        ('7d', '7 days'), ('30d', '30 days'), ('12m', '12 months')])
```

- [ ] **Step 5: Tab in `base.html`** — insert `('stats', 'Stats', []),` after the Uploads entry in the tab list.

- [ ] **Step 6: Create `panel/templates/stats.html`.** Take the colour values from the dataviz skill's `references/palette.md` (categorical, in order; load the skill first) and put them in the `COLORS` array.

```html
{% extends 'base.html' %}
{% block title %}Stats — PID upload bot{% endblock %}
{% block content %}
<section class="card">
  <div class="stats-head">
    <h2>Stats</h2>
    <nav class="rangepick" aria-label="Period">
      {% for key, label in ranges %}
        <a class="{% if view.range == key %}on{% endif %}" {% if view.range == key %}aria-current="true"{% endif %}
           href="{{ url_for('stats_page', range=key) }}">{{ label }}</a>
      {% endfor %}
    </nav>
  </div>
</section>

{% for id, title, block in [('uploads', 'Uploads', view.uploads),
                            ('failures', 'Failures by cause', view.failures),
                            ('backlog', 'Review backlog', view.backlog),
                            ('gemini', 'Gemini calls, free and paid', view.gemini)] %}
  <section class="card">
    <h2>{{ title }}</h2>
    <p class="stats-summary">{{ block.summary }}</p>
    {% if id == 'uploads' or block.since %}
      <div class="chartbox"><canvas id="chart-{{ id }}" role="img" aria-label="{{ block.summary }}"></canvas></div>
      <details class="numbers"><summary>Show numbers</summary>
        <div class="cdx-table"><div class="cdx-table__table-wrapper"><table class="cdx-table__table">
          <thead><tr><th>Period</th>
            {% if id == 'uploads' %}<th>Uploaded</th>
            {% elif id == 'failures' %}{% for c in block.series %}<th>{{ c }}</th>{% endfor %}
            {% elif id == 'backlog' %}<th>Check the English</th><th>Needs categories</th>
            {% else %}<th>Free</th><th>Paid</th><th>Free limit hit</th>{% endif %}</tr></thead>
          <tbody>
            {% for label in view.labels %}{% set i = loop.index0 %}
              <tr><td>{{ label }}</td>
                {% if id == 'uploads' %}<td>{{ block['values'][i] }}</td>
                {% elif id == 'failures' %}{% for c, vals in block.series.items() %}<td>{{ vals[i] }}</td>{% endfor %}
                {% elif id == 'backlog' %}<td>{{ block.review[i] if block.review[i] is not none else '' }}</td><td>{{ block.uncategorised[i] if block.uncategorised[i] is not none else '' }}</td>
                {% else %}<td>{{ block.free[i] }}</td><td>{{ block.paid[i] }}</td><td>{{ block.free_limit[i] }}</td>{% endif %}
              </tr>
            {% endfor %}
          </tbody>
        </table></div></div>
      </details>
    {% endif %}
  </section>
{% endfor %}

<script type="application/json" id="statsdata">{{ view|tojson }}</script>
<script src="{{ url_for('static', filename='chartjs/chart.umd.min.js') }}"></script>
<script>
  // Draw the four charts from the JSON above. Colours: the dataviz skill's
  // categorical palette, in order; one per series, the same series always
  // the same colour.
  (function () {
    var v = JSON.parse(document.getElementById('statsdata').textContent);
    var COLORS = [/* paste the categorical palette here, in order */];
    var css = getComputedStyle(document.documentElement);
    Chart.defaults.font.family = css.getPropertyValue('--font-family-system-sans') || 'sans-serif';
    Chart.defaults.color = css.getPropertyValue('--color-subtle') || '#54595d';
    Chart.defaults.maintainAspectRatio = false;
    function draw(id, type, datasets, stacked) {
      var el = document.getElementById('chart-' + id);
      if (!el) return;
      new Chart(el, {type: type, data: {labels: v.labels, datasets: datasets},
        options: {interaction: {mode: 'index', intersect: false},
                  plugins: {legend: {display: datasets.length > 1, position: 'bottom'}},
                  scales: {x: {stacked: !!stacked, grid: {display: false}},
                           y: {stacked: !!stacked, beginAtZero: true, ticks: {precision: 0}}}}});
    }
    draw('uploads', 'bar', [{label: 'Uploaded', data: v.uploads.values, backgroundColor: COLORS[0]}]);
    draw('failures', 'bar', Object.keys(v.failures.series).map(function (c, i) {
      return {label: c, data: v.failures.series[c], backgroundColor: COLORS[i % COLORS.length]};
    }), true);
    draw('backlog', 'line', [
      {label: 'Check the English', data: v.backlog.review, borderColor: COLORS[0], backgroundColor: COLORS[0], spanGaps: true},
      {label: 'Needs categories', data: v.backlog.uncategorised, borderColor: COLORS[1], backgroundColor: COLORS[1], spanGaps: true}]);
    draw('gemini', 'bar', [
      {label: 'Free', data: v.gemini.free, backgroundColor: COLORS[0], stack: 'calls'},
      {label: 'Paid', data: v.gemini.paid, backgroundColor: COLORS[1], stack: 'calls'},
      {label: 'Free limit hit', data: v.gemini.free_limit, type: 'line', borderColor: COLORS[3], backgroundColor: COLORS[3]}], true);
  })();
</script>
{% endblock %}
```

- [ ] **Step 7: CSS** (append to `panel.css`)

```css
/* ── Stats ─────────────────────────────────────────────────────────────── */
.stats-head { display: flex; flex-wrap: wrap; align-items: center; gap: 1rem; justify-content: space-between; }
.stats-head h2 { margin: 0; }
.rangepick { display: inline-flex; border: 1px solid var(--border-strong); border-radius: var(--radius); }
.rangepick a { padding: 0.3rem 0.75rem; font-size: 0.875rem; color: var(--accent); text-decoration: none; }
.rangepick a + a { border-left: 1px solid var(--border-strong); }
.rangepick a.on { background: var(--accent); color: #fff; font-weight: 700; }
.stats-summary { margin: 0.25rem 0 0.75rem; }
.chartbox { position: relative; height: 240px; }
.numbers { margin-top: 0.75rem; font-size: 0.875rem; }
.numbers .cdx-table { max-height: 20rem; overflow: auto; }
```

- [ ] **Step 8: Run the suite**, then look at `/stats` in `scripts/preview_panel.py` at desktop width and at 360px (iframe check: `scrollWidth <= clientWidth`).

Run: `python test_pipeline.py 2>&1 | tail -1`
Expected: `N checks passed`

- [ ] **Step 9: Commit**

```bash
git add panel/static/chartjs panel/templates/stats.html panel/templates/base.html panel/app.py panel/static/panel.css test_pipeline.py
git commit -m "Add the Stats page: uploads, failures by cause, review backlog, Gemini tiers"
```
