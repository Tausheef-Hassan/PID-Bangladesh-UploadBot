# app.py
# Control panel for the PID bot.
#
# The panel never imports the bot's pipeline. It drives the Toolforge job
# through the Jobs API (via toolforge-weld, the client Wikimedia's own CLI is
# built on) and reads the files the bot already writes into $TOOL_DATA_DIR.

import functools
import hashlib
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

import humanize
from croniter import croniter
from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from toolforge_weld.api_client import ToolforgeClient
from toolforge_weld.kubernetes_config import Kubeconfig

import config
from src import run_state

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

def tail_log():
    """Last chunk of the job's stdout, written by `filelog: true`.

    Reads the file rather than the API's log endpoint: the file outlives the pod,
    so the log survives between hourly runs.
    """
    path = os.path.join(config.CREDS_DIR, f'{config.JOB_NAME}.out')
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
        'signed_in': signed_in(),
        'controls_enabled': bool(panel_token()),
    }


# ── Views ─────────────────────────────────────────────────────────────────────

@app.get('/')
def index():
    return render_template('index.html', **page_context())


@app.get('/partials/dashboard')
def partial_dashboard():
    """Polled by htmx; same context, just the part that changes."""
    return render_template('_dashboard.html', **page_context())


@app.get('/partials/log')
def partial_log():
    return render_template('_log.html', log=tail_log())


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
    return render_template('replacements.html', text=text,
                           signed_in=signed_in(),
                           controls_enabled=bool(panel_token()))


@app.post('/replacements')
@requires_token
def save_replacements():
    path = os.path.join(config.SCRIPT_DIR, 'translation_replacements.tsv')
    Path(path).write_text(request.form.get('text', ''), encoding='utf-8')
    flash('Replacements saved. They apply on the next run.')
    return redirect(url_for('replacements'))


@app.get('/healthz')
def healthz():
    return {'ok': True}
