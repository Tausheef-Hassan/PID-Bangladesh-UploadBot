# Control panel redesign — part 1: Commons look, plain language, name fixing

Part 1 of 5. Later parts, each with its own spec: 2 search all uploads,
3 stats and history, 4 settings from the web + activity log, 5 health checks
+ alerts. "Can't reach the Jobs API" is a separate bug fixed before this ships.

## Problem

Maintainers find the panel hard to understand (terminal look, words like
cronjob, stderr, Jobs API), slow to use (too many clicks to check or fix a
file), cramped on a phone, and dated-looking. Fixing a wrong name means
editing the Commons page by hand, and nothing stops the next run repeating it.

## Decisions (agreed in brainstorming)

- **Look:** Wikimedia's Codex design system, so the panel reads as part of
  Commons.
- **Navigation:** one row of tabs under the title, like a Commons page's
  File / Discussion / History tabs. On a phone the row scrolls sideways.
- **Tabs:** Overview · Uploads · Name corrections · Queue · Access
  (Access only for maintainers).
- **Step-by-step upload page** with name fixing, as mocked up.

## Codex

- Copy `@wikimedia/codex` `codex.style.min.css` and
  `@wikimedia/codex-design-tokens` `theme-wikimedia-ui.css` into
  `panel/static/codex/`, pinned to one version, same as `htmx.min.js`.
  Toolforge tools must not load third-party URLs, so nothing comes from a CDN.
- Use Codex's CSS-only components (buttons, message boxes, text inputs,
  checkbox, tabs, cards, table) by class name. No Vue.
- `panel.css` shrinks to layout plus what Codex lacks (image strip, crop
  marker, caption highlights). The terminal styling and IBM Plex Mono go;
  the self-hosted Noto Sans Bengali stays for Bengali text.

## Wording

A single pass over every template. Examples:

| Now | Becomes |
|---|---|
| Stderr / Output | Errors / Output |
| cronjob, schedule `@hourly` | Runs every hour |
| Jobs API unreachable… | The panel can't reach Toolforge right now, so the buttons won't work. Reason: `<error>` |
| Not loaded on Toolforge | Stopped |
| Replacements | Name corrections |

`fetch_job()` keeps the exception text so the status box can show the reason.

## Pages

### Overview (`/`)

1. **Status box** (Codex message: success / notice / warning / error): one
   sentence, e.g. "Running: 3 photos uploaded so far", "Stopped", "Paused",
   "Last run failed". It shows only the buttons that make sense for the state:
   stopped → Start; running or idle → Run now, Pause, Stop; paused → Resume.
2. Last run's counts: uploaded, already on Commons, failed, images seen.
3. **Recent uploads strip:** last 8 thumbnails, each opening its
   step-by-step page.
4. Run history (the existing tick strip).
5. **Technical log**, collapsed by default (`<details>`), with the existing
   filter, freeze and download tools inside.

### Uploads (`/uploads`)

Thumbnail grid with a filter: **Needs a look** and **All**. Needs a look is
files that still carry `{{Auto-translated PID English description}}` (one
`list=embeddedin` API call on that template, not one fetch per file), plus
uploads that failed in the last 3 days' logs. Clicking opens the step-by-step page.
Bulk categorising stays as it is.

### Step-by-step upload (`/upload/<id>`)

The existing detail route, restructured into steps:

1. Downloaded from PID, with the cut line drawn where the separator was found.
2. Photo uploaded.
3. Bengali caption from OCR. Wikidata-matched names are highlighted green,
   each listed with its English label and a Wikidata link. Selecting any
   words in the caption offers **Fix this name…** (inline popover: type the
   English, which is inserted into the description in step 4).
4. English description (editable) → **Save to Commons**, saved as the
   signed-in user, as today. Checkbox **Also remember my name fixes for future
   uploads**, on by default: each fixed pair is appended to the name
   corrections table.

A message box at the top summarises: "N names confirmed on Wikidata. Any other
names were spelled by Gemini. Select one in the caption to fix it."

The bot can't know which unmatched words are names, so unmatched names are
never highlighted. Selecting the words is how a maintainer points at one.

### Name corrections (`/replacements`)

Replaces the raw textarea with a searchable table: Bengali, English, added
by, added on, Edit / Delete, plus an Add row. Maintainers only for writes;
public read.

File format stays line-based and backward compatible:
`bengali|||english|||user|||YYYY-MM-DD`. The last two fields are optional.
`load_translation_replacements()` reads only the first two fields (today it
would fold the metadata into the English text, so the loader changes).

### Queue and Access

Restyled with Codex and the new wording. No behaviour change.

## Bot change

`main.py` stores the resolver's matches on the row:
`row["name_matches"] = [[bengali, english, qid], ...]`. They reach the daily
Commons log with the rest of the row, where `commons.detail_for()` already
reads per-image detail.

## Phone

Tested at 360px wide: tabs scroll sideways, the Overview buttons wrap, the
upload grid drops to 2 columns, the step-by-step images stack, and the
caption popover stays on screen.

## Testing

Additions to `test_pipeline.py`:
- The status box shows the right buttons for each state (stopped, running,
  paused, API down).
- The Jobs API-down message includes the reason.
- Saving with "remember" appends the pair with user and date. The loader
  ignores the metadata fields.
- The name-corrections table add, edit and delete are maintainer-only.
- Log rows carry `name_matches`, and the detail page highlights them.
- The existing panel tests still pass (routes, auth and read-only views are
  unchanged).

Visual check with `scripts/preview_panel.py` at desktop and 360px widths.

## Out of scope

Search across all uploads, stats, web settings, activity log, health checks,
alerts. Parts 2–5.
