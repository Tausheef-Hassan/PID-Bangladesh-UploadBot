# Panel redesign part 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the control panel look like part of Commons (Codex), speak plain language, work on a phone, and let maintainers fix a wrong name on a file and have the bot remember it.

**Architecture:** Flask + Jinja + htmx, as today. The Codex CSS and design tokens are copied into `panel/static/codex/`. `panel.css` is re-themed onto Codex tokens, and the old terminal styling is deleted. The file page (`/file/<title>`) becomes the step-by-step view: Wikidata matches are looked up live from the page's Bengali with `src.name_resolver.resolve_names`. Name corrections become a table backed by the same `translation_replacements.tsv`, whose line format gains optional `user` and `date` fields.

**Tech Stack:** Python 3, Flask, Jinja2, htmx (vendored), Codex CSS 2.x (vendored), PyMySQL (already added), `test_pipeline.py` self-check runner.

**Spec:** `docs/superpowers/specs/2026-09-26-panel-redesign-part1-design.md`

## Deviations from the spec (found while reading the code)

1. **The step-by-step view goes in `/file/<title>`, not `/upload/<id>`.** Uploads already links every file to `/file/<title>`, which has source vs crop, Bengali, the English editor, categories and flags, and works for the whole backlog. `/upload/<id>` is linked from nowhere. It becomes a redirect to `/file/`.
2. **Name matches are looked up live on the file page** with `resolve_names(bengali)`, not stored by the bot. This works for old files too, and `main.py` does not change.
3. **Uploads keeps its existing queues.** "Needs review" is the spec's "Needs a look", already implemented with one Commons search. It gets a plain label rather than a rebuild.

## Global Constraints

- Nothing loads from outside Toolforge: Codex files are copied into `panel/static/codex/`, never linked from a CDN.
- Tabs, in order: Overview · Uploads · Name corrections · Queue · Access (Access only when `is_maintainer`).
- Wording: never show "stderr", "cronjob", "Jobs API", "Not loaded on Toolforge" to users. Use "Errors", "schedule/runs every hour", "Toolforge", "Stopped".
- API-down message text: `The panel can't reach Toolforge right now, so the buttons won't work.` followed by `Reason: <error>`.
- Name-corrections line format: `bengali|||english|||user|||YYYY-MM-DD`, last two fields optional. The bot reads only the first two.
- Writing name corrections is maintainer-only (it changes what the bot uploads). Reading is public.
- Works at 360px wide.
- Every run of `python test_pipeline.py` must end with `N checks passed`.

## Review Focus

- **API down while the bot's own record says "running"** → the status box must still say Toolforge is unreachable and show no buttons, not Pause/Stop that will 500. (Test in Task 1.)
- **A correction whose Bengali already exists** → "remember" must not add a duplicate line. The existing English wins, and the flash says it was skipped. (Test in Task 3.)
- **English text containing `|||`** typed into the table → it would corrupt the line format. It must be refused. (Test in Task 3.)
- **Bengali with HTML-special characters** (`<`, `&`) when highlighting matches → it must be escaped, never injected. (Test in Task 4.)
- **Replica unreachable on the file page** → the page still renders with no highlights. (Test in Task 4.)

---

### Task 1: Status box that says what's happening, with the real reason when Toolforge is unreachable

**Files:**
- Modify: `panel/app.py` (`fetch_job` ~line 146, `page_context` ~line 421, `pause` ~line 548, `STATE_LABELS` ~line 36)
- Modify: `panel/templates/_dashboard.html` (whole file)
- Test: `test_pipeline.py`

**Interfaces:**
- Produces: `fetch_job() -> (job | None, reachable: bool, error: str)`; `status_view(state: str, paused: bool, reachable: bool, latest: dict | None) -> dict(tone, sentence, actions: list[str])` where tone ∈ `success|notice|warning|error` and actions ⊆ `start|run|pause|resume|stop`. `page_context()` adds keys `status` (that dict) and `api_error` (str).

- [ ] **Step 1: Write the failing tests** (append before `if __name__ == "__main__":`)

```python
def test_status_box_offers_only_what_makes_sense():
    sv = panel_app.status_view
    assert sv("no-job", False, True, None)["actions"] == ["start"]
    assert sv("running", False, True, {"status": "running", "uploaded": 3})["actions"] == ["pause", "stop"]
    assert "3 photos" in sv("running", False, True, {"status": "running", "uploaded": 3})["sentence"]
    assert sv("succeeded", True, True, None)["actions"] == ["run", "resume", "stop"]
    assert sv("succeeded", False, True, None)["actions"] == ["run", "pause", "stop"]
    assert sv("failed", False, True, None)["tone"] == "error"


def test_unreachable_toolforge_shows_the_reason_and_no_buttons():
    """The bot's record can say 'running' while the API is down; buttons would 500."""
    down = panel_app.status_view("running", False, False, {"status": "running"})
    assert down["actions"] == [] and down["tone"] == "error"
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
        def boom():
            raise RuntimeError("certificate expired")
        panel_app.jobs_api = boom
        page = client.get("/").get_data(as_text=True)
        assert "can't reach Toolforge" in page and "certificate expired" in page, page[:500]
        assert "Jobs API" not in page and "Stderr" not in page
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -c "import test_pipeline as t; t.test_status_box_offers_only_what_makes_sense()"`
Expected: `AttributeError: module 'panel.app' has no attribute 'status_view'`

- [ ] **Step 3: Implement.** In `panel/app.py`, replace `fetch_job` with:

```python
def fetch_job():
    """Returns (job, reachable, error).

    `reachable` separates "the API told us there is no such job" from "we could
    not ask". `error` is the reason we could not ask, shown on the page, because
    "unreachable" alone sends whoever reads it hunting in the wrong place.
    """
    try:
        return jobs_api().get(_job_url(), display_messages=False).get('job'), True, ''
    except HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None, True, ''
        app.logger.warning('Jobs API error: %r', e)
        return None, False, str(e)
    except Exception as e:
        app.logger.warning('Jobs API unreachable: %r', e)
        return None, False, str(e)


def status_view(state, paused, reachable, latest):
    """One sentence and the buttons that make sense, for the status box."""
    if not reachable:
        return {'tone': 'error', 'actions': [],
                'sentence': "The panel can't reach Toolforge right now, so the buttons won't work."}
    if state == 'no-job':
        return {'tone': 'notice', 'actions': ['start'],
                'sentence': 'Stopped. Nothing will run until you start it.'}
    if state in ('running', 'pending'):
        n = (latest or {}).get('uploaded', 0) if (latest or {}).get('status') == 'running' else 0
        so_far = f' {n} photo{"" if n == 1 else "s"} uploaded so far.' if n else ''
        return {'tone': 'success', 'actions': ['pause', 'stop'],
                'sentence': 'Running now.' + so_far}
    if paused:
        return {'tone': 'warning', 'actions': ['run', 'resume', 'stop'],
                'sentence': 'Paused. Nothing runs on schedule until you resume it.'}
    if state == 'failed':
        return {'tone': 'error', 'actions': ['run', 'pause', 'stop'],
                'sentence': 'The last run failed. The errors are in the technical log below.'}
    return {'tone': 'success', 'actions': ['run', 'pause', 'stop'],
            'sentence': 'Waiting for the next run. The last one finished cleanly.'}
```

