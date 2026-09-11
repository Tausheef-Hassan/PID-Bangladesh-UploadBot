# wayback.py
# All Wayback Machine / Internet Archive operations:
#   - Persistent pending-archive queue (wayback_pending.json)
#   - Save Page Now v2 authenticated archiving
#   - Unauthenticated fallback archiving
#   - Retrieval of archived snapshots for 404 recovery
#   - Retry loop for URLs that failed in previous runs

import json
import logging
import os
import time
import threading

from urllib.parse import quote

import config  # also installs the IPv4-only patch on import

# Retries transport errors and 429/5xx internally; POSTs are never resubmitted.
session = config.http_session()
session.trust_env = False  # Do not pick up HTTP_PROXY / HTTPS_PROXY env vars; connect directly
CONNECT_TIMEOUT = 8   # seconds

# Wall-clock cap on the queue-confirmation pass. The pool thread running it is
# non-daemon, so without a cap a long queue holds the hourly job open.
RETRY_BUDGET = 600    # seconds

_queue_lock = threading.Lock()

# ── Dedicated Wayback log ─────────────────────────────────────────────────────
_log_dir = getattr(config, 'TOOL_DATA_DIR', None) or os.getcwd()
WAYBACK_LOG_PATH = os.path.join(_log_dir, 'wayback.log')

_wb_logger = logging.getLogger('wayback')
_wb_logger.setLevel(logging.DEBUG)
_wb_logger.propagate = False

_wb_file_handler = logging.FileHandler(WAYBACK_LOG_PATH, encoding='utf-8')
_wb_file_handler.setLevel(logging.DEBUG)
_wb_file_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
_wb_logger.addHandler(_wb_file_handler)


def _wblog(level: str, msg: str):
    """Write a line to wayback.log and also print to stdout."""
    getattr(_wb_logger, level)(msg)
    print(msg)


# ── Persistent queue helpers ──────────────────────────────────────────────────

