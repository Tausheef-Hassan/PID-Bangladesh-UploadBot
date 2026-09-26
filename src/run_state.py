# run_state.py
# Per-run outcome records, written to $TOOL_DATA_DIR/run_state.json.
#
# The Toolforge Jobs API can say whether the pod is alive; only the bot knows
# how many images it actually uploaded. The control panel reads both.

import json
import os
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone

import config

# Three days of hourly runs is what the panel's heartbeat shows; keep a little
# more than that so the file stays small enough to rewrite on every run.
MAX_RECORDS = 200


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def load():
    """Return recorded runs, oldest first. Never raises — the panel polls this."""
    try:
        with open(config.RUN_STATE_PATH, encoding='utf-8') as f:
            records = json.load(f)
        return records if isinstance(records, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"Warning: could not read run state: {e!r}")
        return []


def _save(records):
    """Write atomically — the panel reads this file while the bot is writing it."""
    tmp = config.RUN_STATE_PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(records[-MAX_RECORDS:], f, ensure_ascii=False, indent=1)
        os.replace(tmp, config.RUN_STATE_PATH)
    except Exception as e:
        print(f"Warning: could not save run state: {e!r}")


@contextmanager
def record_run():
    """Record one pipeline run. Yields a dict for the caller to fill in.

    The row is written once at start and again at the end, so a run killed
    mid-flight — the panel's Stop button, an OOM kill — leaves a 'running' row
    with no finish time rather than leaving no trace at all.
    """
    record = {
        'started_at': _now(),
        'finished_at': None,
        'status': 'running',
        'scraped': 0,
        'uploaded': 0,
        'duplicates': 0,
        'failed': 0,
        'error': '',
    }
    _save(load() + [record])

    try:
        yield record
        record['status'] = 'succeeded'
    except BaseException as e:
        record['status'] = 'failed'
        record['error'] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        raise
    finally:
        record['finished_at'] = _now()
        history = load()
        # Replace our own row in place; match on start time rather than position
        # so a concurrent run appending its own row cannot shift us.
        for i in range(len(history) - 1, -1, -1):
            if history[i].get('started_at') == record['started_at']:
                history[i] = record
                break
        else:
            history.append(record)
        _save(history)