In `pause()`, change `job, _ = fetch_job()` to `job, _, _ = fetch_job()`. In `page_context()`, change the first line to `job, api_reachable, api_error = fetch_job()`, and add these to the returned dict:

```python
        'api_error': api_error,
        'status': status_view(state, schedule == NEVER_FIRES, api_reachable, latest),
```

Change `STATE_LABELS`:

```python
STATE_LABELS = {
    'running': 'Running',
    'pending': 'Starting',
    'succeeded': 'Waiting',
    'failed': 'Last run failed',
    'unknown': 'Status unavailable',
    'no-job': 'Stopped',
    'api-down': "Can't reach Toolforge",
}
```

Replace `panel/templates/_dashboard.html` with:

```html
{# Status box: one sentence, then only the buttons that make sense right now. #}
<div id="dashboard" hx-get="{{ url_for('partial_dashboard') }}"
     hx-trigger="every 5s" hx-swap="outerHTML">

  <div class="cdx-message cdx-message--block cdx-message--{{ status.tone }} statusbox"
       role="status" aria-live="polite">
    <span class="cdx-message__icon"></span>
    <div class="cdx-message__content">
      <p class="status-sentence">{{ status.sentence }}</p>
      {% if api_error %}<p class="status-meta">Reason: {{ api_error }}</p>{% endif %}
      <p class="status-meta">
        {% if started_ago %}Last run started {{ started_ago }}{% if duration %}, running for {{ duration }}{% endif %}.
        {% else %}No run recorded yet.{% endif %}
        {% if next_in and not paused %}Next run in {{ next_in }}.{% endif %}
      </p>

      {% if is_maintainer and status.actions %}
        <div class="status-actions">
          {% for a in status.actions %}
            {% if a == 'start' %}
              <form method="post" action="{{ url_for('start') }}"><button type="submit" class="primary">Start job</button></form>
            {% elif a == 'run' %}
              <form method="post" action="{{ url_for('run_now') }}"><button type="submit" class="primary">Run now</button></form>
            {% elif a == 'pause' %}
              <form method="post" action="{{ url_for('pause') }}"><button type="submit">Pause schedule</button></form>
            {% elif a == 'resume' %}
              <form method="post" action="{{ url_for('resume') }}"><button type="submit" class="primary">Resume schedule</button></form>
            {% elif a == 'stop' %}
              <form method="post" action="{{ url_for('stop') }}"
                    onsubmit="return confirm('Stop the job and end the run in progress?\n\nPhotos already uploaded in this run are only recorded at the end, so they will be processed again next run and skipped as duplicates.\n\nTo quiet the bot without losing work, use Pause schedule instead.')">
                <button type="submit" class="danger">Stop job</button>
              </form>
            {% endif %}
          {% endfor %}
        </div>
      {% elif not is_maintainer %}
        <p class="status-meta">
          {% if not controls_enabled %}Controls are off: no maintainers are set up.
          {% elif not wiki_user %}<a href="{{ url_for('oauth_start') }}">Sign in</a> to run the job.
          {% else %}{{ wiki_user }} is not a maintainer of this tool.{% endif %}
        </p>
      {% endif %}
    </div>
  </div>

  {% if latest %}
    <dl class="counts">
      <div><dt>Uploaded</dt><dd>{{ latest.uploaded }}</dd></div>
      <div><dt>Already on Commons</dt><dd>{{ latest.duplicates }}</dd></div>
      <div><dt>Failed</dt><dd class="{% if latest.failed %}is-alert{% endif %}">{{ latest.failed }}</dd></div>
      <div><dt>Photos seen</dt><dd>{{ latest.scraped }}</dd></div>
    </dl>
  {% endif %}
  {% if latest and latest.error %}
    <p class="cdx-message cdx-message--block cdx-message--error"><span class="cdx-message__icon"></span>
      <span class="cdx-message__content">{{ latest.error }}</span></p>
  {% endif %}
</div>
```

- [ ] **Step 4: Run the whole suite.** `test_a_stopped_job_can_be_started_again_from_the_web` (still asserts "Start job") and `test_read_only_views_stay_public` (asserts no "Run now" for signed-out visitors) must still pass.

Run: `python test_pipeline.py 2>&1 | tail -3`
Expected: `N checks passed` (78)

- [ ] **Step 5: Commit**

```bash
git add panel/app.py panel/templates/_dashboard.html test_pipeline.py
git commit -m "Say what the bot is doing in one sentence, and why Toolforge is unreachable"
```

---

### Task 2: Codex look, tabs, phone layout

**Files:**
- Create: `panel/static/codex/codex.style.css`, `panel/static/codex/theme-wikimedia-ui.css`, `panel/static/codex/VERSION`
- Modify: `panel/templates/base.html` (whole file)
- Modify: `panel/static/panel.css` (`:root` block, body, buttons, inputs, nav; delete the "Terminal header" section and the duplicated log-toolbar block)
- Test: `test_pipeline.py`

**Interfaces:**
- Produces: CSS classes later tasks use: `.tabs`, `.tab`, `.tab.active`, `.statusbox`, `.status-sentence`, `.status-meta`, `.status-actions`, `.counts`, `button.primary`, `button.danger`, `.strip`, `.wd` (Wikidata highlight), `.fixpop`, `.fixchips`.

- [ ] **Step 1: Write the failing test**

```python
def test_every_page_has_the_tabs_and_codex():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")
        for route in ("/", "/uploads", "/queue", "/replacements"):
            body = client.get(route).get_data(as_text=True)
            assert 'codex/codex.style.css' in body, route
            for label in ("Overview", "Uploads", "Name corrections", "Queue"):
                assert f">{label}</a>" in body, (route, label)
            assert ">Access</a>" not in body, "Access tab shown to a visitor"
            assert "https://" not in body.split("<body")[0], "external asset in <head>"
```

Also update `test_navbar...` (line ~660: `assert 'class="nav"' in body`) to `assert 'class="tabs"' in body`.

- [ ] **Step 2: Run to verify it fails**

Run: `python -c "import test_pipeline as t; t.test_every_page_has_the_tabs_and_codex()"`
Expected: AssertionError on `codex/codex.style.css`

- [ ] **Step 3: Vendor Codex (pinned)**

```bash
V=$(curl -s "https://data.jsdelivr.com/v1/packages/npm/@wikimedia/codex/resolved?specifier=2" | python -c "import json,sys;print(json.load(sys.stdin)['version'])")
T=$(curl -s "https://data.jsdelivr.com/v1/packages/npm/@wikimedia/codex-design-tokens/resolved?specifier=2" | python -c "import json,sys;print(json.load(sys.stdin)['version'])")
mkdir -p panel/static/codex
curl -sfL "https://cdn.jsdelivr.net/npm/@wikimedia/codex@$V/dist/codex.style.css" -o panel/static/codex/codex.style.css
curl -sfL "https://cdn.jsdelivr.net/npm/@wikimedia/codex-design-tokens@$T/dist/theme-wikimedia-ui.css" -o panel/static/codex/theme-wikimedia-ui.css
printf 'Copied from npm, served locally (Toolforge tools must not load third-party URLs).\n@wikimedia/codex %s\n@wikimedia/codex-design-tokens %s\n' "$V" "$T" > panel/static/codex/VERSION
grep -c "cdx-message" panel/static/codex/codex.style.css   # expect > 0
grep -c "color-progressive" panel/static/codex/theme-wikimedia-ui.css  # expect > 0
```

