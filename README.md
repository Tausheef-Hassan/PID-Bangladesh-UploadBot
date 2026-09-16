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
- **`toolforge/job.yaml`** registers the hourly background job. The Build Service
  builds straight from GitHub and never clones the repo into the tool's home, so
  this file is **not** on the bastion — fetch it before loading:

  ```bash
  curl -fsSL -o ~/job.yaml     https://raw.githubusercontent.com/Tausheef-Hassan/PID-Bangladesh-UploadBot/control-panel/toolforge/job.yaml
  toolforge jobs load ~/job.yaml
  ```

  `toolforge jobs load` reports a missing file as `ERROR: Unable to parse yaml
  file` — its `open()` sits inside the same `try` as the YAML parse and it
  catches bare `Exception` (`jobs_cli/cli.py:1016`). If you see that error, check
  the path exists before you go looking for a syntax problem.
- **`Procfile` order matters.** `toolforge webservice buildservice start` runs the
  *first* entry in the Procfile whatever it is called, so `web` must stay above
  `run-bot`. With `run-bot` first the webservice would launch the pipeline, which
  never binds port 8000, and the webservice would fail to come up.

### Control panel

A Flask app served at `https://<tool>.toolforge.org` shows whether the bot is
running or crashed, streams the live log, and drives the job. It runs in its own
pod and never imports the pipeline: it talks to the **Toolforge Jobs API**
through [`toolforge-weld`](https://pypi.org/project/toolforge-weld/) (the client
Wikimedia's own `toolforge` CLI is built on) and reads the files the bot leaves
in `$TOOL_DATA_DIR`.

| Control | Jobs API call |
|---|---|
| Run now | `POST …/jobs/pid-bot/restart` — "if the job is a cronjob, execute it right now" |
| Pause / Resume | `PATCH …/jobs/pid-bot` rewriting `schedule` |
| Stop | `DELETE …/jobs/pid-bot` |
| Status | `GET …/jobs/pid-bot` → `status.short` |

**Pause, not Stop, is the safe lever.** `main.py` writes every successful upload
to `PIDDateData` in a single batch edit at the very end of a run, so killing a
run mid-flight discards that run's registrations — those images get scraped,
OCR'd and translated again next run, then rejected as duplicates. Pause rewrites
the schedule to a date that never arrives (31 February), leaving the job
definition and any in-flight run untouched. The Stop button says all this before
you press it.

### Reviewing descriptions from the panel

Each upload's detail view can correct the English description and add topic
categories on Commons. The bot writes
`{{en|1=<translation>{{Auto-translated PID English description}}}}`; the
**Description is fine** button removes that marker and moves to the next file,
because once a person has verified it, it is no longer auto-translated. Plain
**Save** never touches the marker — only the button that says so does.

Categories work like HotCat. Every category the file is in is shown as a chip,
and typing offers suggestions fetched live from Commons — so a category that
appears in the list is one that exists, which is what stops the backlog filling
with red-linked typos. Suggestions are fetched server-side through
`/categories/suggest`, sharing the same two-minute cache as the rest of the
panel's Commons reads.

Template-driven categories appear greyed and cannot be removed: `{{Date-PID}}`
and `{{PD-BDGov-PID}}` call `Module:PIDCategoryHelper` and add
`Category:PID-BD images from <Month Year>` and
`Category:Bangladesh photographs taken on <date>` themselves. They are shown
because they are the categories someone would otherwise add again by hand — but
only topic categories are written back to the page.

Edits are attributed to **you**, not the bot, through Wikimedia OAuth. Set it up
once:

1. Propose a consumer at
   [Special:OAuthConsumerRegistration](https://meta.wikimedia.org/wiki/Special:OAuthConsumerRegistration/propose):

   | Field | Value |
   |---|---|
   | OAuth version | **OAuth 2.0**, client type **confidential** |
   | Callback | `https://<tool>.toolforge.org/oauth/callback` |
   | Grants | **Edit existing pages**, plus **Edit structured data** for captions |

   The callback is compared character for character, so it must match the tool
   URL exactly. Pointing the panel at a 1.0a consumer fails with
   `Wrong OAuth version, E012`.

   Registration happens at meta, but the **handshake runs on Commons** —
   `commons.wikimedia.org/w/rest.php/oauth2`. A consumer scoped to commonswiki
   gets a token from meta quite happily and is then refused by meta's own
   resource endpoint: *The authorization headers in your request are not valid
   for metawiki*. Applying the consumer to all projects instead would also
   work, but this tool only ever edits Commons, so the narrower scope is the
   right one to keep.

2. Once approved, store the consumer as **envvars**, which keeps it off NFS
   entirely:
   ```bash
   toolforge envvars create OAUTH_CONSUMER_KEY      # the Client ID, then Ctrl-D
   toolforge envvars create OAUTH_CONSUMER_SECRET   # the Client secret
   toolforge webservice buildservice restart
   ```
   Never pass a secret as a command-line argument — it lands in your shell
   history and is visible to other users of the same bastion.

   A `$TOOL_DATA_DIR/oauth.key` file (`KEY=VALUE` lines) is the fallback and the
   local-development path. If you use it, `chmod 600` it.

Without `oauth.key` the panel stays read-only for Commons and says so, rather
than falling back to the bot's own credentials.

`panel/wikitext.py` rewrites the page by counting braces, never by pattern
matching — `{{en|1=…}}` contains nested templates. It refuses to save anything
it cannot parse confidently, and rejects descriptions or category names
containing markup: leaving a page alone always beats writing a mangled one.

**Auth:** one sign-in for everything, through Wikimedia OAuth. Reads are public.
Commons edits need any signed-in account — they are published under that name.
Job controls (run, pause, stop, replacements, wayback retry) need an account
that has been given access, because OAuth proves who you are and never that you
may operate this tool.

Access is managed from **Access** in the panel, not from the bastion. The
username field suggests accounts that exist on Commons, so a misspelling cannot
be granted access it would never use. One envvar sets the root of trust;
everyone else is added from the web:

```bash
toolforge envvars create PANEL_OWNER      # your Wikimedia username
toolforge envvars create SECRET_KEY       # random, signs the session cookie
```

The owner is the only account that can grant or revoke, and the only one the
web UI cannot remove — so a maintainer whose session is stolen cannot lock the
owner out or promote anyone. Grants live in `maintainers.json` in
`$TOOL_DATA_DIR`, which the webservice writes itself; that file is a list of
public usernames, never key material, which is why it can sit on NFS when a
secret could not. Each entry records who granted it and when.

The list is re-read on every request, so revoking someone takes effect on their
next click. **No owner means the controls are off for everyone**, and an
unreadable `maintainers.json` falls back to the owner alone — it fails closed.

`SECRET_KEY` must be set and stable: it signs the session cookie, and both
gunicorn workers have to agree on it or people get signed out at random. A
`secret.key` file in `$TOOL_DATA_DIR` is the local-development fallback.

`/healthz` reports which source each secret came from, so a deployment can be
checked without exposing anything:

```json
{"ok": true, "oauth": "envvars", "secret_key": "envvars",
 "owner_set": true, "maintainers": 2, "tool_data_dir": true}
```

Values are `envvars`, `file` or `missing` — names only, never key material. If
this says `file` after running `toolforge envvars create`, the webservice has
not been restarted, or a stale key file is shadowing the envvar.

### Flagging copyright problems

`{{PD-BDGov-PID}}` covers Bangladesh government works. The backlog also contains
photographs of artwork, logos, screenshots and agency photos that it does not
cover, so the workbench can flag one without leaving the panel. Two tiers:

| Tier | What it writes | Who sees it |
|---|---|---|
| Add to the review list | `[[Category:PID files with copyright concerns]]` | only this tool's **Copyright flags** queue |
| Tag on Commons | `{{No permission since}}`, `{{No license since}}`, `{{No source since}}`, `{{Wrong license}}`, `{{Copyvio}}` | Commons maintainers act on these |

The review list is the safe default: nothing is nominated for deletion and the
flag is one category edit to undo. `{{Copyvio}}` is a speedy-deletion
nomination, so the panel will not send one until the filename is typed out.

The reason is always recorded in the edit summary, and a file already carrying a
flag cannot be flagged again — resolve the first one on Commons instead.

`Category:PID files with copyright concerns` must exist on Commons before the
first flag; the panel files into categories but never creates them. It is also
treated as a template-driven category, so editing topic categories cannot
silently remove a flag.

Deliberately not built: full deletion requests (`{{Delete}}` plus a DR subpage
plus the daily log listing) and uploader talk-page notifications. Both are
multi-edit workflows that leave a mess on Commons when half-applied.

### Keeping secrets private on Toolforge

`/data/project/<tool>` is **readable by every other tool on Toolforge** unless
you tighten permissions, and OAuth credentials have leaked this way before
([T286414](https://phabricator.wikimedia.org/T286414)). Prefer the envvars
service, which stores values outside the shared filesystem — only the tool's
code and its maintainers can read them, though the *names* are public:

```bash
toolforge envvars create SECRET_KEY             # paste, then Ctrl-D
toolforge envvars create PANEL_OWNER            # your Wikimedia username
toolforge envvars create OAUTH_CONSUMER_KEY
toolforge envvars create OAUTH_CONSUMER_SECRET
toolforge envvars list                          # names and values, as the tool
toolforge webservice buildservice restart       # pick up the new values
```

If you keep secrets as files instead, restrict both the files and the directory:

```bash
chmod 600 ~/oauth.key ~/secret.key ~/gemini.key ~/ia.key ~/JSON.json           ~/drive_token.json ~/user-password.py
chmod 750 ~                     # stop other tools listing your home
ls -l ~/*.key                   # expect -rw------- and the tool as owner
```

Neither approach hides a secret from the tool's own **maintainers** — anyone who
can `become` the tool can read anything it can. That is inherent to Toolforge;
if a key is exposed, revoke it rather than trying to hide it.

**The live log is public.** The panel serves `pid-bot.out` to anyone, so the bot
must never print key material. `credentials.py` used to log the first six
characters of the Internet Archive access key; it no longer does.

**No third-party requests:** htmx is vendored into `panel/static/`, and Bengali
text uses `local()` fonts via `unicode-range` rather than a font CDN — a
Wikimedia tool should not hand visitors' IP addresses to someone else.

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
| `src/run_state.py` | Per-run outcome records (`run_state.json`) that the panel reads |
| `panel/app.py` | Control panel: Jobs API proxy, auth, log tail |
| `panel/templates/` | Server-rendered views; htmx polls the two that change |
| `panel/static/panel.css` | Panel styling |
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
- **Two status signals:** The Jobs API knows whether the pod is alive; `run_state.json` knows what the run actually did. The panel prefers the API and falls back to the file when the API is unreachable, rather than reporting "not loaded" for what is really a control-plane outage.
