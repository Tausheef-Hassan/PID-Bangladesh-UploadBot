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
 │                  │  Detects horizontal separator with Multi-Pass Top-Down
 │                  │  Variance Scanning (see Cropping Algorithm below).
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
 │  Commons Logger  │  Appends a run summary to the bot's log page on Commons.
 └──────────────────┘
        │
        ▼  (async background thread)
 ┌──────────────────┐
 │  Wayback Machine │  Submits source URLs to the Internet Archive Save Page
 │    Archiver      │  Now API; retries failures from a persistent JSON queue.
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

The separator detection runs in **three passes**, implemented in [`src/image_processor.py`](file:///D:/PID/PID%202.0/pid/src/image_processor.py) as `find_white_separator()`.

### Pass 1 — Strict white band

| Parameter | Value |
|---|---|
| Grayscale threshold | pixel value **> 240** |
| Required row coverage | **≥ 95 %** of image width |
| Search window | rows 35 % – 95 % of image height |
| Minimum consecutive rows (`MIN_RUN`) | `max(3, height / 300)` — scales with image size |

The algorithm converts the image to grayscale and computes, for each row, the fraction of pixels brighter than 240 (pure white). Scanning top-down from 35 % of height, it looks for the first **run** of ≥ `MIN_RUN` consecutive rows that all exceed 95 % coverage.

Using a run (rather than just checking 2–3 rows as before) prevents **JPEG ringing artifacts** — the faint dark fringe that JPEG compression places at high-contrast edges — from creating false positives at the photograph/separator boundary.

The bottom 5 % of the image is excluded to avoid white bottom-padding being mistaken for the separator.

### Pass 2 — Relaxed off-white band

| Parameter | Value |
|---|---|
| Grayscale threshold | pixel value **> 220** |
| Required row coverage | **≥ 90 %** of image width |

Older or heavily re-compressed PID images use a **light-grey separator** (not pure white). If pass 1 found nothing, pass 2 repeats the same run-length scan with looser thresholds, catching these cases without affecting the majority of images where pass 1 already succeeds.

### Pass 3 — Gradient edge fallback

If no flat-colour band is found at all (unusual layouts, missing separator), the algorithm falls back to a **Sobel horizontal-edge detector**:

1. Gaussian blur (5 × 5) to suppress JPEG noise.
2. Vertical Sobel derivative (`dy`) to amplify horizontal edges.
3. Sum gradient magnitude per row → `row_energy`.
4. Find the row with maximum energy in the lower half (50 % – 95 %).
5. Accept only if that row's energy is **> 2.5 × the mean** — avoids returning a "best" row in a featureless image.

This ensures the function almost never returns `-1`; instead the full-image OCR fallback in `process_image()` is reserved for truly unrecognisable images.

### Crop offset

After the separator row is found, a small **log-scaled pixel offset** is subtracted so the photograph is not clipped at its very bottom edge:

```python
offset = max(2, int(round((3 / math.log(3100 / 670)) * math.log(height / 670))))
```

This scales from ~2 px for a 670 px tall image up to ~5 px for a 3100 px tall image, giving proportional breathing room without wasting caption rows.

### Side whitespace cropping

A separate `crop_side_whitespace()` step removes white (`≥ 250, 250, 250`) or near-white `#fbf9fa` (`≥ 245, 244, 246`) columns from the left and right edges when ≥ 98 % of pixels in a column match. The crop boundary is then expanded outward by the same log-scaled formula to avoid clipping content right at the border.

---

## Project Structure

```
pid/
├── main.py                      # Pipeline orchestrator
├── config.py                    # Constants, logging, IPv4 enforcement, retry decorator
├── credentials.py               # Credential loading (Gemini, GCloud, IA keys)
├── requirements.txt
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
| `config.py` | Central constants, logging, retry decorator, IPv4 patch |
| `credentials.py` | Loads all credentials from files/env at runtime |
| `src/scraper.py` | Scrapes PID site; compares MD5 checksums against Commons |
| `src/image_processor.py` | Downloads, crops, and OCRs images |
| `src/translator.py` | Translation pipeline and Gemini filename generation |
| `src/uploader.py` | Pywikibot upload and `Module:PIDDateData` batch update |
| `src/commons_log.py` | Writes run summary to bot log page on Commons |
| `src/wayback.py` | Async Wayback Machine archiving with persistent retry queue |
| `data/translation_replacements.tsv` | Manual OCR correction rules applied before translation |
| `wayback_pending.json` | Persistent queue for failed Wayback Machine submissions |

---

## Development Conventions

- **Resilience:** Every network and AI call is wrapped in exponential-backoff retry logic (`config.retry_on_failure`). Max retries and backoff parameters are configurable in `config.py`.
- **Duplicate prevention:** MD5 checksums of raw image bytes are compared against all existing Wikimedia Commons records *before* any AI processing, saving API quota.
- **Concurrency model:** Up to 5 worker threads process images in parallel. Uploads and `Module:PIDDateData` edits are serialised with a dedicated lock to avoid edit conflicts.
- **Batch module updates:** Successful upload metadata is queued and written to `Module:PIDDateData` in a single batch edit at the end of each run, minimising API round-trips.
- **IPv4 enforcement:** `urllib3`'s `allowed_gai_family` is monkey-patched at import time to force IPv4 and avoid Kubernetes/Toolforge IPv6 connectivity issues.
- **Toolforge ready:** The bot is a plain one-shot script; Toolforge's job scheduler owns the hourly cadence, so the process has no internal loop to supervise.
