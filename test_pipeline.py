# test_pipeline.py
# Offline self-checks for the failure modes that take the bot down silently:
#   1. the Commons log page outgrowing $wgMaxArticleSize
#   2. the Wayback tail holding the hourly job open long past its work
#   3. the PIDDateData ledger outgrowing $wgMaxArticleSize, after which every
#      hourly run re-uploads the same images under new filenames
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

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


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


# ── 3. Upload ledger (PIDDateData) ────────────────────────────────────────────
# The yearly ledger page outgrew $wgMaxArticleSize, every save was rejected,
# and each hourly run re-uploaded the same images under new AI filenames.

def _import_uploader():
    os.environ.setdefault("PYWIKIBOT_NO_USER_CONFIG", "1")
    from src import uploader
    return uploader


class LedgerPage(FakePage):
    """A ledger page that already holds `existing` records."""
    existing = None

    def __init__(self, site, title):
        super().__init__(site, title)
        if LedgerPage.existing is not None:
            self.text = json.dumps(LedgerPage.existing)

    def exists(self):
        return LedgerPage.existing is not None


def _ledger_record(i):
    return ("https://objectstorage.example/o/office-pressinform/2026/9/%032d.jpg" % i,
            "2026-10-08 18:31:00", "%032x" % i, "", "Some descriptive filename, 2026-10-08.jpg")


def test_ledger_is_monthly():
    from datetime import datetime
    title = config.pid_date_data_title(datetime(2026, 10, 10, 13, 0))
    assert title == "User:PID-Bangladesh-UploadBot/PIDDateData/2026-10.json", title


def test_ledger_creates_and_appends_to_month_page():
    uploader = _import_uploader()

    class Fake:
        Page = LedgerPage
    uploader.pywikibot = Fake
    FakePage.instances.clear()
    LedgerPage.existing = None

    assert uploader.batch_update_pid_date_data(object(), [_ledger_record(1), _ledger_record(2)])
    page = FakePage.instances[-1]
    assert page.saved and page.title_str.endswith(".json")
    assert [r["checksum"] for r in json.loads(page.text)] == ["%032x" % 1, "%032x" % 2]


def test_ledger_refuses_oversized_save():
    uploader = _import_uploader()

    class Fake:
        Page = LedgerPage
    uploader.pywikibot = Fake
    FakePage.instances.clear()
    # Roughly the 2026.json page as it stood: 301 bytes under the limit.
    record = dict(zip(("url", "date", "checksum", "unique_id", "filename"), _ledger_record(0)))
    per_record = len(json.dumps(record, separators=(",", ":"))) + 1
    LedgerPage.existing = [record] * (config.MAX_PAGE_BYTES // per_record)

    ok = uploader.batch_update_pid_date_data(object(), [_ledger_record(1)])
    assert ok is False, "an oversized ledger save must report failure"
    assert not FakePage.instances[-1].saved
    LedgerPage.existing = None


class LedgerSession:
    """Serves allpages listings and action=raw page bodies like Commons."""

    def __init__(self, pages, fail_raw=False):
        self.pages = pages
        self.fail_raw = fail_raw

    def get(self, url, params=None, **kw):
        if params.get("list") == "allpages":
            prefix = "User:" + params["apprefix"]
            titles = [{"title": t} for t in self.pages if t.startswith(prefix)]
            return FakeResponse({"query": {"allpages": titles}})
        if self.fail_raw:
            return FakeResponse({}, status_code=503)
        return FakeResponse(self.pages[params["title"]])


def _ledger_entries(*names):
    return [{"url": f"https://pressinform.gov.bd/{n}.jpg", "checksum": n} for n in names]


def test_scraper_reads_yearly_and_monthly_ledgers():
    from src import scraper
    base = "User:" + config.PID_DATE_DATA_PREFIX
    scraper.session = LedgerSession({
        base + "2026.json": _ledger_entries("a", "b"),
        base + "2026-10.json": _ledger_entries("c"),
        base + "2025.json": _ledger_entries("old"),
    })
    urls, checksums = scraper.fetch_wikimedia_data(2026)
    assert checksums == {"a", "b", "c"}, checksums
    assert scraper.normalize_url("https://pressinform.gov.bd/c.jpg") in urls


def test_scraper_raises_when_ledger_unreadable():
    """An unreadable ledger must stop the run, not look like 'nothing uploaded'."""
    from src import scraper
    scraper.session = LedgerSession(
        {"User:" + config.PID_DATE_DATA_PREFIX + "2026.json": []}, fail_raw=True)
    try:
        scraper.fetch_wikimedia_data(2026)
    except Exception:
        return
    raise AssertionError("fetch_wikimedia_data swallowed a failed ledger read")


def test_upload_skips_byte_identical_file():
    import numpy as np
    uploader = _import_uploader()

    class Existing:
        def title(self, with_ns=True):
            return ("File:" if with_ns else "") + "Earlier upload.jpg"

    class Site:
        def allimages(self, sha1=None, total=None):
            assert len(sha1) == 40
            return iter([Existing()])

    class NeverUpload:
        def __init__(self, *a):
            raise AssertionError("uploaded a file Commons already has")

    image = np.zeros((8, 8, 3), dtype=np.uint8)
    ok, error, name = uploader.upload_to_commons(
        Site(), NeverUpload, image, "New AI title.jpg", "jpg", None, "desc")
    assert ok is False and "already exists" in error.lower(), error
    assert name == "Earlier upload.jpg", name


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed")
