# test_pipeline.py
# Offline self-checks for the two failure modes that take the bot down silently:
#   1. the Commons log page outgrowing $wgMaxArticleSize
#   2. the Wayback tail holding the hourly job open long past its work
#
# Run with:  python test_pipeline.py

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from src import commons_log, wayback


# ── Fakes ─────────────────────────────────────────────────────────────────────

class FakePage:
    """Stands in for pywikibot.Page: records what the bot would save."""
    instances = []

    def __init__(self, site, title):
        self.title_str = title
        self.text = ""
        self.saved = False
        FakePage.instances.append(self)

    def exists(self):
        return False

    def save(self, summary="", bot=True):
        self.saved = True


class FakePywikibot:
    Page = FakePage


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Counts calls so we can assert the hot path never polls."""

    def __init__(self, get_payload=None):
        self.gets = []
        self.posts = []
        self.get_payload = get_payload or {}

    def post(self, url, **kw):
        self.posts.append(url)
        return FakeResponse({"job_id": "job-123"})

    def get(self, url, **kw):
        self.gets.append(url)
        return FakeResponse(self.get_payload)


def row(**over):
    base = {
        "unique_id": "PID_20260911_abc_0001",
        "date": "2026-09-11 10:00:00",
        "image_url": "https://pressinform.gov.bd/a.jpg",
        "detail_url": "",
        "ocr_text": "বাংলা টেক্সট",
        "ocr_status": "Success",
        "translation": "Bengali text",
        "translation_status": "Success",
        "filename": "Something 2026-09-11.jpg",
        "filename_status": "Success",
        "pid_date_data_info": "JSON Tabular: ...",
        "pid_date_data_status": "Success (queued)",
        "wikitext_description": "=={{int:filedesc}}==\n" + ("x" * 500),
        "upload_status": "Success",
    }
    base.update(over)
    return base


# ── 1. Commons log ────────────────────────────────────────────────────────────

def test_log_skips_empty_runs():
    """An hourly cron with nothing to do must not write a record."""
    FakePage.instances.clear()
    commons_log.pywikibot = FakePywikibot
    assert commons_log.log_to_commons(object()) is True
    assert FakePage.instances == [], "empty run wrote a log entry"


def test_log_is_daily_and_drops_wikitext():
    FakePage.instances.clear()
    commons_log.pywikibot = FakePywikibot
    rows = [row(), row(upload_status="Failed: timeout")]

    assert commons_log.log_to_commons(object(), rows, 1, 1, 2) is True
    page = FakePage.instances[-1]

    # Daily page, not monthly: a month of hourly runs overruns the 2 MB limit.
    assert page.title_str.startswith("User:PID-Bangladesh-UploadBot/Log/")
    date_part = page.title_str.rsplit("/", 1)[1].removesuffix(".json")
    time.strptime(date_part, "%Y-%m-%d")   # raises if not a YYYY-MM-DD page

    data = json.loads(page.text)
    items = data[0]["items"]
    assert len(items) == 2
    assert "wikitext_description" not in items[0], "heaviest field still logged"
    assert items[0]["ocr_text"] == "বাংলা টেক্সট", "Bengali OCR must survive"
    assert items[1]["upload_status"] == "Failed: timeout"
    # The caller's rows are untouched — main.py still needs the description.
    assert "wikitext_description" in rows[0]


# ── 2. Wayback hot path ───────────────────────────────────────────────────────

def _isolate_queue(tmpdir):
    config.WAYBACK_QUEUE_PATH = os.path.join(tmpdir, "wayback_pending.json")


def test_hot_path_submits_without_polling():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_queue(tmp)
        config.IA_KEYS = {"access": "k", "secret": "s"}
        fake = FakeSession()
        wayback.session = fake

        started = time.monotonic()
        ok = wayback.archive_to_wayback("https://example.org/a.jpg", confirm=False)
        elapsed = time.monotonic() - started

        assert ok is False, "unconfirmed submit must not report success"
        assert len(fake.posts) == 1, "should submit exactly one save job"
        assert fake.gets == [], f"hot path polled: {fake.gets}"
        assert elapsed < 1, f"hot path blocked for {elapsed:.1f}s"

        queued = json.load(open(config.WAYBACK_QUEUE_PATH, encoding="utf-8"))
        assert queued == ["https://example.org/a.jpg"], queued


def test_confirm_path_still_polls():
    """The retry path must keep waiting for a verdict — that is its whole job."""
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_queue(tmp)
        config.IA_KEYS = {"access": "k", "secret": "s"}

        class PollingSession(FakeSession):
            def get(self, url, **kw):
                self.gets.append(url)
                return FakeResponse({"status": "success", "timestamp": "20260911"})

        fake = PollingSession()
        wayback.session = fake
        real_sleep, time.sleep = time.sleep, lambda s: None
        try:
            ok = wayback.archive_to_wayback("https://example.org/b.jpg")
        finally:
            time.sleep = real_sleep

        assert ok is True
        assert any("save/status/" in g for g in fake.gets), "confirm path stopped polling"


def test_retry_confirms_cheaply_and_honours_budget():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_queue(tmp)
        urls = [f"https://example.org/{i}.jpg" for i in range(5)]
        with open(config.WAYBACK_QUEUE_PATH, "w", encoding="utf-8") as f:
            json.dump(urls, f)

        # Every URL already landed, so no SPN2 POST should be issued at all.
        fake = FakeSession(get_payload={"archived_snapshots": {"closest": {"url": "x"}}})
        wayback.session = fake

        started = time.monotonic()
        wayback.retry_wayback_queue()
        elapsed = time.monotonic() - started

        assert fake.posts == [], f"re-submitted already-archived URLs: {fake.posts}"
        assert elapsed < 5, f"confirmation pass slept anyway ({elapsed:.1f}s)"
        assert json.load(open(config.WAYBACK_QUEUE_PATH, encoding="utf-8")) == []


def test_retry_budget_leaves_remainder_queued():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_queue(tmp)
        urls = [f"https://example.org/{i}.jpg" for i in range(4)]
        with open(config.WAYBACK_QUEUE_PATH, "w", encoding="utf-8") as f:
            json.dump(urls, f)

        # Nothing archived and nothing submittable, so each item costs a turn.
        fake = FakeSession(get_payload={"archived_snapshots": {}})
        wayback.session = fake
        config.IA_KEYS = {"access": None, "secret": None}
        real_sleep, time.sleep = time.sleep, lambda s: None
        try:
            wayback.retry_wayback_queue(budget_seconds=0)
        finally:
            time.sleep = real_sleep

        left = json.load(open(config.WAYBACK_QUEUE_PATH, encoding="utf-8"))
        assert left == urls, f"a spent budget must requeue everything, got {left}"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed")