- [ ] **Step 4: Replace `panel/templates/base.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}PID upload bot{% endblock %}</title>
<link rel="stylesheet" href="{{ url_for('static', filename='codex/theme-wikimedia-ui.css') }}">
<link rel="stylesheet" href="{{ url_for('static', filename='codex/codex.style.css') }}">
<link rel="stylesheet" href="{{ url_for('static', filename='panel.css') }}">
<script src="{{ url_for('static', filename='htmx.min.js') }}" defer></script>
</head>
<body>

<header class="masthead">
  <div class="masthead-row">
    <a class="brand" href="{{ url_for('index') }}">PID upload bot</a>
    <div class="account">
      {% if wiki_user %}
        <span class="account-name" {% if is_maintainer %}title="Maintainer: can run the job"{% endif %}>{{ wiki_user }}</span>
        <form method="post" action="{{ url_for('oauth_logout') }}">
          <button type="submit" class="quiet small">Sign out</button>
        </form>
      {% elif wiki_configured %}
        <a href="{{ url_for('oauth_start') }}">Sign in with Wikimedia</a>
      {% else %}
        <span class="account-name">Read-only</span>
      {% endif %}
    </div>
  </div>
  <nav class="tabs" aria-label="Pages">
    {% set here = request.endpoint %}
    {% for endpoint, label, also in [('index', 'Overview', []),
                                     ('uploads', 'Uploads', ['file_detail']),
                                     ('replacements', 'Name corrections', []),
                                     ('queue', 'Queue', [])]
           + ([('admin', 'Access', [])] if is_maintainer else []) %}
      <a class="tab {% if here == endpoint or here in also %}active{% endif %}"
         {% if here == endpoint or here in also %}aria-current="page"{% endif %}
         href="{{ url_for(endpoint) }}">{{ label }}</a>
    {% endfor %}
  </nav>
</header>

{% with messages = get_flashed_messages() %}
  {% if messages %}
    <div class="wrap">
      {% for m in messages %}
        <div class="cdx-message cdx-message--block cdx-message--notice notice" role="status">
          <span class="cdx-message__icon"></span><div class="cdx-message__content">{{ m }}</div>
        </div>
      {% endfor %}
    </div>
  {% endif %}
{% endwith %}

<main class="wrap">
{% block content %}{% endblock %}
</main>

</body>
</html>
```

- [ ] **Step 5: Re-theme `panel/static/panel.css`**

5a. Replace the header comment, `@import`, and `:root` block (lines 1–41) with:

```css
/* panel.css
 *
 * Layout and the few pieces Codex has no component for. Colours, type and
 * spacing come from the Codex design tokens (codex/theme-wikimedia-ui.css), so
 * the panel reads as part of Commons. The var() fallbacks are the Wikimedia UI
 * values, for the odd token a future Codex renames.
 */

@import url('fonts.css');

:root {
  --bg:          var(--background-color-base, #fff);
  --surface:     var(--background-color-base, #fff);
  --surface-alt: var(--background-color-interactive-subtle, #f8f9fa);
  --border:      var(--border-color-subtle, #c8ccd1);
  --border-strong: var(--border-color-base, #a2a9b1);
  --text:        var(--color-base, #202122);
  --muted:       var(--color-subtle, #54595d);
  --accent:      var(--color-progressive, #36c);
  --accent-hover: var(--color-progressive--hover, #3056a9);

  --ok-bg:   var(--background-color-success-subtle, #dff2eb);  --ok-text:   var(--color-success, #177860);
  --bad-bg:  var(--background-color-error-subtle, #ffe9e5);    --bad-text:  var(--color-error, #bf3c2c);
  --warn-bg: var(--background-color-warning-subtle, #fdf2d5);  --warn-text: var(--color-warning, #886425);

  --shadow: none;
  --radius: var(--border-radius-base, 2px);
  --radius-sm: var(--border-radius-base, 2px);

  --gutter: clamp(1rem, 4vw, 2.5rem);
  --sans: var(--font-family-system-sans, -apple-system, 'Segoe UI', Roboto, Lato, Helvetica, Arial, sans-serif);
  --mono: ui-monospace, 'Cascadia Mono', Menlo, Consolas, monospace;
  --bn: NotoSansBengali, 'Nirmala UI', sans-serif;
}
```

5b. Replace the `.card` rule with (flat Commons sections, no shadows):

```css
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 1rem 1.25rem;
  margin-top: 1rem;
}
```

5c. Replace the `button { … }` rule and the `button:hover`, `button.quiet`, `button.danger` rules (lines ~166–183) with Codex-style buttons:

```css
button {
  font: inherit; font-size: 0.875rem; font-weight: 700;
  min-height: 32px; padding: 0 12px;
  color: var(--text); background: var(--surface-alt);
  border: 1px solid var(--border-strong); border-radius: var(--radius);
  cursor: pointer;
}
button:hover { background: var(--background-color-interactive, #eaecf0); }
button:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
button.primary { color: #fff; background: var(--accent); border-color: var(--accent); }
button.primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
button.quiet { background: transparent; border-color: transparent; color: var(--accent); }
button.quiet:hover { background: var(--surface-alt); }
button.danger { color: #fff; background: var(--color-destructive, #d73333); border-color: var(--color-destructive, #d73333); }
button.danger:hover { background: var(--color-destructive--hover, #b32424); }
```

5d. Replace the `input, textarea { … }` rule and its `:focus` rule with:

```css
input, textarea, select {
  font: inherit; font-size: 0.9rem; color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border-strong); border-radius: var(--radius);
  padding: 6px 8px; min-height: 32px;
}
input:focus, textarea:focus, select:focus {
  border-color: var(--accent); outline: none;
  box-shadow: inset 0 0 0 1px var(--accent);
}
```

5e. Delete the whole `/* ── Navbar ─` section (the `.nav`, `.nav-inner`, `.brand`, `.routes`, `.route*`, `.account*` rules) and the `/* Small screens` block that follows it. In their place add:

