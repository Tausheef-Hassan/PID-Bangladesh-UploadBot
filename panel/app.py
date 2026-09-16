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
from panel import commons, wikiauth, wikitext
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

# Signing key for the session cookie. It must survive restarts and be identical
# in every gunicorn worker: a per-process random key would sign each worker's
# cookies differently and bounce people out at random. $SECRET_KEY first, then a
# file for local development.
_key_material = os.environ.get('SECRET_KEY', '').strip()
if not _key_material:
    try:
        _key_material = Path(config.SECRET_KEY_PATH).read_text(encoding='utf-8').strip()
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

def owner():
    """The account that owns this tool, from $PANEL_OWNER.

    The root of trust, and the one thing the web UI cannot change. Everyone
    else is granted access by the owner from /admin, so a maintainer whose
    session is stolen cannot lock the owner out or promote anyone.
    """
    return _username(os.environ.get('PANEL_OWNER', ''))


def granted():
    """Maintainers the owner has added, newest last. [] if none or unreadable.

    Kept as a file rather than an envvar because the tool has to write it at
    runtime, and `toolforge envvars` needs credentials the webservice does not
    have. That is safe here in a way it would not be for a secret: this is a
    list of public usernames, so NFS being world-readable costs nothing.
    """
    try:
        data = json.loads(Path(config.MAINTAINERS_PATH).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return [e for e in data if isinstance(e, dict) and e.get('user')]


def save_granted(entries):
    Path(config.MAINTAINERS_PATH).write_text(
        json.dumps(entries, indent=1), encoding='utf-8')


def maintainers():
    """Every account allowed to operate the job: the owner, plus the granted.

    Re-read on every call, so revoking someone takes effect on their very next
    click rather than at the next deploy. No owner means nobody, because the
    safe direction to fail is closed: a Wikimedia account proves who you are,
    never that you are allowed to stop this tool.

    MediaWiki treats underscores and spaces in usernames as the same character,
    so both sides are normalised before comparing.
    """
    people = {_username(e['user']) for e in granted()}
    if owner():
        people.add(owner())
    return {p for p in people if p}


def _username(name):
    return (name or '').strip().replace('_', ' ')


def current_user():
    """The signed-in Wikimedia account, or ''."""
    return _username(session.get('wiki_user'))


def is_maintainer():
    return bool(current_user()) and current_user() in maintainers()


def is_owner():
    return bool(owner()) and current_user() == owner()


def requires_owner(view):
    """Gate granting and revoking. Only the owner may change who has access."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not owner():
            abort(503, 'No PANEL_OWNER is set on the server.')
        if not is_owner():
            abort(403, 'Only the tool owner can change who has access.')
        return view(*args, **kwargs)
    return wrapped


def requires_maintainer(view):
    """Gate a job control.

    Three distinct answers, because they need three distinct fixes: the server
    has no allowlist, you are not signed in, or you are signed in as someone
    who is not a maintainer.
    """
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not maintainers():
            abort(503, 'Controls are off: no PANEL_OWNER set on the server.')
        if not current_user():
            abort(403, 'Sign in with your Wikimedia account to use the controls.')
        if not is_maintainer():
            abort(403, f'{current_user()} is not a maintainer of this tool.')
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def nav_state():
    """Every page renders the navbar, so its state is global context."""
    return {'wiki_user': session.get('wiki_user'),
            'wiki_configured': wikiauth.consumer() is not None,
            'is_maintainer': is_maintainer(),
            'is_owner': is_owner(),
            'controls_enabled': bool(maintainers())}


# ── Wikimedia OAuth ───────────────────────────────────────────────────────────
#
# One sign-in for everything. OAuth says who you are, which is what a Commons
# edit needs — it is published under your name. Operating the job needs more
# than that, so the maintainer allowlist above decides who may, and the same
# session answers both questions.

def _redirect_uri():
    """Must match the callback on the consumer registration, character for
    character — MediaWiki compares them exactly."""
    return url_for('oauth_callback', _external=True)


@app.get('/oauth/start')
def oauth_start():
    try:
        authorize_url, state = wikiauth.start(_redirect_uri())
    except Exception as e:
        flash(str(e))
        return redirect(url_for('index'))
    session['oauth_state'] = state
    return redirect(authorize_url)


@app.get('/oauth/callback')
def oauth_callback():
    # 2.0 has no request token, so `state` is the only thing tying this call
    # back to a sign-in we started. Without the check, anyone could hand a
    # signed-in user a link that logs them into someone else's account.
    expected = session.pop('oauth_state', None)
    if not expected or request.args.get('state') != expected:
        flash('That sign-in attempt expired. Try again.')
        return redirect(url_for('index'))

    if request.args.get('error'):
        flash('Wikimedia declined the sign-in: %s' % request.args['error'])
        return redirect(url_for('index'))

    try:
        token, username = wikiauth.finish(
            request.args.get('code', ''), _redirect_uri())
    except Exception as e:
        app.logger.warning('OAuth handshake failed: %r', e)
        flash('Wikimedia sign-in failed: %s' % e)
        return redirect(url_for('index'))

    session['wiki_token'] = token
    session['wiki_user'] = username
    flash(f'Signed in to Commons as {username}.')
    return redirect(request.args.get('next') or url_for('uploads'))


@app.post('/oauth/logout')
def oauth_logout():
    session.pop('wiki_token', None)
    session.pop('wiki_user', None)
    return redirect(url_for('uploads'))


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
@requires_maintainer
def run_now():
    # The Jobs API spec for restart: "If the job is a cronjob, execute it right now."
    jobs_api().post(_job_url('/restart'), display_messages=False)
    flash('Run started.')
    return redirect(url_for('index'))


@app.post('/pause')
@requires_maintainer
def pause():
    job, _ = fetch_job()
    if not job:
        abort(404, 'No pid-bot job is loaded.')
    session['schedule_before_pause'] = job.get('schedule')
    jobs_api().patch(_job_url(), json={'schedule': NEVER_FIRES}, display_messages=False)
    flash('Paused. The job definition is intact; nothing will fire until you resume.')
    return redirect(url_for('index'))


@app.post('/resume')
@requires_maintainer
def resume():
    schedule = session.pop('schedule_before_pause', None) or '@hourly'
    jobs_api().patch(_job_url(), json={'schedule': schedule}, display_messages=False)
    flash(f'Resumed on {schedule}.')
    return redirect(url_for('index'))


@app.post('/stop')
@requires_maintainer
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
@requires_maintainer
def save_replacements():
    path = os.path.join(config.SCRIPT_DIR, 'translation_replacements.tsv')
    Path(path).write_text(request.form.get('text', ''), encoding='utf-8')
    flash('Replacements saved. They apply on the next run.')
    return redirect(url_for('replacements'))


# ── Who has access ────────────────────────────────────────────────────────────

@app.get('/admin')
def admin():
    """The owner's view of who can operate this tool.

    Visible to any maintainer, so someone can see why they do or do not have
    access; only the owner can change it.
    """
    return render_template('admin.html', owner=owner(), granted=granted())


@app.post('/admin')
@requires_owner
def save_admin():
    entries = granted()
    name = _username(request.form.get('user', ''))
    action = request.form.get('action', '')

    if action == 'revoke':
        kept = [e for e in entries if _username(e['user']) != name]
        if len(kept) == len(entries):
            flash(f'{name} was not on the list.')
        else:
            save_granted(kept)
            flash(f'Removed {name}. They lose access on their next click.')
        return redirect(url_for('admin'))

    if not name:
        flash('Type the Wikimedia username to add.')
    elif name == owner():
        flash('You are the owner; that access cannot be granted or taken away here.')
    elif any(_username(e['user']) == name for e in entries):
        flash(f'{name} already has access.')
    else:
        entries.append({'user': name, 'granted_by': current_user(),
                        'at': datetime.now(timezone.utc).isoformat(timespec='seconds')})
        save_granted(entries)
        flash(f'{name} can now run the job. They need to sign in with Wikimedia.')
    return redirect(url_for('admin'))


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
    page = wikiauth.fetch_wikitext(f"File:{record['filename']}")
    english, auto_translated, categories, parse_error = '', True, [], ''
    if page is None:
        parse_error = "Couldn't read the page from Commons."
    else:
        try:
            english, auto_translated = wikitext.read_english(page)
            categories = wikitext.read_categories(page)
        except wikitext.Unparseable as e:
            parse_error = f'This page is not in the shape the bot writes ({e}), so it is not editable here.'

    template = '_detail.html' if request.headers.get('HX-Request') else 'detail.html'
    return render_template(template, record=record,
                           detail=commons.detail_for(unique_id),
                           english=english, auto_translated=auto_translated,
                           categories="\n".join(categories),
                           parse_error=parse_error,
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


@app.post('/upload/<unique_id>')
def save_description(unique_id):
    """Apply an edited description and categories to the file page.

    Only files the bot recorded in PIDDateData can be edited here — the panel
    must not become a general-purpose Commons editor — and only as the person
    who signed in with OAuth.
    """
    record = commons.find_upload(unique_id)
    if not record:
        abort(404, 'No upload recorded with that id.')
    token = session.get('wiki_token')
    if not token:
        abort(403, 'Sign in to Commons first.')

    title = f"File:{record['filename']}"
    page = wikiauth.fetch_wikitext(title)
    if page is None:
        flash("Couldn't read that page from Commons; nothing was changed.")
        return redirect(url_for('upload_detail', unique_id=unique_id))

    try:
        updated = wikitext.write_english(
            page,
            request.form.get('english', ''),
            mark_auto_translated=request.form.get('reviewed') != 'on')
        updated = wikitext.write_categories(
            updated, request.form.get('categories', '').splitlines())
    except wikitext.Unparseable as e:
        flash(f'Not saved: {e}')
        return redirect(url_for('upload_detail', unique_id=unique_id))

    if updated == page:
        flash('No change to save.')
        return redirect(url_for('upload_detail', unique_id=unique_id))

    try:
        wikiauth.edit_description(
            token, title, updated,
            'Reviewed the auto-translated description via the PID control panel')
    except Exception as e:
        flash(f'Commons refused the edit: {e}')
        return redirect(url_for('upload_detail', unique_id=unique_id))

    flash('Saved to Commons.')
    return redirect(url_for('upload_detail', unique_id=unique_id))


# ── Commons work queues ───────────────────────────────────────────────────────
#
# The queues come from Commons, not from PIDDateData: the backlog is tens of
# thousands of files, most uploaded long before this panel existed.

QUEUES = {
    'review': {
        'label': 'Needs review',
        'blurb': 'Still carrying the auto-translated marker. Check the English '
                 'against the Bengali, fix it, then clear the marker.',
    },
    'uncategorised': {
        'label': 'Needs categories',
        'blurb': 'In no topic category. Add what the photograph actually shows.',
        'category': commons.UNCATEGORISED,
    },
    'flagged': {
        'label': 'Copyright flags',
        'blurb': 'Flagged here as a possible copyright problem. Nothing is '
                 'nominated for deletion — these are waiting for a decision.',
        'category': wikitext.CONCERNS_CATEGORY,
    },
    'month': {
        'label': 'Browse by month',
        'blurb': 'Everything the bot filed for a given month.',
    },
}


def _queue_titles(queue, month, offset, cursor):
    """(titles, next_offset, next_cursor, total) for one page of a queue."""
    bucket = commons._bucket()

    # Every queue but 'review' is a category; 'month' just picks its own.
    target = QUEUES[queue].get('category')
    if queue == 'month':
        target = month or commons.month_categories(
            datetime.now(timezone.utc).year)[datetime.now(timezone.utc).month - 1]
    if target:
        titles, nxt = commons.category_page(target, cursor, bucket)
        return titles, None, nxt, commons.category_size(target, bucket)

    search = commons.REVIEW_SEARCH + (' incategory:"%s"' % month if month else '')
    titles, total = commons.search_page(search, offset, bucket)
    return titles, offset + len(titles), '', total


@app.get('/uploads')
def uploads():
    queue = request.args.get('queue', 'review')
    if queue not in QUEUES:
        queue = 'review'
    year = request.args.get('year', str(datetime.now(timezone.utc).year))
    month = request.args.get('month', '')
    offset = int(request.args.get('offset', 0) or 0)
    cursor = request.args.get('cursor', '')

    titles, next_offset, next_cursor, total = _queue_titles(
        queue, month, offset, cursor)
    bucket = commons._bucket()

    return render_template(
        'uploads.html', queue=queue, queues=QUEUES, year=year, month=month,
        titles=titles, total=total, offset=offset,
        next_offset=next_offset, next_cursor=next_cursor,
        months=commons.month_categories(year),
        years=[str(y) for y in range(datetime.now(timezone.utc).year, 2014, -1)],
        uncategorised_count=commons.category_size(commons.UNCATEGORISED, bucket),
        thumb=commons.thumb_url)


@app.get('/file/<path:title>')
def file_detail(title):
    """One file's editor, with prev/next that walk the queue it came from.

    Staying inside the queue is the point: a backlog of eight thousand is only
    tractable if finishing one file puts you on the next one.
    """
    if not title.startswith('File:'):
        abort(404)

    queue = request.args.get('queue', 'review')
    month = request.args.get('month', '')
    offset = int(request.args.get('offset', 0) or 0)
    cursor = request.args.get('cursor', '')

    titles, _next_offset, _next_cursor, total = _queue_titles(
        queue, month, offset, cursor)
    position = titles.index(title) if title in titles else None

    page = wikiauth.fetch_wikitext(title)
    english, auto_translated, categories = '', True, []
    parse_error, source_url, existing_tag = '', '', ''
    if page is None:
        parse_error = "Couldn't read the page from Commons."
    else:
        source_url = wikitext.read_source_url(page)
        existing_tag = wikitext.read_copyright_tag(page)
        try:
            english, auto_translated = wikitext.read_english(page)
            categories = wikitext.read_categories(page)
        except wikitext.Unparseable as e:
            parse_error = 'Not in the shape the bot writes (%s), so the description is not editable here.' % e

    bucket = commons._bucket()

    # The wikitext only knows the categories written on the page; the date and
    # PID-BD ones are added by Module:PIDCategoryHelper at render time. Showing
    # both is what stops someone hand-adding a category the file already has.
    editable = [c.strip() for c in categories]
    from_templates = [c for c in commons.categories_of(title, bucket)
                      if c not in editable]

    return render_template(
        'file.html', title=title, filename=title[len('File:'):],
        english=english, auto_translated=auto_translated,
        categories="\n".join(categories), editable_categories=editable,
        template_categories=from_templates, parse_error=parse_error,
        source_url=source_url, caption=commons.caption(title, bucket),
        prev_title=titles[position - 1] if position else None,
        next_title=(titles[position + 1]
                    if position is not None and position + 1 < len(titles) else None),
        position=position, page_count=len(titles), total=total,
        queue=queue, month=month, offset=offset, cursor=cursor, queues=QUEUES,
        existing_tag=existing_tag, reasons=wikitext.REASONS,
        confirm_reason=wikitext.NEEDS_CONFIRMATION,
        thumb=commons.thumb_url, filepage=commons.file_page_url)


@app.post('/file/<path:title>')
def save_file(title):
    """Apply caption, description and categories to one Commons file."""
    if not title.startswith('File:'):
        abort(404)
    token = session.get('wiki_token')
    if not token:
        abort(403, 'Sign in to Commons first.')

    onward = {'queue': request.form.get('queue', 'review'),
              'month': request.form.get('month', ''),
              'offset': request.form.get('offset', 0),
              'cursor': request.form.get('cursor', '')}

    page = wikiauth.fetch_wikitext(title)
    if page is None:
        flash("Couldn't read that page from Commons; nothing was changed.")
        return redirect(url_for('file_detail', title=title, **onward))

    changed = []
    bucket = commons._bucket()

    new_caption = request.form.get('caption', '').strip()
    if new_caption and new_caption != commons.caption(title, bucket):
        try:
            wikiauth.set_caption(
                token, commons.page_id(title, bucket), new_caption)
            changed.append('caption')
        except Exception as e:
            flash('Caption not saved: %s' % e)

    # The marker is only ever cleared by the button that says so. A plain Save
    # reads the page's own state rather than the form, so editing a description
    # never silently drops a marker someone else still needs to see.
    try:
        _current, still_marked = wikitext.read_english(page)
    except wikitext.Unparseable:
        still_marked = False
    clearing = request.form.get('action') == 'clear-next'

    updated = page
    try:
        updated = wikitext.write_english(
            page, request.form.get('english', ''),
            mark_auto_translated=still_marked and not clearing)
        updated = wikitext.write_categories(
            updated, request.form.get('categories', '').splitlines())
    except wikitext.Unparseable as e:
        flash('Description not saved: %s' % e)
        updated = page

    if updated != page:
        try:
            wikiauth.edit_description(
                token, title, updated,
                'Checked against the Bengali; cleared the auto-translated marker'
                if clearing else
                'Reviewed the auto-translated description via the PID control panel')
            changed.append('marker cleared' if clearing
                           else 'description and categories')
        except Exception as e:
            flash('Commons refused the edit: %s' % e)

    flash('Saved %s.' % ' and '.join(changed) if changed else 'Nothing to save.')

    # Land on the next file, so reviewing a queue is one continuous pass.
    onward_title = title
    if request.form.get('action') in ('save-next', 'clear-next'):
        onward_title = request.form.get('next_title') or title
    return redirect(url_for('file_detail', title=onward_title, **onward))


@app.get('/categories/suggest')
def suggest_categories():
    """Commons categories matching what someone is typing.

    Server-side so the browser never talks to the Commons API directly, and so
    the suggestions share the same two-minute cache as everything else here.
    """
    return render_template(
        '_catsuggest.html',
        names=commons.suggest_categories(request.args.get('q', ''),
                                         commons._bucket()))


@app.post('/file/<path:title>/tag')
def tag_file(title):
    """Flag one file as a possible copyright problem.

    The mild reasons only add a review category; the severe ones write the
    maintenance templates Commons administrators act on, which is why the
    heaviest of them will not go through without the filename typed out.
    """
    if not title.startswith('File:'):
        abort(404)
    token = session.get('wiki_token')
    if not token:
        abort(403, 'Sign in to Commons first.')

    onward = {'queue': request.form.get('queue', 'review'),
              'month': request.form.get('month', ''),
              'offset': request.form.get('offset', 0),
              'cursor': request.form.get('cursor', '')}
    back = redirect(url_for('file_detail', title=title, **onward))

    reason = request.form.get('reason', '')
    note = request.form.get('note', '')

    if reason == wikitext.NEEDS_CONFIRMATION:
        typed = request.form.get('confirm', '').strip()
        if typed != title[len('File:'):]:
            flash('Type the filename exactly to confirm a copyright violation. '
                  'Nothing was changed.')
            return back

    page = wikiauth.fetch_wikitext(title)
    if page is None:
        flash("Couldn't read that page from Commons; nothing was changed.")
        return back

    try:
        updated = wikitext.tag_copyright(page, reason, note)
    except wikitext.Unparseable as e:
        flash('Not flagged: %s' % e)
        return back

    try:
        wikiauth.edit_description(token, title, updated,
                                  wikitext.flag_summary(reason, note))
    except Exception as e:
        flash('Commons refused the edit: %s' % e)
        return back

    flash('Flagged: %s.' % wikitext.REASONS[reason][0])

    # Flagging is a verdict, so move on the way saving does.
    return redirect(url_for('file_detail',
                            title=request.form.get('next_title') or title,
                            **onward))


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
@requires_maintainer
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


def secret_source(configured, *env_names):
    """Where a working secret came from: 'envvars', 'file' or 'missing'.

    Names only, never values: this endpoint is public, and so is the bot log the
    panel serves beside it. A half-set pair of envvars reads as 'file', because
    that is the one the loader will actually have fallen back to.
    """
    if not configured:
        return 'missing'
    return ('envvars' if all(os.environ.get(n, '').strip() for n in env_names)
            else 'file')


@app.get('/healthz')
def healthz():
    """Liveness, plus where each secret is coming from.

    After `toolforge envvars create`, this is how you confirm the webservice
    picked the values up rather than falling back to a stale key file on NFS.
    """
    return {
        'ok': True,
        'oauth': secret_source(wikiauth.consumer(),
                               'OAUTH_CONSUMER_KEY', 'OAUTH_CONSUMER_SECRET'),
        'secret_key': secret_source(_key_material, 'SECRET_KEY'),
        'owner_set': bool(owner()),
        'maintainers': len(maintainers()),
        'tool_data_dir': bool(config.TOOL_DATA_DIR),
    }
