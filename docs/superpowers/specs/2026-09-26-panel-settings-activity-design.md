# Control panel part 4 — Settings and activity log

**Status: DRAFT, design proposed and not yet approved by the user (2026-09-26).**
Resume by asking the user to confirm the design below, then finish the spec,
write the plan, and build as parts 1–3 were built.

## Decided with the user

- **Settings to expose:** schedule, photos per run, paid Gemini on/off. (Not model choice.)
- **Who can change them:** the owner only (`PANEL_OWNER`). Maintainers see the values read-only.

## Proposed (awaiting the user's OK)

### Storage

- `settings.json` in `$TOOL_DATA_DIR`. The panel writes it atomically (temp file, then `os.replace`); the bot reads it at the start of each run. A missing or corrupt file means today's behaviour.
- **Paid Gemini on/off** (default on). When off, `translator._gemini_text` skips the Vertex slots. When the free tier is exhausted the row fails with "Paid tier is off". Failed rows aren't registered in PIDDateData, so the next run retries them.
- **Photos per run** (default no limit; allowed 1–500). `main.py` processes at most N new rows per run; the rest wait for the next run for the same reason. Check the scraper's row order (newest first?) before choosing which N.
- **Schedule** (every hour, 2 hours, 6 hours, or daily). It is PATCHed to the Toolforge job and also saved in `settings.json`. **Resume** and **Start** read the saved schedule instead of `session['schedule_before_pause']`, which fixes resuming from another browser falling back to `@hourly`. While the job is paused, a change is saved and applies on resume.

### Activity log

- `activity.jsonl` in `$TOOL_DATA_DIR`, append-only. Each line is `{at, user, action, detail}`.
- Recorded for every panel write:
  - start, run, pause, resume, stop
  - settings changes (old → new)
  - name corrections added, updated, deleted, or remembered from a file page
  - access granted or revoked
  - file saves, copyright flags, bulk categorising (with titles)
- Visible to maintainers only.

### Page

- The **Access** tab becomes **Settings** (maintainers only), with three sections:
  1. Bot settings: a form for the owner, read-only for others.
  2. Who can run the bot: the current Access page, unchanged.
  3. Activity: the newest 100 entries, a filter by person, and "Show older".

### Tests

- The bot reads the settings; a missing or corrupt file gives the defaults.
- The Vertex slots are skipped when paid is off.
- The per-run cap is honoured.
- Resume uses the saved schedule.
- Only the owner can write settings.
- Each panel action writes exactly one log line with the right user.

## Remaining roadmap

- Part 5: health checks and alerts.
- Deferred minors from parts 1–3 are listed in their commit history and final-review notes.
- Open: the live panel showed "Can't reach the Jobs API". The panel now shows the actual reason on the Overview status box; the user needs to deploy and report it.