```css
/* ── Masthead and tabs ───────────────────────────────────────────────── */
.masthead { padding: 0.75rem var(--gutter) 0; border-bottom: 1px solid var(--border-strong); }
.masthead-row { display: flex; align-items: center; gap: 1rem; }
.brand { font-family: 'Linux Libertine', Georgia, Times, serif; font-size: 1.5rem; color: var(--text); text-decoration: none; }
.account { margin-left: auto; display: flex; align-items: center; gap: 0.5rem; font-size: 0.875rem; }
.account form { margin: 0; }
.account-name { color: var(--muted); }
.tabs { display: flex; gap: 1.25rem; margin-top: 0.75rem; overflow-x: auto; scrollbar-width: none; }
.tabs::-webkit-scrollbar { display: none; }
.tab { padding: 0.4rem 0 0.5rem; white-space: nowrap; font-size: 0.875rem; color: var(--accent); text-decoration: none; border-bottom: 2px solid transparent; }
.tab:hover { color: var(--accent-hover); border-bottom-color: var(--border-strong); }
.tab.active { color: var(--text); font-weight: 700; border-bottom-color: var(--accent); }
.notice { margin-top: 0.75rem; }
button.small { font-size: 0.8125rem; min-height: 28px; padding: 0 8px; }

/* ── Status box ─────────────────────────────────────────────────────── */
.statusbox { margin-top: 1rem; }
.status-sentence { margin: 0; font-size: 1.05rem; font-weight: 700; }
.status-meta { margin: 0.25rem 0 0; font-size: 0.875rem; color: var(--muted); }
.status-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.75rem; }
.status-actions form { margin: 0; }
.counts { display: grid; grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr)); gap: 0.5rem; margin: 1rem 0 0; }
.counts div { border: 1px solid var(--border); padding: 0.5rem 0.75rem; }
.counts dt { font-size: 0.8125rem; color: var(--muted); }
.counts dd { margin: 0; font-size: 1.4rem; font-weight: 700; }
.counts dd.is-alert { color: var(--bad-text); }

/* ── Recent uploads strip ───────────────────────────────────────────── */
.strip { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 0.5rem; }
.strip a { display: block; border: 1px solid var(--border); }
.strip img { display: block; width: 100%; height: 90px; object-fit: cover; }

/* ── Names on the file page ─────────────────────────────────────────── */
.wd { background: var(--ok-bg); border-bottom: 2px solid var(--ok-text); color: inherit; }
.wdlist { margin: 0.4rem 0 0; padding: 0; list-style: none; font-size: 0.875rem; }
.wdlist li { margin: 0.15rem 0; }
.fixpop { position: absolute; z-index: 10; max-width: min(22rem, calc(100vw - 2rem));
          background: var(--surface); border: 1px solid var(--border-strong);
          box-shadow: 0 2px 6px rgba(0,0,0,.2); padding: 0.75rem; display: flex; flex-direction: column; gap: 0.4rem; }
.fixpop[hidden] { display: none; }
.fixchips { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0; padding: 0; list-style: none; }
.fixchips li { background: var(--surface-alt); border: 1px solid var(--border); padding: 0.1rem 0.5rem; font-size: 0.875rem; }
.bn-source { position: relative; }

@media (max-width: 640px) {
  .brand { font-size: 1.2rem; }
  .card { padding: 0.75rem; }
  .grid { grid-template-columns: repeat(2, 1fr) !important; }
  .compare { grid-template-columns: 1fr !important; }
}
```

5f. Delete the `/* ── Terminal header ─` section through the second duplicated log block: every rule from `.card.terminal` down to and including the second `.ln.head` rule (`statusbar`, `.statusbar *`, `.statusmeta`, `.controls*`, `.countbar`, `.count b*`, `.inline-warn`, and the second copies of `.toolbar.filters`, `.segmented`, `.seg*`, `.filterbox`, `.check`, `.link.small`, `.logmeta`, `.logempty`, `#log.reading`, `.ln*`).

- [ ] **Step 6: Run the suite**

Run: `python test_pipeline.py 2>&1 | tail -3`
Expected: `N checks passed`

- [ ] **Step 7: Look at it.** Run `python scripts/preview_panel.py`, open http://127.0.0.1:5173 at desktop width and at 360px (Chrome device toolbar). Check that the tabs scroll sideways on the phone, the Codex message box has its icon, and buttons are 2px-radius Codex style. Fix any CSS rule still referencing a deleted class.

- [ ] **Step 8: Commit**

```bash
git add panel/static/codex panel/templates/base.html panel/static/panel.css test_pipeline.py
git commit -m "Give the panel the Commons look: Codex, a row of tabs, phone layout"
```

---

### Task 3: Name corrections as a table the bot and panel both understand

**Files:**
- Modify: `src/translator.py` (`load_translation_replacements`)
- Create: `panel/corrections.py`
- Modify: `panel/app.py` (`replacements`, `save_replacements` routes ~line 583)
- Modify: `panel/templates/replacements.html` (whole file)
- Test: `test_pipeline.py` (update `test_what_the_panel_saves_the_bot_loads`, add new tests)

**Interfaces:**
- Produces: `translator.parse_replacement_line(line: str) -> tuple[str, str, str, str] | None` returning `(bengali, english, user, date)`. `panel.corrections.rows(path) -> list[dict(bn, en, user, date)]`, `add(path, bn, en, user) -> bool` (False if `bn` already present), `update(path, old_bn, bn, en, user) -> bool`, `delete(path, bn) -> bool`. All raise `ValueError` if a field contains `|||` or a newline, or if `bn` or `en` is empty.

- [ ] **Step 1: Write the failing tests.** Replace `test_what_the_panel_saves_the_bot_loads` with the version below, and add the others:

```python
def test_what_the_panel_saves_the_bot_loads():
    """The round trip the operator is relying on when they fix a wrong name."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", granted=["Sadi"], signed_in_as="Sadi")
        real = config.REPLACEMENTS_PATH
        config.REPLACEMENTS_PATH = os.path.join(tmp, "translation_replacements.tsv")
        try:
            r = client.post("/replacements", data={
                "action": "add", "bn": "মোঃ নজরুল ইসলাম হীরু", "en": "Shaikh Faridul Islam"})
            assert r.status_code == 302, r.status_code
            pairs = translator.load_translation_replacements()
        finally:
            config.REPLACEMENTS_PATH = real
        assert ("মোঃ নজরুল ইসলাম হীরু", "Shaikh Faridul Islam") in pairs, pairs


def test_correction_metadata_never_leaks_into_the_translation():
    assert translator.parse_replacement_line("ক খ|||Ka Kha|||Sadi|||2026-09-26") == \
        ("ক খ", "Ka Kha", "Sadi", "2026-09-26")
    assert translator.parse_replacement_line("ক খ|||Ka Kha") == ("ক খ", "Ka Kha", "", "")
    assert translator.parse_replacement_line("# note") is None
    assert translator.parse_replacement_line("no separator") is None


def test_corrections_add_edit_delete_keep_comments_and_refuse_duplicates():
    from panel import corrections
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "t.tsv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# keep me\nক খ|||Ka Kha\n")
        assert corrections.add(p, "গ ঘ", "Ga Gha", "Sadi") is True
        assert corrections.add(p, "ক খ", "Other", "Sadi") is False, "duplicate Bengali added"
        assert corrections.update(p, "ক খ", "ক খ", "Ka Kha Fixed", "RIFAT712") is True
        assert corrections.delete(p, "গ ঘ") is True
        text = open(p, encoding="utf-8").read()
        assert text.startswith("# keep me\n"), text
        assert [(r["bn"], r["en"], r["user"]) for r in corrections.rows(p)] == \
            [("ক খ", "Ka Kha Fixed", "RIFAT712")]
        for bad in ("a|||b", "two\nlines", ""):
            try:
                corrections.add(p, "চ ছ", bad, "Sadi")
                raise AssertionError(f"accepted {bad!r}")
            except ValueError:
                pass


def test_only_maintainers_change_name_corrections():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RandomPasserby")
        r = client.post("/replacements", data={"action": "add", "bn": "ক খ", "en": "X"})
        assert r.status_code == 403
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -c "import test_pipeline as t; t.test_correction_metadata_never_leaks_into_the_translation()"`
Expected: `AttributeError: ... has no attribute 'parse_replacement_line'`

