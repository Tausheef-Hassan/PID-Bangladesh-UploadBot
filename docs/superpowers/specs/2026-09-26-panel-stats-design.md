# Control panel part 3 — Stats and history

Part 3 of 5 (see the part 1 spec). Builds on the Codex panel shipped in part 1.

## Goal

A **Stats** tab that answers four questions at a glance:

1. Is the bot uploading as much as usual? (uploads over time)
2. When photos fail, why? (failures by cause)
3. Is the review backlog shrinking? (English still unchecked, categories missing)
4. How much is the paid Gemini tier being used? (free vs paid calls, free-tier limit hits)

## Decisions (agreed)

- **Charts: Chart.js 4**, copied into `panel/static/chartjs/chart.umd.min.js` and pinned, like Codex. Nothing from a CDN.
- **Storage: a daily totals file written by the bot**, `stats_daily.json` in `$TOOL_DATA_DIR` (`config.STATS_PATH`). It keeps history indefinitely and doesn't need the page to be open.
- **Gemini: counts only**, no money estimates.
- Tab order: Overview · Uploads · **Stats** · Name corrections · Queue · Access.

## Data

### `stats_daily.json` (new, written by the bot)

```json
{
  "2026-09-26": {
    "runs": 14,
    "uploaded": 37,
    "duplicates": 5,
    "failed": {"download": 1, "ocr": 0, "translation": 2, "title": 0, "upload": 1, "other": 0},
    "gemini": {"free": 70, "paid": 4, "free_limit": 3},
    "backlog": {"review": 5120, "uncategorised": 8342}
  }
}
```

- Days are UTC dates. Each run **adds** its counts to that day's entry, and **replaces** `backlog` with the latest snapshot.
- Written atomically (a temp file, then `os.replace`). Only one bot run happens at a time (hourly job), so there are no competing writers.
- A missing or corrupt file reads as `{}`. A failed stats write is logged and never fails the run.

### Where each number comes from

| Field | Source |
|---|---|
| `uploaded`, `duplicates` | the run's existing counters |
| `failed.<cause>` | new pure function `stats.failure_cause(row)` applied to each row at the end of the run |
| `gemini.free/paid` | `translator._gemini_text` counts each accepted answer by the slot that produced it (AI Studio = free, Vertex = paid) |
| `gemini.free_limit` | the same function counts each retryable 429 from an AI Studio slot |
| `backlog.review` | Commons search `totalhits` for `hastemplate:"Auto-translated PID English description"` |
| `backlog.uncategorised` | page count of *Press Information Department images without category* |

`failure_cause(row)` returns `None` for successes, duplicates and rows skipped with no URL. Otherwise, the first of these that applies:
- `download`: `ocr_status` mentions 404, download or image processing error
- `ocr`: `ocr_status` starts with "OCR failed" or is "No text detected"
- `translation`: `translation_status` starts with "Error"
- `title`: `filename_status` starts with "Error"
- `upload`: `upload_status` starts with "Failed"
- `other`: anything else, such as `Exception:`

Gemini usage counters live in `translator` behind a lock: `translator.usage()` returns them and `translator.reset_usage()` clears them. `main.py` resets at the start of a run and reads at the end.

The backlog numbers are two small Commons API GETs from the bot, through `config.http_session`. If they fail, `backlog` keeps its previous value for the day.

### Uploads over time (existing data)

Read from `PIDDateData/<year>.json` through the existing `commons._uploads_raw` (cached for 2 minutes). Only rows with a filename count; duplicates registered without a file don't. This gives full history, not just from when this ships.

## Page `/stats`

- A range switch as links: **7 days · 30 days · 12 months** (`?range=7d|30d|12m`, default `30d`). Days are bucketed daily for 7d/30d and monthly for 12m.
- Four sections, each with a one-sentence summary above its chart:
  1. **Uploads.** Bar chart. The summary compares with the previous period of the same length, e.g. "412 uploaded in the last 30 days, 9% fewer than the 30 days before."
  2. **Failures by cause.** Stacked bars, one colour per cause. Summary: "18 failed; most were translation (11)."
  3. **Review backlog.** Two lines. Summary: "5,120 photos still need the English checked (down 140 this period)."
  4. **Gemini calls.** Stacked bars, free and paid, with free-limit hits as a line on the same axis. Summary: "4% of calls used the paid tier."
- **Charts 2–4 while history is thin** say "Collecting since <first date>." and show what exists. With no data at all they show "No data yet: the first run after this update starts it." instead of an empty chart.
- **Accessibility:** each canvas has an `aria-label` restating its summary. Every chart also has a "Show numbers" `<details>` table.
- **Colours** come from the Codex tokens, using a fixed, colour-blind-safe order for failure causes. The dataviz skill guides the palette and chart marks.
- **Phone:** charts are full width with the aspect ratio kept; no horizontal scroll at 360px.
- **Code layout:**
  - The route computes every series in Python: `panel/stats_view.py` has pure functions from (daily dict, uploads rows, range, today) to chart data, so it can be tested without Flask.
  - The data goes to the template as JSON in a `<script type="application/json">` tag.
  - One small inline script draws the four charts from it.

## Testing (`test_pipeline.py`)

- `failure_cause` has one row per cause, plus success, duplicate and no-URL rows returning None.
- `stats.record_run(...)` adds to today's counts and replaces the backlog; it writes atomically; a corrupt file is treated as `{}`.
- Gemini usage counts free and paid acceptances and free-tier 429s, using the existing fake-client pattern in the ladder tests.
- `stats_view`:
  - daily and monthly bucketing
  - the previous-period comparison
  - the "Collecting since" / "No data yet" states
  - uploads counted from rows with filenames only
- `/stats` renders for an anonymous visitor, contains the JSON and all four sections, and has no external script URL.
- The tab appears on every page.

## Out of scope

Money estimates, per-person/ministry stats, CSV export, backfilling charts 2–4 from old Commons logs.
