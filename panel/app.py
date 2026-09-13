# app.py
# Control panel for the PID bot.
#
# The panel never imports the bot's pipeline. It drives the Toolforge job
# through the Jobs API (via toolforge-weld, the client Wikimedia's own CLI is
# built on) and reads the files the bot already writes into $TOOL_DATA_DIR.

import functools
import hashlib
import re
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import humanize
from croniter import croniter
from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from toolforge_weld.api_client import ToolforgeClient
from toolforge_weld.kubernetes_config import Kubeconfig

import config
from panel import commons
from src import run_state, wayback

API_SERVER = 'https://api.svc.tools.eqiad1.wikimedia.cloud:30003/jobs/v1'

# What each Jobs API status means for someone looking after this bot, rather
# than what Kubernetes calls it.
STATE_LABELS = {
    'running': 'Running',
    'pending': 'Starting',
    'succeeded': 'Idle, last run finished cleanly',
    'failed': 'Last run failed',
    'unknown': 'Status unavailable',
    'no-job': 'Not loaded on Toolforge',
    'api-down': "Can't reach the Jobs API",
}

# Toolforge has no "disable this cronjob" flag, so pausing means rescheduling it
# for 31 February — a date that never arrives. The definition survives intact.
# ponytail: replace with a real disable if the Jobs API ever grows one.
NEVER_FIRES = '0 0 31 2 *'

# Enough log to see a whole run without reading a week of history into memory.
LOG_TAIL_BYTES = 60_000

# Hosts the source-image proxy will fetch from. Without this allowlist the
# endpoint would be an open proxy sitting inside Toolforge's network.
SOURCE_HOSTS = frozenset({
    'pressinform.gov.bd', 'pressinform.portal.gov.bd', 'web.archive.org'})
SOURCE_HOST_SUFFIXES = ('.oraclecloud.com', '.oraclecloud15.com')
MAX_SOURCE_BYTES = 12 * 1024 * 1024

# The panel fetches source images itself because pressinform's certificate does
# not validate; the bot already works around this with verify=False.
# Short retries and short timeouts: this session serves <img> requests, so it
# must fail fast rather than retry for a minute behind a spinning thumbnail.
source_session = config.http_session(retries=1, backoff=0.3)
SOURCE_TIMEOUT = 8

_UNAVAILABLE_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 260'>"
    "<rect width='400' height='260' fill='#f4f8fb'/>"
    "<text x='200' y='122' text-anchor='middle' font-family='sans-serif' "
    "font-size='15' fill='#47637c'>Original no longer published</text>"
    "<text x='200' y='146' text-anchor='middle' font-family='sans-serif' "
    "font-size='13' fill='#8ba2b8'>and no archive snapshot was found</text>"
    "</svg>").encode()

app = Flask(__name__)

# Signing key for the session cookie. Derived from panel.key so it survives
# restarts and is shared across gunicorn workers; random (logins drop on
# restart) when no key is configured and the controls are disabled anyway.
_key_material = ''
try:
    _key_material = Path(config.PANEL_KEY_PATH).read_text(encoding='utf-8').strip()
except OSError:
    pass
app.secret_key = (hashlib.sha256(('panel-session:' + _key_material).encode()).digest()
                  if _key_material else secrets.token_bytes(32))


# ── Toolforge Jobs API ────────────────────────────────────────────────────────

def _kubeconfig_path():
    """Locate the tool's kubeconfig.

    $HOME is /app inside a Build Service container, so ~/.kube/config does not
    resolve there — the tool's real home is $TOOL_DATA_DIR. Same trap config.py
    already works around for the credential files.
    """
    for candidate in (os.environ.get('KUBECONFIG'),
                      os.path.join(config.CREDS_DIR, '.kube', 'config'),
                      os.path.expanduser('~/.kube/config')):
        if candidate and os.path.exists(candidate):
            return Path(candidate)
    return None


@functools.lru_cache(maxsize=1)
def jobs_api():
    """The Jobs API client. Built lazily so a missing cert renders an error
    page instead of killing the worker at import time."""
    path = _kubeconfig_path()
    if path is None:
        raise RuntimeError(
            'No Toolforge kubeconfig found. The panel can only reach the Jobs '
            'API from inside the tool account.')
    return ToolforgeClient(
        server=API_SERVER,
        kubeconfig=Kubeconfig.from_path(path),
        user_agent='pid-bot-panel',
    )


def _job_url(suffix=''):
    return f'/tool/{config.TOOL_NAME}/jobs/{config.JOB_NAME}{suffix}'


def fetch_job():
    """Returns (job, reachable).

    `reachable` separates "the API told us there is no such job" from "we could
    not ask" — they look identical from a None return but mean opposite things
    to whoever is reading the page at 3am.
    """
    try:
        return jobs_api().get(_job_url(), display_messages=False).get('job'), True
    except Exception as e:
        app.logger.warning('Jobs API unreachable: %r', e)
        return None, False