- [ ] **Step 3: Implement the parser in `src/translator.py`.** Replace `load_translation_replacements` with:

```python
SEPARATOR = '|||'


def parse_replacement_line(line):
    """(bengali, english, user, date) from one table line, or None.

    Format: bengali|||english|||user|||YYYY-MM-DD — the last two are optional
    and only there so the panel can show who added a correction and when.
    """
    line = line.rstrip('\r\n')
    if not line.strip() or line.lstrip().startswith('#') or SEPARATOR not in line:
        return None
    parts = [p.strip() for p in line.split(SEPARATOR)] + ['', '']
    if not parts[0]:
        return None
    return parts[0], parts[1], parts[2], parts[3]


def load_translation_replacements():
    """Load find/replace pairs from translation_replacements.tsv in $TOOL_DATA_DIR."""
    replacements = []
    tsv_path = config.REPLACEMENTS_PATH
    if not os.path.exists(tsv_path):
        logger.info("No translation_replacements.tsv found, skipping pre-translation replacements")
        return replacements
    try:
        with open(tsv_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                parsed = parse_replacement_line(line)
                if parsed is None:
                    if line.strip() and not line.lstrip().startswith('#'):
                        logger.warning(
                            f"translation_replacements.tsv line {line_num}: missing '{SEPARATOR}' separator, skipping: {line!r}")
                    continue
                replacements.append(parsed[:2])
        logger.info(f"Loaded {len(replacements)} translation replacements from {tsv_path}")
    except Exception as e:
        logger.error(f"Error loading translation_replacements.tsv: {e}")
    return replacements
```

- [ ] **Step 4: Create `panel/corrections.py`**

```python
# corrections.py
# The name-corrections table (translation_replacements.tsv) as rows the panel
# can list, add, edit and delete. Comment lines are preserved; rows are keyed
# by their Bengali, which the bot matches on, so two rows can't share one.

from datetime import date
from pathlib import Path

from src.translator import SEPARATOR, parse_replacement_line


def _check(*fields):
    for f in fields:
        if not f or not f.strip() or SEPARATOR in f or '\n' in f or '\r' in f:
            raise ValueError(f'Not a usable value: {f!r}')


def _lines(path):
    try:
        return Path(path).read_text(encoding='utf-8').splitlines()
    except OSError:
        return []


def _write(path, lines):
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _line(bn, en, user):
    return SEPARATOR.join([bn.strip(), en.strip(), user, date.today().isoformat()])


def rows(path):
    out = []
    for line in _lines(path):
        parsed = parse_replacement_line(line)
        if parsed:
            out.append(dict(zip(('bn', 'en', 'user', 'date'), parsed)))
    return out


def add(path, bn, en, user):
    """Append a row. False, and nothing written, if that Bengali is already there."""
    _check(bn, en)
    if any(r['bn'] == bn.strip() for r in rows(path)):
        return False
    _write(path, _lines(path) + [_line(bn, en, user)])
    return True


def update(path, old_bn, bn, en, user):
    _check(bn, en)
    lines, found = _lines(path), False
    for i, line in enumerate(lines):
        parsed = parse_replacement_line(line)
        if parsed and parsed[0] == old_bn:
            lines[i], found = _line(bn, en, user), True
    if found:
        _write(path, lines)
    return found


def delete(path, bn):
    lines = _lines(path)
    kept = [l for l in lines if (parse_replacement_line(l) or ('',))[0] != bn]
    if len(kept) == len(lines):
        return False
    _write(path, kept)
    return True
```

- [ ] **Step 5: Routes in `panel/app.py`.** Add `from panel import commons, corrections, wikiauth, wikitext` (extend the existing import). Replace the two replacements routes with:

```python
@app.get('/replacements')
def replacements():
    return render_template('replacements.html',
                           rows=corrections.rows(config.REPLACEMENTS_PATH))


@app.post('/replacements')
@requires_maintainer
def save_replacements():
    path, form = config.REPLACEMENTS_PATH, request.form
    action = form.get('action')
    try:
        if action == 'add':
            ok = corrections.add(path, form.get('bn', ''), form.get('en', ''), current_user())
            flash('Added. It applies from the next run.' if ok
                  else 'That Bengali already has a correction. Edit it instead.')
        elif action == 'update':
            corrections.update(path, form.get('old_bn', ''), form.get('bn', ''),
                               form.get('en', ''), current_user())
            flash('Saved. It applies from the next run.')
        elif action == 'delete':
            corrections.delete(path, form.get('bn', ''))
            flash('Deleted.')
        else:
            abort(400)
    except ValueError:
        flash("Not saved: both fields are needed, on one line, without '|||'.")
    return redirect(url_for('replacements'))
```

- [ ] **Step 6: Replace `panel/templates/replacements.html`**

