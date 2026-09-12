# PID Image Processor & Uploader

A Python automation bot that archives press-release photographs from the Bangladesh **Press Information Department** (PID) website (`pressinform.gov.bd`) to [Wikimedia Commons](https://commons.wikimedia.org/). It scrapes images, performs Bengali OCR, translates captions to English with Gemini AI, generates policy-compliant filenames, and uploads with full Wikitext metadata — all automatically.

---

## Table of Contents

- [Architecture](#architecture)
- [Cropping Algorithm](#cropping-algorithm)
- [Project Structure](#project-structure)
- [Technologies](#technologies)
- [Prerequisites & Setup](#prerequisites--setup)
- [Running the Bot](#running-the-bot)
- [Deployment on Toolforge](#deployment-on-toolforge)
- [Key Files](#key-files)
- [Development Conventions](#development-conventions)

---

## Architecture

The bot operates as a sequential pipeline with concurrent per-image processing:

```
pressinform.gov.bd
        │
        ▼
 ┌─────────────┐
 │   Scraper   │  Crawls daily-photos pages; filters duplicates via MD5 checksums
 └──────┬──────┘  and URL history pulled from Wikimedia Commons.
        │
        ▼
 ┌──────────────────┐
 │  Image Processor │  Downloads images (Wayback Machine fallback for 404s).
 │                  │  Detects horizontal separator by vertical edge-column
 │                  │  scanning (see Cropping Algorithm below).
 │                  │  Crops photograph from Bengali caption.
 │                  │  Performs OCR on caption via Google Drive API.
 └──────┬───────────┘
        │
        ▼  (concurrent — up to 5 threads)
 ┌──────────────────┐
 │   Translator     │  Cleans OCR output with a TSV replacement table.
 │                  │  Translates Bengali → English via Gemini Vertex AI
 │                  │  (primary) or Google Cloud Translate (fallback).
 └──────┬───────────┘
        │
        ▼
 ┌──────────────────┐
 │ Filename/Title   │  Gemini AI Studio or Vertex AI generates a descriptive,
 │   Generator      │  policy-compliant Wikimedia Commons filename ≤240 bytes.
 └──────┬───────────┘
        │
        ▼
 ┌──────────────────┐
 │    Uploader      │  Uploads image with {{Information}}, {{PD-BDGov-PID}},
 │                  │  and Bengali/English descriptions via pywikibot.
 │                  │  Batch-updates Module:PIDDateData on Commons.
 └──────┬───────────┘
        │
        ▼
 ┌──────────────────┐
 │  Commons Logger  │  Appends a run summary to the bot's daily JSON log
 │                  │  page on Commons (one page per day).
 └──────────────────┘
        │
        ▼  (async background thread)
 ┌──────────────────┐
 │  Wayback Machine │  Fires source URLs at the Internet Archive Save Page
 │    Archiver      │  Now API without waiting for the capture; a persistent
 │                  │  JSON queue is confirmed on the next run.
 └──────────────────┘
```

---

## Cropping Algorithm

PID source images are composite JPEGs — a **press photograph on top** and a **printed Bengali caption band on the bottom**, divided by a horizontal white (or off-white) separator strip.

```
┌──────────────────────────────────┐
│                                  │  ← photograph (uploaded to Commons)
│         Press photo              │
│                                  │
├──────────────────────────────────┤  ← white / off-white separator band
│  Bengali caption text...         │  ← OCR'd then translated
└──────────────────────────────────┘
```

Separator detection is a **vertical edge-column scan**, implemented in
`src/image_processor.py` as `find_white_separator()`. It does not look for a
white band; it looks for where the flat page colour below the photograph stops
being flat.

### Pass 1 — Edge-column uniform runs

| Parameter | Value |
|---|---|
| Columns sampled | 8 — `x = 1..4` and `x = width-5..width-2` |
| Colour tolerance | within **2 %** of 255 per channel, against the running mean |
| Search floor | rows below **40 %** of image height |
| Scan start | `height - 6` (skips bottom padding), walking **upwards** |

Each of the eight columns walks bottom-up, accumulating pixels for as long as
each new pixel stays within 2 % of the mean of the pixels already collected.
Where a column breaks out of that run is its vote for where the caption band
begins. Sampling only the extreme left and right edges avoids the caption text
itself, which never reaches the margins.

### Pass 2 — Cross-column agreement

Every (left column, right column) pair is checked:

- pairs whose run heights differ by more than **4 px** are discarded;
- for the rest, the lower of the two boundary rows is scanned horizontally
  between the two columns;
- that row is accepted as a `valid_line` only if **≥ 98 %** of its pixels are
  within 2 % of the row's own mean colour.

`cutoff_row` is then `min(valid_lines)`, or — if no pair agreed — the highest
uniform-run top found by any single column.

> **Known limitation.** `min()` takes the **highest** candidate row, so a single
> edge column whose uniform run leaks up into the photograph pulls the whole cut
> with it. Failures are therefore always cuts that are too high, never too low.
> Measured at roughly 0.3 % of images.

### Pass 3 — Background-row fallback

The fallback (`find_separator_fallback()`) runs when pass 2 produced nothing, or
when `cutoff_row` lands in the suspicious **38 %–42 %** height band. Starting at
75 % of image height, it scans downwards for consecutive rows that are ≥ 98 %
page background — pure white `(255,255,255)` or `#faf9fb` `(250,249,251)`, each
within the same 2 % tolerance.

The run length required scales with image height:

```python
fallback_required_lines = round((4 / math.log(3100 / 670)) * math.log(height / 670) + 5)
```

When the fallback fires, side-whitespace cropping is enabled for that image.

### Crop offset

A small **log-scaled pixel offset** is subtracted from `cutoff_row` so the
photograph is not clipped at its very bottom edge:

```python
offset = max(2, round(2 + 3 / math.log(3100 / 670) * math.log(height / 670)))
```

This runs from 2 px at 670 px tall up to ~5 px at 3100 px. The OCR section is
trimmed from the top by **twice** the same offset, so the separator strip itself
never reaches the OCR engine.

### Side whitespace cropping

`crop_side_whitespace()` removes left and right columns that are ≥ 98 % page
background — the same two colours and 2 % tolerance the fallback uses. The crop
boundary is then expanded back outward by the log-scaled formula above to avoid
clipping content sitting right at the border.

It is **not** applied to every image: `crop_image_sections()` only enables it
when the pass-3 fallback was used.

---

## Project Structure

```
pid/
├── main.py                      # Pipeline orchestrator
├── config.py                    # Constants, logging, IPv4 enforcement, retrying HTTP session
├── credentials.py               # Credential loading (Gemini, GCloud, IA keys)
├── requirements.txt
│
├── test_pipeline.py             # Offline self-checks: log page size, Wayback tail
├── test_processor_cv.py         # Separator detection against sample images
│
├── src/
│   ├── scraper.py               # Web scraping & duplicate detection
│   ├── image_processor.py       # Download, separator detection, OCR
│   ├── translator.py            # Bengali → English translation & filename generation
│   ├── uploader.py              # Pywikibot upload & Module:PIDDateData update
│   ├── commons_log.py           # Commons bot log page writer
│   └── wayback.py               # Wayback Machine archiver & retry queue
│
├── data/                        # translation_replacements.tsv and similar data files
├── output/                      # Local run artifacts
├── toolforge/                   # Toolforge-specific deployment configs
│
├── user-config.py               # Pywikibot site config  ← not committed
├── user-password.py             # Pywikibot bot password ← not committed
├── gemini.key                   # Gemini AI Studio API key ← not committed
├── ia.key                       # Internet Archive S3 keys ← not committed
├── JSON.json                    # Google Cloud service account ← not committed
├── drive_token.json             # Google Drive OAuth2 token ← not committed
└── drive_oauth_client.json      # Google Drive OAuth2 client secrets ← not committed
```

---

## Technologies

| Category | Library / Service | Role |
|---|---|---|
| **Language** | Python 3.x | — |
| **AI — Primary** | Google Gemini AI Studio (`gemini-3.1-flash-lite`) | Translation & filename generation — **free tier, tried first** |
| **AI — Secondary** | Google Gemini Vertex AI (`gemini-3.1-flash-lite`) | Paid fallback if AI Studio fails |
| **Translation fallback** | Google Cloud Translate v2 | Final fallback if all Gemini clients fail |
| **OCR** | Google Drive API | Uploads image as Google Doc; exports plain text |
| **Image processing** | OpenCV, Pillow, NumPy | Separator detection, cropping, EXIF preservation |
| **Web scraping** | BeautifulSoup4, Requests | Crawls PID archive pages |
| **Wikimedia** | Pywikibot | Upload, page editing, module updates |
| **Archiving** | Internet Archive Save Page Now API | Preserves source URLs permanently |
| **Concurrency** | `threading`, `concurrent.futures` | Parallel per-image processing (5 workers) |

---

## Prerequisites & Setup

### 1. Python Environment

Install dependencies from `requirements.txt`:

```bash
pip install -r requirements.txt
```

### 2. Credential Files

All credential files must be placed in the **same directory as `main.py`** (for local development) or in `$TOOL_DATA_DIR` (for Toolforge).

| File | Contents |
|---|---|
| `user-config.py` | Pywikibot site configuration |
| `user-password.py` | Pywikibot bot password |
| `gemini.key` | One line: `GEMINI_API_KEY=your_key_here` (AI Studio free tier) |
| `JSON.json` | Google Cloud service account JSON key (for Vertex AI + Translate) |
| `drive_token.json` | Persisted Google Drive OAuth2 token |
| `drive_oauth_client.json` | Google Drive OAuth2 client secrets |
| `ia.key` | Internet Archive S3-like keys for Save Page Now |

> **Pywikibot config search order:** `$TOOL_DATA_DIR` → script directory → `~/pywikibot/` → `~/.pywikibot/` → current working directory.

### 3. AI Client Setup

The bot uses **two Gemini clients** tried in this order (cheapest first):
1. **AI Studio** (`gemini.key`) — free tier, primary attempt.
2. **Vertex AI** (`JSON.json` service account) — paid, used only if AI Studio fails.
3. **Google Cloud Translate** — final fallback if Gemini output still contains Bengali.

If AI Studio's primary model fails, AI Studio's fallback model is tried next before escalating to Vertex AI. The primary model is `gemini-3.1-flash-lite`; the fallback is `gemini-3.5-flash`. Both can be changed in `config.py`.

---

## Running the Bot

Run the main pipeline once:

```bash
python main.py
```

That is the only entry point. Scheduling is Toolforge's job: `toolforge/job.yaml`
runs `run-bot` on an `@hourly` cron schedule.

### Self-checks

Both test files are plain `assert` scripts — no pytest, no fixtures, no network:

```bash
python test_pipeline.py       # Commons log page stays under the size limit;
                              # the Wayback tail honours its wall-clock budget
python test_processor_cv.py   # separator detection on sample images
```

---

## Deployment on Toolforge

The bot is designed for [Wikimedia Toolforge](https://wikitech.wikimedia.org/wiki/Toolforge) (Kubernetes-based):

- **`Procfile`** defines the `run-bot` process type that `job.yaml` invokes.
- **`$TOOL_DATA_DIR`** is automatically set by the Build Service; credential files are read from there.
- **IPv4 enforcement** is applied at startup (via `config.py`) to avoid Kubernetes IPv6 issues.
- **`toolforge/job.yaml`** registers the hourly background job; there is no web service.

---

## Key Files

| File | Purpose |
|---|---|
| `main.py` | Top-level pipeline orchestrator; wires all modules together |
| `config.py` | Central constants, logging, `http_session()` retry policy, IPv4 patch |
| `credentials.py` | Loads all credentials from files/env at runtime |
| `src/scraper.py` | Scrapes PID site; compares MD5 checksums against Commons |
| `src/image_processor.py` | Downloads, crops, and OCRs images |
| `src/translator.py` | Translation pipeline and Gemini filename generation |
| `src/uploader.py` | Pywikibot upload and `Module:PIDDateData` batch update |
| `src/commons_log.py` | Writes run summary to the bot's **daily** JSON log page on Commons |
| `test_pipeline.py` | Offline self-checks for the two silent-failure modes (log size, Wayback tail) |
| `src/wayback.py` | Async Wayback archiving; submits without polling, confirms from the queue next run |
| `data/translation_replacements.tsv` | Manual OCR correction rules applied before translation |
| `wayback_pending.json` | Persistent queue for failed Wayback Machine submissions |

---

## Development Conventions

- **Resilience:** HTTP calls go through `config.http_session()` — a `requests.Session` carrying a `urllib3` `Retry` (exponential backoff with jitter, `429`/`5xx` retried, transport errors retried). Only idempotent methods are retried, so a Save Page Now `POST` is never resubmitted behind the bot's back. Google API calls use the client library's own `num_retries=config.API_RETRIES`.
- **Duplicate prevention:** MD5 checksums of raw image bytes are compared against all existing Wikimedia Commons records *before* any AI processing, saving API quota.
- **Concurrency model:** Up to 5 worker threads process images in parallel. Uploads and `Module:PIDDateData` edits are serialised with a dedicated lock to avoid edit conflicts.
- **Batch module updates:** Successful upload metadata is queued and written to `Module:PIDDateData` in a single batch edit at the end of each run, minimising API round-trips.
- **IPv4 enforcement:** `urllib3`'s `allowed_gai_family` is monkey-patched at import time to force IPv4 and avoid Kubernetes/Toolforge IPv6 connectivity issues.
- **Bounded log pages:** The Commons log is one JSON page per day. Every run rewrites the whole page, so a month of hourly runs on a single page would cross `$wgMaxArticleSize` and every subsequent save would fail silently. `wikitext_description` is stripped before logging for the same reason — it is the heaviest key and is rebuilt at upload time anyway.
- **Bounded Wayback tail:** Archive submissions are fire-and-forget (`confirm=False`); polling SPN2 costs up to 90 s per URL and the pool thread is non-daemon, which kept the hourly job alive long after its work was done. Unconfirmed URLs sit in `wayback_pending.json` and the next run's confirmation pass clears them under a `RETRY_BUDGET` wall-clock cap (600 s).
- **Toolforge ready:** The bot is a plain one-shot script; Toolforge's job scheduler owns the hourly cadence, so the process has no internal loop to supervise.