def _load_wayback_queue():
    """Load the persistent pending-archive queue from disk."""
    if not os.path.exists(config.WAYBACK_QUEUE_PATH):
        return []
    try:
        with open(config.WAYBACK_QUEUE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        _wblog('warning', f"Warning: could not read wayback queue: {repr(e)}")
        return []


def _save_wayback_queue(queue):
    """Persist the pending-archive queue to disk."""
    try:
        with open(config.WAYBACK_QUEUE_PATH, 'w', encoding='utf-8') as f:
            json.dump(queue, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _wblog('warning', f"Warning: could not save wayback queue: {repr(e)}")


def _enqueue_wayback(url):
    """Add a URL to the persistent queue if not already present."""
    with _queue_lock:
        queue = _load_wayback_queue()
        if url not in queue:
            queue.append(url)
            _save_wayback_queue(queue)
            _wblog('info', f"  Queued for next run: {url}")


def _dequeue_wayback(url):
    """Remove a successfully archived URL from the queue."""
    with _queue_lock:
        queue = _load_wayback_queue()
        if url in queue:
            queue.remove(url)
            _save_wayback_queue(queue)


# ── Core Wayback operations ───────────────────────────────────────────────────

def get_wayback_url(url):
    """Get the oldest archived version from Wayback Machine (used for 404 fallback).
    Transport errors and 429/5xx are retried inside `session`."""
    try:
        encoded_url = quote(url, safe='')
        api_url = f"http://archive.org/wayback/available?url={encoded_url}"

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = session.get(api_url, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()

        if data.get('archived_snapshots') and data['archived_snapshots'].get('closest'):
            wayback_url = data['archived_snapshots']['closest']['url']

            cdx_url = f"http://web.archive.org/cdx/search/cdx?url={encoded_url}&limit=1&output=json"
            cdx_response = session.get(cdx_url, headers=headers, timeout=30)

            if cdx_response.status_code == 200:
                cdx_data = cdx_response.json()
                if len(cdx_data) > 1:
                    timestamp = cdx_data[1][1]
                    original_url = cdx_data[1][2]
                    oldest_url = f"http://web.archive.org/web/{timestamp}/{original_url}"
                    _wblog('info', f"[404 fallback] Retrieved oldest snapshot: {oldest_url}")
                    return oldest_url, None

            _wblog('info', f"[404 fallback] Retrieved snapshot: {wayback_url}")
            return wayback_url, None
        else:
            _wblog('warning', f"[404 fallback] FAIL — Status: {response.status_code} | Raw Response: {response.text}")
            return None, "No archived version found"

    except Exception as e:
        _wblog('error', f"[404 fallback] RAW EXCEPTION for {url}: {repr(e)}")
        return None, f"Wayback Machine error: {str(e)}"


def is_archived(url):
    """Cheap check: does Wayback already hold a snapshot of this URL?"""
    try:
        r = session.get(f'https://archive.org/wayback/available?url={quote(url, safe="")}',
                        timeout=(CONNECT_TIMEOUT, 15))
        return bool(r.json().get('archived_snapshots', {}).get('closest'))
    except Exception as e:
        _wblog('debug', f"[Archived?] check failed for {url}: {repr(e)}")
        return False


def archive_to_wayback(url, _enqueue_on_fail=True, confirm=True):
    """Submit URL to the Save Page Now v2 API for Wayback Machine archiving.
    Outputs raw status codes and payloads on all failure scenarios.

    confirm=False submits the job and returns immediately without waiting for
    the capture to land. The URL goes onto the persistent queue and the next
    run confirms it. Polling costs up to 90 s per URL, which on the hot path
    keeps the hourly job alive long after its work is done.
    """
    _wblog('info', f"Archiving to Wayback Machine: {url}")
    success = False
    try:
        headers = {
            'User-Agent': 'PID-Bangladesh-UploadBot/2.0',
            'Accept': 'application/json',
        }
        
        # Authenticated path — uses SPN2 API with IA S3-like keys
        if getattr(config, 'IA_KEYS', {}).get('access') and getattr(config, 'IA_KEYS', {}).get('secret'):
            headers['Authorization'] = f"LOW {config.IA_KEYS['access']}:{config.IA_KEYS['secret']}"
            data = {'url': url, 'capture_all': '1'}
            response = session.post(
                'https://web.archive.org/save',
                headers=headers, data=data, timeout=(CONNECT_TIMEOUT, 120))
            
            try:
                resp_json = response.json()
            except Exception:
                resp_json = {}

            if response.status_code == 200 and resp_json.get('job_id'):
                job_id = resp_json['job_id']

                if not confirm:
                    # Leave success False so the URL lands on the queue below.
                    _wblog('info', f"SPN2 job submitted: {job_id} — queued for confirmation next run")
                else:
                    _wblog('info', f"SPN2 job submitted: {job_id} — polling for result...")

                    timed_out = True
                    for _ in range(18):
                        time.sleep(5)
                        try:
                            status_r = session.get(
                                f'https://web.archive.org/save/status/{job_id}',
                                headers=headers, timeout=(CONNECT_TIMEOUT, 30))
                            status = status_r.json()
                        except Exception as poll_err:
                            _wblog('debug', f"[SPN2 Polling] Raw tracking exception: {repr(poll_err)}")
                            continue

                        s = status.get('status', '')
                        if s == 'success':
                            archived = 'https://web.archive.org/web/' + status.get('timestamp', '') + '/' + url
                            _wblog('info', f"[SPN2] SUCCESS — Archived: {archived}")
                            _dequeue_wayback(url)
                            success = True
                            timed_out = False
                            break
                        elif s == 'error':
                            _wblog('error', f"[SPN2] RAW JOB ERROR JSON: {status}")
                            timed_out = False
                            break

                    if timed_out:
                        _wblog('error', f"[SPN2] FAIL — Job timed out (>90 s) for: {url}")
            elif resp_json.get('status_ext') == 'error:too-many-daily-captures':
                _wblog('info', f"[SPN2] SUCCESS — Already captured today: {url}")
                _dequeue_wayback(url)
                success = True
            else:
                _wblog('error', f"[SPN2] RAW HTTP ERROR — Status: {response.status_code} | Body: {response.text}")
        
        else:
            # Unauthenticated fallback
            fallback_resp = session.get(
                f'https://web.archive.org/save/{url}',
                headers={'User-Agent': 'PID-Bangladesh-UploadBot/2.0'},
                timeout=(CONNECT_TIMEOUT, 60), allow_redirects=True)

            if not confirm:
                # Leave success False so the URL lands on the queue below.
                _wblog('info', f"[Unauth] Save requested — queued for confirmation next run: {url}")
            else:
                time.sleep(5)
                check = session.get(
                    f'https://archive.org/wayback/available?url={url}',
                    timeout=(CONNECT_TIMEOUT, 15))

                snapshots = check.json().get('archived_snapshots', {})
                if snapshots:
                    confirmed_url = snapshots.get('closest', {}).get('url', '')
                    _wblog('info', f"[Unauth] SUCCESS — Confirmed archived: {confirmed_url}")
                    _dequeue_wayback(url)
                    success = True
                else:
                    _wblog('error', f"[Unauth] RAW FAIL — Save Status: {fallback_resp.status_code} | Check Status: {check.status_code} | Check Body: {check.text}")
                
    except Exception as e:
        _wblog('error', f"[Archive] RAW SYSTEM EXCEPTION for {url}: {repr(e)}")

    if not success and _enqueue_on_fail:
        _enqueue_wayback(url)

    return success


def retry_wayback_queue(budget_seconds=RETRY_BUDGET):
    """Confirm or re-submit pending URLs, within a wall-clock budget.

    Most entries were submitted by the previous run and have landed by now, so
    they cost one cheap availability check. Whatever the budget does not reach
    stays queued for the next run — that is what the persistent queue is for.
    """
    queue = _load_wayback_queue()
    if not queue:
        _wblog('info', "Wayback queue is empty — nothing to retry.")
        return

    _wblog('info', f"\nConfirming {len(queue)} pending archive(s) (budget: {budget_seconds}s)...")
    deadline = time.monotonic() + budget_seconds
    still_pending = []
    confirmed = 0

    for idx, url in enumerate(queue, 1):
        if time.monotonic() >= deadline:
            _wblog('info', f"  Budget spent — leaving {len(queue) - idx + 1} URL(s) queued for next run.")
            still_pending.extend(queue[idx - 1:])
            break

        _wblog('info', f"Processing item [{idx}/{len(queue)}]: {url}")
        try:
            if is_archived(url):
                _wblog('info', f"  Already archived: {url}")
                confirmed += 1
                continue   # no SPN2 call, no sleep — this is the common case

            if archive_to_wayback(url, _enqueue_on_fail=False):
                _wblog('info', f"  Retry successful: {url}")
                confirmed += 1
            else:
                still_pending.append(url)
        except Exception as exc:
            _wblog('error', f"  [Loop Engine] RAW EXCEPTION for {url}: {repr(exc)}")
            still_pending.append(url)
            continue

        # Space out actual SPN2 submissions; confirmations above skip this.
        if idx < len(queue):
            _wblog('info', "  Sleeping 15 seconds before launching next request...")
            time.sleep(15)

    _save_wayback_queue(still_pending)
    _wblog('info', f"Wayback retry cycle finished. {confirmed} confirmed, {len(still_pending)} remains in file.")