```html
{% extends 'base.html' %}
{% block title %}Name corrections — PID upload bot{% endblock %}
{% block content %}
<section class="card">
  <h2>Name corrections</h2>
  <p class="prose">When the Bengali caption contains the text on the left, the bot
    puts the English on the right in its place before translating. Use it for
    names that Wikidata doesn't have, or has spelled differently.</p>

  {% if is_maintainer %}
    <form method="post" class="corr-add">
      <input type="hidden" name="action" value="add">
      <input name="bn" lang="bn" placeholder="Bengali, as in the caption" required aria-label="Bengali">
      <input name="en" placeholder="Correct English" required aria-label="English">
      <button type="submit" class="primary">Add correction</button>
    </form>
  {% endif %}

  <input type="search" id="corrfilter" placeholder="Search corrections" aria-label="Search corrections" class="corr-filter">

  {% if rows %}
    <div class="cdx-table"><div class="cdx-table__table-wrapper">
    <table class="cdx-table__table" id="corrtable">
      <thead><tr><th>Bengali</th><th>English</th><th>Added by</th>{% if is_maintainer %}<th></th>{% endif %}</tr></thead>
      <tbody>
        {% for r in rows %}
          <tr>
            {% if is_maintainer %}
              <td colspan="4">
                <form method="post" class="corr-row">
                  <input type="hidden" name="old_bn" value="{{ r.bn }}">
                  <input name="bn" value="{{ r.bn }}" lang="bn" aria-label="Bengali">
                  <input name="en" value="{{ r.en }}" aria-label="English">
                  <span class="hint">{{ r.user or 'unknown' }}{% if r.date %}, {{ r.date }}{% endif %}</span>
                  <button type="submit" name="action" value="update" class="small">Save</button>
                  <button type="submit" name="action" value="delete" class="quiet small"
                          onclick="return confirm('Delete this correction?')">Delete</button>
                </form>
              </td>
            {% else %}
              <td lang="bn" class="bn">{{ r.bn }}</td><td>{{ r.en }}</td>
              <td class="hint">{{ r.user or 'unknown' }}{% if r.date %}, {{ r.date }}{% endif %}</td>
            {% endif %}
          </tr>
        {% endfor %}
      </tbody>
    </table>
    </div></div>
  {% else %}
    <p class="prose">No corrections yet. Add one above, or tick "remember" when you fix a name on a file.</p>
  {% endif %}

  {% if not is_maintainer %}
    <p class="hint">{% if not controls_enabled %}Editing is off: no maintainers are set up.
      {% elif not wiki_user %}Sign in with your Wikimedia account to edit these.
      {% else %}{{ wiki_user }} is not a maintainer of this tool.{% endif %}</p>
  {% endif %}
</section>
<script>
  // Filters as you type; the table is small enough to live in the page.
  document.getElementById('corrfilter').addEventListener('input', function () {
    var q = this.value.trim().toLowerCase();
    document.querySelectorAll('#corrtable tbody tr').forEach(function (tr) {
      var text = Array.prototype.map.call(tr.querySelectorAll('input, td'),
        function (el) { return el.value || el.textContent; }).join(' ').toLowerCase();
      tr.hidden = q && text.indexOf(q) === -1;
    });
  });
</script>
{% endblock %}
```

Add to `panel.css`:

```css
.corr-add, .corr-row { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin: 0; }
.corr-add { margin-bottom: 1rem; }
.corr-add input, .corr-row input { flex: 1 1 12rem; }
.corr-row input[lang="bn"], .corr-add input[lang="bn"] { font-family: var(--bn); }
.corr-filter { width: 100%; max-width: 24rem; margin-bottom: 0.75rem; }
```

- [ ] **Step 7: Run the suite**

Run: `python test_pipeline.py 2>&1 | tail -3`
Expected: `N checks passed`

- [ ] **Step 8: Commit**

```bash
git add src/translator.py panel/corrections.py panel/app.py panel/templates/replacements.html panel/static/panel.css test_pipeline.py
git commit -m "Turn name corrections into a table that records who added each one"
```

---

### Task 4: The file page shows Wikidata's names and lets you fix the rest

**Files:**
- Modify: `panel/app.py` (`file_detail` ~line 900, `save_file` ~line 970; add `highlight_names`)
- Modify: `panel/templates/file.html` (Bengali block, the step headings, add the popover, the remember checkbox, JS)
- Test: `test_pipeline.py`

**Interfaces:**
- Consumes: `src.name_resolver.resolve_names(text) -> (text, [(bn, en, qid)])`, `panel.corrections.add(path, bn, en, user) -> bool`.
- Produces: `highlight_names(text: str, matches: list) -> markupsafe.Markup`; the form fields `name_fix` (repeatable, value `bengali|||english`) and `remember_names` (`on`).

- [ ] **Step 1: Write the failing tests**

```python
def test_bengali_names_are_highlighted_safely():
    out = str(panel_app.highlight_names("<b>মুহাম্মদ ইউনূসের সাথে</b>",
                                        [("মুহাম্মদ ইউনূস", "Muhammad Yunus", "Q1")]))
    assert "<b>" not in out and "&lt;b&gt;" in out, "caption HTML was injected"
    assert '<mark class="wd" title="Muhammad Yunus (Q1)">মুহাম্মদ ইউনূস</mark>ের' in out, out
    assert str(panel_app.highlight_names("কিছু না", [])) == "কিছু না"


def test_remembered_name_fixes_reach_the_table_once():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
        real = config.REPLACEMENTS_PATH
        config.REPLACEMENTS_PATH = os.path.join(tmp, "t.tsv")
        page = "{{Information|description={{bn|1=ক খ}}{{en|1=Ka Kha{{Auto-translated PID English description}}}}}}"
        saved = []
        panel_app.wikiauth.fetch_wikitext = lambda t: (page, "rev1")
        panel_app.wikiauth.edit_description = lambda *a, **k: saved.append(a)
        panel_app.commons.caption = lambda *a, **k: ""
        try:
            for _ in range(2):
                client.post("/file/File:X.jpg", data={
                    "english": "Ka Kha fixed", "categories": "", "action": "save",
                    "name_fix": ["ক খ|||Ka Kha Fixed"], "remember_names": "on"})
            rows = __import__("panel.corrections", fromlist=["x"]).rows(config.REPLACEMENTS_PATH)
        finally:
            config.REPLACEMENTS_PATH = real
        assert [(r["bn"], r["en"]) for r in rows] == [("ক খ", "Ka Kha Fixed")], rows


def test_file_page_survives_the_replica_being_down():
    from src import name_resolver
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")
        page = "{{Information|description={{bn|1=মুহাম্মদ ইউনূস}}{{en|1=Muhammad Yunus}}}}"
        panel_app.wikiauth.fetch_wikitext = lambda t: (page, "rev1")
        real = name_resolver._lookup
        name_resolver._lookup = lambda keys: (_ for _ in ()).throw(RuntimeError("down"))
        os.environ.setdefault("TOOL_REPLICA_USER", "test")
        try:
            r = client.get("/file/File:X.jpg")
        finally:
            name_resolver._lookup = real
        assert r.status_code == 200 and "মুহাম্মদ ইউনূস" in r.get_data(as_text=True)
```

Note: `wikitext.read_bengali` / `read_english` must parse the fake pages above. If either raises `Unparseable`, copy the exact shape used by an existing passing test in `test_pipeline.py` (search for `{{bn|1=`) into these two tests instead.

- [ ] **Step 2: Run to verify they fail**

Run: `python -c "import test_pipeline as t; t.test_bengali_names_are_highlighted_safely()"`
Expected: `AttributeError: ... has no attribute 'highlight_names'`

- [ ] **Step 3: Implement in `panel/app.py`.** Add imports `import unicodedata`, `from markupsafe import Markup, escape` and `from src.name_resolver import resolve_names`. Add, above `file_detail`:

```python
def highlight_names(text, matches):
    """The Bengali, HTML-escaped, with each Wikidata-matched name marked."""
    text = unicodedata.normalize('NFC', text or '')
    out = str(escape(text))
    for bn, en, qid in sorted(matches, key=lambda m: -len(m[0])):
        out = out.replace(str(escape(bn)),
                          f'<mark class="wd" title="{escape(en)} ({escape(qid)})">{escape(bn)}</mark>')
    return Markup(out)
```

In `file_detail`, after `bengali = wikitext.read_bengali(page)` has run (i.e. just before `bucket = commons._bucket()`), add:

```python
    name_matches = resolve_names(bengali)[1] if bengali else []
```

and pass these to `render_template('file.html', …)`:

```python
        name_matches=name_matches,
        bengali_html=highlight_names(bengali, name_matches),
```

