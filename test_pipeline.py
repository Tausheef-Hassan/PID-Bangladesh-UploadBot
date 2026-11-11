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


def test_ledger_never_appends_a_recorded_url_again():
    """Overlapping or stale-read runs re-recorded the same photo up to 17 times
    on PIDDateData/2026-11.json; a URL already on the page must be skipped."""
    uploader = _import_uploader()

    class Fake:
        Page = LedgerPage
    uploader.pywikibot = Fake
    FakePage.instances.clear()
    first = dict(zip(("url", "date", "checksum", "unique_id", "filename"), _ledger_record(1)))
    LedgerPage.existing = [first]
    try:
        # Only already-recorded URLs: report success without editing the page.
        assert uploader.batch_update_pid_date_data(object(), [_ledger_record(1)])
        assert not FakePage.instances[-1].saved

        # Mixed batch (with a repeat inside it): only the new URL is appended, once.
        assert uploader.batch_update_pid_date_data(
            object(), [_ledger_record(1), _ledger_record(2), _ledger_record(2)])
        page = FakePage.instances[-1]
        assert page.saved
        assert [r["checksum"] for r in json.loads(page.text)] == ["%032x" % 1, "%032x" % 2]
    finally:
        LedgerPage.existing = None


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
    """Serves allpages listings and latest-revision page bodies like the
    Commons API. index.php?action=raw is refused: the CDN can serve it stale."""

    def __init__(self, pages, fail_raw=False):
        self.pages = pages
        self.fail_raw = fail_raw

    def get(self, url, params=None, **kw):
        assert params.get("action") != "raw", "ledger read through cacheable action=raw"
        if params.get("list") == "allpages":
            prefix = "User:" + params["apprefix"]
            titles = [{"title": t} for t in self.pages if t.startswith(prefix)]
            return FakeResponse({"query": {"allpages": titles}})
        if self.fail_raw:
            return FakeResponse({}, status_code=503)
        assert params.get("prop") == "revisions", params
        title = params["titles"]
        if title not in self.pages:
            return FakeResponse({"query": {"pages": [{"title": title, "missing": True}]}})
        content = json.dumps(self.pages[title])
        return FakeResponse({"query": {"pages": [{"title": title, "revisions": [
            {"slots": {"main": {"content": content}}}]}]}})


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


def test_scraper_raises_when_ledger_page_missing():
    """A listed page that the API reports missing must also stop the run."""
    from src import scraper
    base = "User:" + config.PID_DATE_DATA_PREFIX

    class Vanishing(LedgerSession):
        def get(self, url, params=None, **kw):
            if params.get("list") == "allpages":
                return FakeResponse({"query": {"allpages": [{"title": base + "2026-11.json"}]}})
            return super().get(url, params, **kw)

    scraper.session = Vanishing({})
    try:
        scraper.fetch_wikimedia_data(2026)
    except Exception:
        return
    raise AssertionError("fetch_wikimedia_data treated a missing ledger page as empty")


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


def test_upload_log_lists_urls_already_on_commons():
    """URLs the local log says are on Commons are skipped before OCR, so a
    failed ledger save never sends the same photo through paid translation."""
    from src import upload_log

    saved = config.UPLOAD_LOG_PATH
    with tempfile.TemporaryDirectory() as tmp:
        config.UPLOAD_LOG_PATH = os.path.join(tmp, "upload_log.jsonl")
        try:
            assert upload_log.uploaded_urls() == {}
            upload_log.record("a" * 40, "uploaded", "A.jpg", "https://pid/a.jpg", "A.jpg")
            upload_log.record("b" * 40, "commons_duplicate", "B new.jpg", "https://pid/b.jpg", "B.jpg")
            upload_log.record("c" * 40, "local_duplicate", "C new.jpg", "https://pid/c.jpg", "C.jpg")
            upload_log.record("d" * 40, "title_conflict", "D.jpg", "https://pid/d.jpg", "D.jpg")
            upload_log.record("e" * 40, "failed", "E.jpg", "https://pid/e.jpg")
            upload_log.record("f" * 40, "imported", "F.jpg", "https://pid/f.jpg", "F.jpg")
            upload_log.record("", "commons_import_complete", "", "", "1")
            assert upload_log.uploaded_urls() == {
                "https://pid/a.jpg": "A.jpg",
                "https://pid/b.jpg": "B.jpg",
                "https://pid/c.jpg": "C.jpg",
                "https://pid/f.jpg": "F.jpg",
            }
        finally:
            config.UPLOAD_LOG_PATH = saved


def test_replacements_prefer_tool_data_dir():
    """The webservice edits $TOOL_DATA_DIR/translation_replacements.tsv; the
    bot must read that copy, and fall back to the one next to main.py."""
    from src import translator

    saved = config.TRANSLATION_REPLACEMENTS_PATHS
    with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as app:
        data_tsv = os.path.join(data, "translation_replacements.tsv")
        app_tsv = os.path.join(app, "translation_replacements.tsv")
        with open(app_tsv, "w", encoding="utf-8") as f:
            f.write("# comment\nআবুল|||Abul\n")
        config.TRANSLATION_REPLACEMENTS_PATHS = [data_tsv, app_tsv]
        try:
            assert translator.load_translation_replacements() == [("আবুল", "Abul")]

            with open(data_tsv, "w", encoding="utf-8") as f:
                f.write("তারেক|||Tarique\nno separator here\n\n")
            assert translator.load_translation_replacements() == [("তারেক", "Tarique")]

            os.remove(data_tsv)
            os.remove(app_tsv)
            assert translator.load_translation_replacements() == []
        finally:
            config.TRANSLATION_REPLACEMENTS_PATHS = saved


if __name__ == "__main__":
    tests =[v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed")