# ── Auth ──────────────────────────────────────────────────────────────────────

def panel_token():
    """The shared secret, re-read each time so it can be rotated without a
    restart. Empty means the controls are switched off."""
    try:
        return Path(config.PANEL_KEY_PATH).read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def signed_in():
    return bool(session.get('signed_in'))


def requires_token(view):
    """Gate a write. A missing panel.key disables controls — it never opens them."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not panel_token():
            abort(503, 'Controls are off: no panel.key on the server.')
        if not signed_in():
            abort(403, 'Sign in to use the controls.')
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def nav_state():
    """Every page renders the navbar, so its state is global context."""
    return {'signed_in': signed_in(), 'controls_enabled': bool(panel_token())}


@app.get('/sign-in')
def sign_in_page():
    return render_template('signin.html')


@app.post('/sign-in')
def sign_in():
    expected = panel_token()
    supplied = request.form.get('token', '')
    if expected and secrets.compare_digest(supplied, expected):
        session['signed_in'] = True
        session.permanent = True
    else:
        flash("That key didn't match.")
    return redirect(url_for('index'))


@app.post('/sign-out')
def sign_out():
    session.clear()
    return redirect(url_for('index'))


# ── Reading what the bot leaves behind ────────────────────────────────────────

# Lines worth picking out of a wall of scrolling output.
TROUBLE = re.compile(
    r'error|failed|failure|traceback|exception|exhausted|429|timed out|'
    r'giving up|no separator|warning', re.I)
GOOD = re.compile(r'upload successful|succeeded|success|confirmed|created', re.I)
HEADING = re.compile(r'^(=+$|STEP |Processing row |Batch updating|PROCESSING)')


def read_log(stream='out'):
    """Tail of the job's output, written by `filelog: true`.

    Reads the file rather than the API's log endpoint: the file outlives the
    pod, so the log survives between hourly runs.
    """
    suffix = 'err' if stream == 'err' else 'out'
    path = os.path.join(config.CREDS_DIR, f'{config.JOB_NAME}.{suffix}')
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            f.seek(max(0, size - LOG_TAIL_BYTES))
            raw = f.read()
    except OSError:
        return ''
    text = raw.decode('utf-8', errors='replace')
    # Drop the leading partial line when the seek landed mid-line.
    return text.split('\n', 1)[-1] if size > LOG_TAIL_BYTES else text


def log_lines(stream='out', query='', errors_only=False):
    """Tail split into classified lines, filtered. Returns (lines, total)."""
    everything = read_log(stream).split('\n')
    total = len(everything)

    kept = []
    for line in everything:
        if errors_only and not TROUBLE.search(line):
            continue
        if query and query.lower() not in line.lower():
            continue
        if TROUBLE.search(line):
            kind = 'bad'
        elif GOOD.search(line):
            kind = 'good'
        elif HEADING.match(line):
            kind = 'head'
        else:
            kind = ''
        kept.append({'text': line, 'kind': kind})
    return kept, total


def _parse(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except ValueError:
        return None


def heartbeat(records, slots=72):
    """One tick per recent run for the strip at the top of the page.

    Height encodes images uploaded, colour encodes outcome. Three days of
    hourly runs fits in 72 ticks.
    """
    recent = records[-slots:]
    ceiling = max([r.get('uploaded', 0) for r in recent] or [0]) or 1
    ticks = []
    for r in recent:
        uploaded = r.get('uploaded', 0)
        status = r.get('status', 'unknown')
        started = _parse(r.get('started_at'))
        ticks.append({
            'status': status,
            # Floor at 8% so a zero-upload run is still a visible tick, not a gap.
            'height': round(8 + 92 * (uploaded / ceiling)) if uploaded else 8,
            'label': '{}, {} uploaded{}'.format(
                humanize.naturaltime(datetime.now(timezone.utc) - started) if started else 'unknown time',
                uploaded,
                f", {r['failed']} failed" if r.get('failed') else ''),
        })
    return ticks


def page_context():
    job, api_reachable = fetch_job()
    records = run_state.load()
    latest = records[-1] if records else None
    status = (job or {}).get('status', {})

    next_run = None
    schedule = (job or {}).get('schedule')
    if schedule and schedule != NEVER_FIRES:
        try:
            next_run = croniter(schedule, datetime.now(timezone.utc)).get_next(datetime)
        except (ValueError, KeyError):
            next_run = None

    if job:
        state = status.get('short', 'unknown')
    elif not api_reachable:
        # Fall back to the bot's own record: an unreachable control plane is no
        # reason to stop reporting what the last run actually did.
        state = 'running' if (latest and latest.get('status') == 'running'
                              and not latest.get('finished_at')) else 'api-down'
    else:
        state = 'no-job'

    return {
        'job': job,
        'state': state,
        'state_label': STATE_LABELS.get(state, state),
        'state_detail': ', '.join(status.get('messages', []) or []),
        'duration': status.get('duration', ''),
        'paused': schedule == NEVER_FIRES,
        'api_reachable': api_reachable,
        'schedule': schedule,
        'next_run': next_run,
        'next_in': humanize.naturaldelta(next_run - datetime.now(timezone.utc)) if next_run else '',
        'latest': latest,
        'started_ago': (humanize.naturaltime(
            datetime.now(timezone.utc) - _parse(latest.get('started_at')))
            if latest and _parse(latest.get('started_at')) else ''),
        'ticks': heartbeat(records),
    }


# ── Views ─────────────────────────────────────────────────────────────────────

@app.get('/')
def index():
    view = _log_view()
    lines, total = log_lines(view['stream'], view['query'], view['errors_only'])
    return render_template(
        'index.html', lines=lines, total=total,
        updated=datetime.now().strftime('%H:%M:%S'),
        # htmx polls this URL, so the filters must travel with it.
        log_url=url_for('partial_log', stream=view['stream'],
                        q=view['query'] or None,
                        level='errors' if view['errors_only'] else None,
                        live=None if view['live'] else '0'),
        **view, **page_context())


@app.get('/uploads')
def uploads():
    return render_template('uploads.html')


@app.get('/queue')
def queue():
    return render_template('queue.html')


@app.get('/partials/runs')
def partial_runs():
    """The run strip polls on its own clock: it changes hourly, not every 5s."""
    return render_template('_runs.html', **page_context())


@app.get('/partials/dashboard')
def partial_dashboard():
    """Polled by htmx; same context, just the part that changes."""
    return render_template('_dashboard.html', **page_context())


def _log_view():
    """Filter state shared by the log partial and the raw download."""
    return {
        'stream': 'err' if request.args.get('stream') == 'err' else 'out',
        'query': request.args.get('q', '').strip(),
        'errors_only': request.args.get('level') == 'errors',
        'live': request.args.get('live') != '0',
    }


@app.get('/partials/log')
def partial_log():
    view = _log_view()
    lines, total = log_lines(view['stream'], view['query'], view['errors_only'])
    return render_template('_log.html', lines=lines, total=total,
                           updated=datetime.now().strftime('%H:%M:%S'), **view)


@app.get('/log.txt')
def log_download():
    """The raw tail, for grepping somewhere more comfortable than a browser."""
    view = _log_view()
    return Response(read_log(view['stream']), mimetype='text/plain; charset=utf-8')


# ── Controls ──────────────────────────────────────────────────────────────────

@app.post('/run')
@requires_token
def run_now():
    # The Jobs API spec for restart: "If the job is a cronjob, execute it right now."
    jobs_api().post(_job_url('/restart'), display_messages=False)
    flash('Run started.')
    return redirect(url_for('index'))


@app.post('/pause')
@requires_token
def pause():
    job, _ = fetch_job()
    if not job:
        abort(404, 'No pid-bot job is loaded.')
    session['schedule_before_pause'] = job.get('schedule')
    jobs_api().patch(_job_url(), json={'schedule': NEVER_FIRES}, display_messages=False)
    flash('Paused. The job definition is intact; nothing will fire until you resume.')
    return redirect(url_for('index'))


@app.post('/resume')
@requires_token
def resume():
    schedule = session.pop('schedule_before_pause', None) or '@hourly'
    jobs_api().patch(_job_url(), json={'schedule': schedule}, display_messages=False)
    flash(f'Resumed on {schedule}.')
    return redirect(url_for('index'))


@app.post('/stop')
@requires_token
def stop():
    """Delete the job, killing any pod mid-run.

    main.py writes every successful upload to PIDDateData in one batch edit at
    the very end, so a kill discards this run's registrations: those images get
    scraped, OCR'd and translated again next run, then rejected as duplicates.
    The template says so before the button is pressed.
    """
    jobs_api().delete(_job_url(), display_messages=False)
    flash('Job stopped and removed. Reload it with: toolforge jobs load toolforge/job.yaml')
    return redirect(url_for('index'))


# ── Tools ─────────────────────────────────────────────────────────────────────

@app.get('/replacements')
def replacements():
    path = os.path.join(config.SCRIPT_DIR, 'translation_replacements.tsv')
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError:
        text = ''
    return render_template('replacements.html', text=text)


@app.post('/replacements')
@requires_token
def save_replacements():
    path = os.path.join(config.SCRIPT_DIR, 'translation_replacements.tsv')
    Path(path).write_text(request.form.get('text', ''), encoding='utf-8')
    flash('Replacements saved. They apply on the next run.')
    return redirect(url_for('replacements'))


# ── Uploaded images ───────────────────────────────────────────────────────────

@app.get('/partials/gallery')
def partial_gallery():
    """Lazy-loaded so a slow Commons fetch never delays the status card."""
    try:
        uploads = commons.recent_uploads(limit=24)
        error = ''
    except Exception as e:
        app.logger.warning('Commons unreachable: %r', e)
        uploads, error = [], "Couldn't reach Commons for the upload list."
    return render_template('_gallery.html', uploads=uploads, error=error,
                           thumb=commons.thumb_url, filepage=commons.file_page_url)


@app.get('/upload/<unique_id>')
def upload_detail(unique_id):
    """Source image beside the cropped upload, with the text the bot read.

    This is the only place the cropper's output can be checked without opening
    Commons: if the separator was found in the wrong place, the two images side
    by side show it immediately.
    """
    record = commons.find_upload(unique_id)
    if not record:
        abort(404, 'No upload recorded with that id.')
    # htmx asks for the fragment; a plain click (or no JS) gets a whole page.
    template = '_detail.html' if request.headers.get('HX-Request') else 'detail.html'
    return render_template(template, record=record,
                           detail=commons.detail_for(unique_id),
                           thumb=commons.thumb_url,
                           filepage=commons.file_page_url)


def _allowed_source(url):
    host = (urlparse(url).hostname or '').lower()
    return host in SOURCE_HOSTS or host.endswith(SOURCE_HOST_SUFFIXES)


@functools.lru_cache(maxsize=512)
def _archived_copy(url, bucket):
    """Wayback snapshot for a source image PID has removed, or ''.

    Deliberately not wayback.get_wayback_url(): that helper rides the bot's
    10-retry session with 30s timeouts, which is right for a batch job and far
    too slow inside an image request. Cached per two-minute bucket so a dead
    image costs one lookup, not one per page view.
    """
    try:
        r = source_session.get('https://archive.org/wayback/available',
                               params={'url': url}, timeout=6)
        closest = r.json().get('archived_snapshots', {}).get('closest', {})
        return closest.get('url') or ''
    except Exception:
        return ''


@app.get('/source-image')
def source_image():
    """Proxy one source image, so the original can sit next to the crop.

    Restricted to the hosts the bot actually scrapes; anything else is refused
    rather than fetched.
    """
    url = request.args.get('url', '')
    if not url or not _allowed_source(url):
        abort(400, 'Not a PID source image.')

    try:
        upstream = source_session.get(url, timeout=SOURCE_TIMEOUT,
                                      verify=False, stream=True)

        # PID rotates images out of its object storage, which is the whole
        # reason the bot archives them. Fall back to the snapshot, as
        # image_processor.download_image already does on a 404.
        if upstream.status_code == 404:
            snapshot = _archived_copy(url, int(time.time() // 120))
            if not snapshot:
                raise ValueError('gone from PID, no snapshot')
            upstream = source_session.get(snapshot, timeout=SOURCE_TIMEOUT,
                                          verify=False, stream=True)

        upstream.raise_for_status()
        content_type = upstream.headers.get('Content-Type', 'image/jpeg')
        if not content_type.startswith('image/'):
            raise ValueError(f'not an image: {content_type}')
        body = upstream.raw.read(MAX_SOURCE_BYTES + 1, decode_content=True)
        if len(body) > MAX_SOURCE_BYTES:
            raise ValueError('source image too large to preview')
    except Exception as e:
        app.logger.info('source image unavailable for %s: %r', url, e)
        # A placeholder keeps the comparison laid out; a 502 would just leave a
        # broken-image icon with no explanation of why.
        return Response(_UNAVAILABLE_SVG, mimetype='image/svg+xml',
                        headers={'Cache-Control': 'public, max-age=300'})

    return Response(body, mimetype=content_type,
                    headers={'Cache-Control': 'public, max-age=3600'})


# ── Wayback queue ─────────────────────────────────────────────────────────────

@app.get('/partials/wayback')
def partial_wayback():
    try:
        with open(config.WAYBACK_QUEUE_PATH, encoding='utf-8') as f:
            queue = json.load(f)
        queue = queue if isinstance(queue, list) else []
    except (OSError, ValueError):
        queue = []
    return render_template('_wayback.html', queue=queue)


@app.post('/wayback/retry')
@requires_token
def wayback_retry():
    """Confirm pending archives in the background.

    A full pass can take minutes, which is far longer than a web request should
    live, so it runs on a thread under a short budget and the page reports the
    result on the next poll.
    """
    threading.Thread(target=wayback.retry_wayback_queue,
                     kwargs={'budget_seconds': 60}, daemon=True).start()
    flash('Confirming pending archives in the background.')
    return redirect(url_for('index'))


@app.get('/healthz')
def healthz():
    return {'ok': True}