In `save_file`, immediately before the line `flash('Saved %s.' % …`, add:

```python
    if request.form.get('remember_names') == 'on' and is_maintainer():
        remembered = 0
        for pair in request.form.getlist('name_fix'):
            bn, _, en = pair.partition('|||')
            try:
                remembered += corrections.add(config.REPLACEMENTS_PATH, bn, en, current_user())
            except ValueError:
                pass
        if remembered:
            changed.append(f'{remembered} name fix{"" if remembered == 1 else "es"} '
                           f'remembered for future uploads')
```

- [ ] **Step 4: Update `panel/templates/file.html`**

4a. Above `<div class="workbench">`, add the summary box:

```html
  {% if bengali %}
    <div class="cdx-message cdx-message--block cdx-message--notice">
      <span class="cdx-message__icon"></span>
      <div class="cdx-message__content">
        {% if name_matches %}{{ name_matches|length }} name{{ '' if name_matches|length == 1 else 's' }} confirmed on Wikidata.
        {% else %}No names in this caption were found on Wikidata.{% endif %}
        Any other names were spelled by Gemini. Select one in the Bengali to fix it.
      </div>
    </div>
  {% endif %}
```

4b. Change the two `<figcaption>`s to `1. Downloaded from PID` and `2. Uploaded to Commons, caption cut off`.

4c. Replace the whole `{% if bengali %} … {% endif %}` Bengali block inside the form with:

```html
          {% if bengali %}
            <span class="stat-l">3. Bengali caption, read by OCR</span>
            <p class="bn-source" lang="bn" id="bnsource">{{ bengali_html }}</p>
            {% if name_matches %}
              <ul class="wdlist">
                {% for bn, en, qid in name_matches %}
                  <li><mark class="wd" lang="bn">{{ bn }}</mark> → <b>{{ en }}</b>
                    <a href="https://www.wikidata.org/wiki/{{ qid }}" target="_blank" rel="noopener">Wikidata</a></li>
                {% endfor %}
              </ul>
            {% endif %}
            <ul class="fixchips" id="fixchips"></ul>
            {% if is_maintainer %}
              <label class="check"><input type="checkbox" name="remember_names" checked>
                Also remember my name fixes for future uploads</label>
            {% endif %}
          {% else %}
            <p class="hint">No Bengali on this page, so there is nothing to
              check the translation against here.</p>
          {% endif %}
```

4d. Change the English label to `<label for="english" class="stat-l">4. English description on Commons</label>`.

4e. Just before `<script>`, add the popover (outside the form, so its inputs are not posted):

```html
<div class="fixpop" id="fixpop" hidden role="dialog" aria-label="Fix a name">
  <b>Fix this name</b>
  <span lang="bn" id="fixbn"></span>
  <label>How Gemini spelled it in the English (optional)
    <input id="fixwrong" autocomplete="off"></label>
  <label>Correct English
    <input id="fixright" autocomplete="off"></label>
  <span style="display:flex;gap:.5rem">
    <button type="button" class="primary small" id="fixapply">Apply</button>
    <button type="button" class="quiet small" id="fixcancel">Cancel</button>
  </span>
</div>
```

4f. Append to the page's `<script>`:

```js
  // Fix a name: select words in the Bengali, say what the English should be.
  // The fix edits the English box, and travels with the form as name_fix so the
  // server can remember it for future runs.
  (function () {
    var src = document.getElementById('bnsource'), pop = document.getElementById('fixpop');
    var form = src && src.closest('form');
    if (!src || !pop || !form) return;
    var english = document.getElementById('english'), chips = document.getElementById('fixchips');
    var bn = '';
    function close() { pop.hidden = true; }
    src.addEventListener('mouseup', openFromSelection);
    src.addEventListener('touchend', function () { setTimeout(openFromSelection, 0); });
    function openFromSelection() {
      var sel = window.getSelection();
      bn = sel ? sel.toString().trim() : '';
      if (!bn || !src.contains(sel.anchorNode)) return;
      var r = sel.getRangeAt(0).getBoundingClientRect();
      document.getElementById('fixbn').textContent = bn;
      document.getElementById('fixwrong').value = '';
      document.getElementById('fixright').value = '';
      pop.style.top = (window.scrollY + r.bottom + 6) + 'px';
      pop.style.left = Math.max(8, Math.min(window.scrollX + r.left,
                                            window.scrollX + document.documentElement.clientWidth - pop.offsetWidth - 8)) + 'px';
      pop.hidden = false;
      document.getElementById('fixright').focus();
    }
    document.getElementById('fixcancel').addEventListener('click', close);
    document.getElementById('fixapply').addEventListener('click', function () {
      var wrong = document.getElementById('fixwrong').value.trim();
      var right = document.getElementById('fixright').value.trim();
      if (!right || right.indexOf('|||') !== -1) return;
      if (wrong) english.value = english.value.split(wrong).join(right);
      var hidden = document.createElement('input');
      hidden.type = 'hidden'; hidden.name = 'name_fix'; hidden.value = bn + '|||' + right;
      form.appendChild(hidden);
      var li = document.createElement('li');
      li.textContent = bn + ' → ' + right;
      chips.appendChild(li);
      close();
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
  })();
```

- [ ] **Step 5: Run the suite**

Run: `python test_pipeline.py 2>&1 | tail -3`
Expected: `N checks passed`

- [ ] **Step 6: Look at it** in `scripts/preview_panel.py` on a file page (the preview's fake Commons data; if it has no file page, check that `/file/File:X.jpg` renders with the Commons fetch failing). Select Bengali text: the popover must open next to it, stay on screen at 360px, and Apply must add a chip.

- [ ] **Step 7: Commit**

```bash
git add panel/app.py panel/templates/file.html test_pipeline.py
git commit -m "Show which names Wikidata confirmed on a file, and let maintainers fix the rest"
```

---

### Task 5: Overview page — recent uploads, run history, log tucked away

**Files:**
- Modify: `panel/templates/index.html` (whole file)
- Modify: `panel/templates/_gallery.html` (link target, strip layout)
- Modify: `panel/templates/_logtools.html` (labels)
- Modify: `panel/templates/_runs.html` (heading copy)
- Modify: `panel/app.py` (`partial_gallery` limit)
- Test: `test_pipeline.py`

**Interfaces:**
- Consumes: `commons.recent_uploads(limit)` → rows with `filename`, `unique_id`.

- [ ] **Step 1: Write the failing test**

```python
def test_overview_links_recent_uploads_to_their_file_page_and_hides_the_log():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")
        panel_app.commons.recent_uploads = lambda limit=24: [
            {"filename": "A b.jpg", "unique_id": "u1"}][:limit]
        strip = client.get("/partials/gallery").get_data(as_text=True)
        assert "/file/File:A%20b.jpg" in strip or "/file/File:A b.jpg" in strip, strip
        page = client.get("/").get_data(as_text=True)
        assert "<details" in page and "Technical log" in page
        assert "Stderr" not in page
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -c "import test_pipeline as t; t.test_overview_links_recent_uploads_to_their_file_page_and_hides_the_log()"`
Expected: AssertionError (the strip links to `/upload/u1`)

- [ ] **Step 3: Implement.** In `partial_gallery`, change `commons.recent_uploads(limit=24)` to `commons.recent_uploads(limit=8)`.

Replace `panel/templates/_gallery.html`:

```html
{% if error %}
  <p class="prose">{{ error }}</p>
{% elif not uploads %}
  <p class="prose">Nothing uploaded yet. Photos appear here after a run uploads them.</p>
{% else %}
  <div class="strip">
    {% for u in uploads %}
      <a href="{{ url_for('file_detail', title='File:' ~ u.filename) }}" title="{{ u.filename }}">
        <img src="{{ thumb(u.filename, 240) }}" alt="{{ u.filename }}" loading="lazy">
      </a>
    {% endfor %}
  </div>
{% endif %}
```

Replace `panel/templates/index.html`:

```html
{% extends 'base.html' %}
{% block title %}Overview — PID upload bot{% endblock %}
{% block content %}

  {% include '_dashboard.html' %}

  <section class="card">
    <h2>Latest uploads</h2>
    <div hx-get="{{ url_for('partial_gallery') }}" hx-trigger="load" hx-swap="innerHTML">
      <p class="hint">Loading…</p>
    </div>
    <p class="hint"><a href="{{ url_for('uploads') }}">All uploads</a></p>
  </section>

  <section class="card">
    {% include '_runs.html' %}
  </section>

  <details class="card" {% if query or errors_only or request.args.get('log') %}open{% endif %}>
    <summary><h2 style="display:inline">Technical log</h2>
      <span class="hint">What the bot printed during its runs</span></summary>
    {% include '_logtools.html' %}
    <div id="logwrap" hx-get="{{ log_url }}" {% if live %}hx-trigger="every 3s"{% endif %} hx-swap="innerHTML">
      {% include '_log.html' %}
    </div>
  </details>

{% endblock %}
```

In `_logtools.html`: change the link text `Stderr` to `Errors`, `Freeze` to `Pause updates`, `Problems only` to `Only problems`, and add `log=1` to both stream links' `url_for` calls, so switching streams keeps the section open (e.g. `url_for('index', stream='err', log=1, …)`).

In `_runs.html`: change the hint to `One bar per run. Taller means more photos uploaded; red means the run failed.`

- [ ] **Step 4: Run the suite**

Run: `python test_pipeline.py 2>&1 | tail -3`
Expected: `N checks passed`

- [ ] **Step 5: Commit**

```bash
git add panel/templates/index.html panel/templates/_gallery.html panel/templates/_logtools.html panel/templates/_runs.html panel/app.py test_pipeline.py
git commit -m "Overview: latest uploads up front, the technical log folded away"
```

---

### Task 6: Uploads, Queue, Access in plain words, and retire the orphan detail page

**Files:**
- Modify: `panel/app.py` (`QUEUES` labels/blurbs; `upload_detail` becomes a redirect; delete `save_description`)
- Delete: `panel/templates/_detail.html`, `panel/templates/detail.html`
- Modify: `panel/templates/queue.html`, `panel/templates/_wayback.html`, `panel/templates/admin.html` (copy only)
- Test: `test_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
def test_old_upload_links_land_on_the_file_page():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")
        panel_app.commons.find_upload = lambda uid: {"filename": "A.jpg", "unique_id": uid}
        r = client.get("/upload/u1")
        assert r.status_code == 302 and "/file/File:A.jpg" in r.headers["Location"], r.headers
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -c "import test_pipeline as t; t.test_old_upload_links_land_on_the_file_page()"`
Expected: fails (currently renders a page, status 200)

- [ ] **Step 3: Implement.** Replace the whole `upload_detail` function with:

```python
@app.get('/upload/<unique_id>')
def upload_detail(unique_id):
    """Old links: the file page is where uploads are reviewed now."""
    record = commons.find_upload(unique_id)
    if not record:
        abort(404, 'No upload recorded with that id.')
    return redirect(url_for('file_detail', title=f"File:{record['filename']}"))
```

Delete `save_description` (the `@app.post('/upload/<unique_id>')` route) and the two templates:

```bash
git rm panel/templates/_detail.html panel/templates/detail.html
```

Change `QUEUES` labels and blurbs:

```python
    'review': {'label': 'Check the English',
               'blurb': 'The English was written by Gemini and nobody has checked it yet. '
                        'Compare it with the Bengali, fix it, then mark it checked.'},
    'uncategorised': {'label': 'Needs categories',
                      'blurb': 'Not in any topic category yet. Add what the photo shows.',
                      'category': commons.UNCATEGORISED},
    'flagged': {'label': 'Copyright questions',
                'blurb': 'Flagged as a possible copyright problem. Nothing has been '
                         'nominated for deletion; these are waiting for a decision.',
                'category': wikitext.CONCERNS_CATEGORY},
    'month': {'label': 'By month', 'blurb': 'Everything the bot uploaded in one month.'},
```

Copy edits: `queue.html` heading `Wayback queue` → `Waiting to be archived`, hint → `Source pages sent to the Internet Archive but not confirmed saved yet.`. `_wayback.html` button `Confirm them now` → `Check them now`. `admin.html` heading `Who can run this tool` → `Who can run the bot`, and `owner — set on the server, cannot be removed here` → `owner (set on the server, can't be removed here)`. In the file page's copyright button JS, keep the behaviour unchanged.

- [ ] **Step 4: Run the suite**

Run: `python test_pipeline.py 2>&1 | tail -3`
Expected: `N checks passed`

- [ ] **Step 5: Commit**

```bash
git add -A panel test_pipeline.py
git commit -m "Plain labels on Uploads, Queue and Access; old upload links open the file page"
```

---

### Task 7: Phone pass, final check, push

**Files:** whatever the pass turns up (CSS only, expected).

- [ ] **Step 1:** `python scripts/preview_panel.py`, then in Chrome's device toolbar at 360×740 visit `/`, `/uploads`, `/replacements`, `/queue`, `/admin` (signed in as owner, if the preview supports it). For each: no horizontal page scroll (`document.documentElement.scrollWidth <= innerWidth` in the console), the tabs scroll, the buttons wrap, the text is readable. Fix in `panel.css` under the `@media (max-width: 640px)` block.
- [ ] **Step 2:** Search the templates for leftover jargon:

Run: `grep -rn -i "stderr\|cronjob\|jobs api\|terminal\|pill" panel/templates`
Expected: no user-visible hits (comments are fine).

- [ ] **Step 3:** Delete now-unused CSS: for each class selector in `panel.css`, `grep -rn "<class>" panel/templates` and remove the rules whose class appears nowhere (e.g. `.pill*`, `.jobname`, `.since`, `.stats`, `.stat-n`).
- [ ] **Step 4:** `python test_pipeline.py 2>&1 | tail -1` → `N checks passed`.
- [ ] **Step 5: Commit and push to both remotes**

```bash
git add -A panel
git commit -m "Tidy the panel for phones and drop styles nothing uses"
git push tausheef control-panel && git push origin control-panel
```
