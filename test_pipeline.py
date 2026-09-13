# test_pipeline.py
# Offline self-checks for the two failure modes that take the bot down silently:
#   1. the Commons log page outgrowing $wgMaxArticleSize
#   2. the Wayback tail holding the hourly job open long past its work
#
# Run with:  python test_pipeline.py

import importlib
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from src import commons_log, run_state, translator, wayback
from panel import app as panel_app, wikitext


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


# ── 3. Gemini model ladder ────────────────────────────────────────────────────

class FakeGemini:
    """Plays back scripted replies: a str is returned as resp.text, an
    Exception is raised. Doubles as its own `.models` namespace."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls += 1
        reply = self.replies.pop(0) if self.replies else RuntimeError("script exhausted")
        if isinstance(reply, Exception):
            raise reply
        return type("Resp", (), {"text": reply})()


def _no_sleep():
    translator.sleep = lambda s: None


def test_ladder_retries_transient_on_the_same_model():
    _no_sleep()
    free = FakeGemini(RuntimeError("429 resource exhausted"), "Cabinet meeting held")
    out, status = translator.translate_text(free, FakeGemini(), None, "বাংলা", 1)

    assert status == "Success", status
    assert out == "Cabinet meeting held"
    assert free.calls == 2, f"a 429 must be retried on the free tier, not skipped ({free.calls})"


def test_ladder_drops_to_the_paid_client_on_a_hard_error():
    _no_sleep()
    free = FakeGemini(ValueError("permission denied"))   # not transient
    paid = FakeGemini("Paid answer")
    out, status = translator.translate_text(free, paid, None, "বাংলা", 2)

    assert (out, status) == ("Paid answer", "Success"), (out, status)
    assert free.calls == 1, "a hard error must not burn retries on the same model"


def test_over_long_title_is_resampled():
    _no_sleep()
    free = FakeGemini("x" * 300, "Cabinet meeting in Dhaka 2026-09-11")
    title, status = translator.generate_title(
        free, FakeGemini(), "description", "2026-09-11", 3, "jpg")

    assert status == "Success", status
    assert title == "Cabinet meeting in Dhaka 2026-09-11.jpg", title
    assert len(title.encode()) <= 244, "Commons rejects titles past its byte cap"
    assert free.calls == 2, "an over-long title must cost another sample"


def test_exhausted_ladder_reports_an_error_instead_of_raising():
    _no_sleep()
    dead = lambda: FakeGemini(*[RuntimeError("boom")] * 20)
    out, status = translator.translate_text(dead(), dead(), None, "বাংলা", 4)

    assert out == ""
    assert status.startswith("Error:"), status


# ── 4. Run state ──────────────────────────────────────────────────────────────

def _isolate_state(tmpdir):
    config.RUN_STATE_PATH = os.path.join(tmpdir, "run_state.json")


def test_successful_run_is_recorded():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_state(tmp)
        with run_state.record_run() as run:
            run["uploaded"] = 12
            run["scraped"] = 40

        (rec,) = run_state.load()
        assert rec["status"] == "succeeded", rec
        assert rec["uploaded"] == 12
        assert rec["finished_at"], "a finished run must carry a finish time"


def test_crash_is_recorded_and_still_raises():
    """A crash must reach Toolforge (so the job is marked failed) AND be logged."""
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_state(tmp)
        try:
            with run_state.record_run():
                raise RuntimeError("gemini exploded")
        except RuntimeError:
            pass
        else:
            assert False, "record_run swallowed the exception"

        (rec,) = run_state.load()
        assert rec["status"] == "failed", rec
        assert "gemini exploded" in rec["error"]


def test_killed_run_leaves_a_running_row():
    """The panel's Stop button kills the pod; the row written at start is the
    only evidence the run ever happened."""
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_state(tmp)
        run = run_state.record_run()
        run.__enter__()          # start the run, then walk away as SIGKILL would

        (rec,) = run_state.load()
        assert rec["status"] == "running", rec
        assert rec["finished_at"] is None, "an interrupted run must have no finish time"

        run.__exit__(None, None, None)   # close it out inside the temp dir


def test_history_is_capped():
    with tempfile.TemporaryDirectory() as tmp:
        _isolate_state(tmp)
        with open(config.RUN_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump([{"started_at": str(i)} for i in range(500)], f)
        with run_state.record_run():
            pass
        assert len(run_state.load()) == run_state.MAX_RECORDS


# ── 5. Panel controls ─────────────────────────────────────────────────────────

def _panel_client(tmp, token=None):
    config.PANEL_KEY_PATH = os.path.join(tmp, "panel.key")
    config.RUN_STATE_PATH = os.path.join(tmp, "run_state.json")
    if token:
        with open(config.PANEL_KEY_PATH, "w", encoding="utf-8") as f:
            f.write(token)
    importlib.reload(panel_app)
    return panel_app.app.test_client()


def test_missing_key_disables_controls_rather_than_opening_them():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp)          # no panel.key at all
        assert client.post("/run").status_code == 503, "no key must disable, not allow"
        assert client.post("/stop").status_code == 503


def test_wrong_key_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, token="correct-horse")
        assert client.post("/run").status_code == 403
        client.post("/sign-in", data={"token": "not-it"})
        assert client.post("/run").status_code == 403, "a bad key granted access"


def test_read_only_views_stay_public():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, token="correct-horse")
        for route in ("/", "/uploads", "/queue", "/replacements", "/sign-in",
                      "/partials/dashboard", "/partials/log", "/healthz"):
            assert client.get(route).status_code == 200, route
        assert "Run now" not in client.get("/").get_data(as_text=True), \
            "controls must not render for a signed-out visitor"


def _seed_log(tmp, text, suffix="out"):
    config.CREDS_DIR = tmp
    with open(os.path.join(tmp, f"{config.JOB_NAME}.{suffix}"), "w",
              encoding="utf-8") as f:
        f.write(text)


LOG_SAMPLE = (
    "STEP 2: Processing image...\n"
    "Row 1: Upload successful\n"
    "Row 2: Retryable error on AI Studio primary: 429 RESOURCE_EXHAUSTED\n"
    "Sanitized OCR Data: বাংলা টেক্সট\n"
)


def test_log_classifies_trouble_and_success():
    """A wall of output is unreadable until the bad lines stand out."""
    with tempfile.TemporaryDirectory() as tmp:
        _seed_log(tmp, LOG_SAMPLE)
        lines, total = panel_app.log_lines()

        def kind_of(fragment):
            for line in lines:
                if fragment in line["text"]:
                    return line["kind"]
            raise AssertionError(f"{fragment!r} missing from {lines}")

        assert total >= 4
        assert kind_of("RESOURCE_EXHAUSTED") == "bad"
        assert kind_of("Upload successful") == "good"
        assert kind_of("STEP 2") == "head"
        assert kind_of("বাংলা") == "", "ordinary lines must not be coloured"


def test_log_filters_by_search_and_by_problems():
    with tempfile.TemporaryDirectory() as tmp:
        _seed_log(tmp, LOG_SAMPLE)

        found, _ = panel_app.log_lines(query="row 1")
        assert len(found) == 1 and "Upload successful" in found[0]["text"], found
        assert panel_app.log_lines(query="ROW 1")[0], "search must ignore case"

        problems, total = panel_app.log_lines(errors_only=True)
        assert len(problems) == 1, problems
        assert "429" in problems[0]["text"]
        assert total >= 4, "total must count the whole tail, not the filtered set"


def test_log_reads_the_error_stream_separately():
    with tempfile.TemporaryDirectory() as tmp:
        _seed_log(tmp, "stdout line\n")
        _seed_log(tmp, "Traceback (most recent call last):\n", suffix="err")

        out, _ = panel_app.log_lines(stream="out")
        err, _ = panel_app.log_lines(stream="err")
        assert any("stdout line" in l["text"] for l in out)
        assert any("Traceback" in l["text"] for l in err), err
        assert not any("Traceback" in l["text"] for l in out)


def test_log_survives_a_missing_file():
    """The panel comes up before the job has ever run."""
    with tempfile.TemporaryDirectory() as tmp:
        config.CREDS_DIR = tmp
        lines, total = panel_app.log_lines()
        assert lines == [] or all(not l["text"] for l in lines), lines


# ── 6. Commons description editing ────────────────────────────────────────────

PAGE = """=={{int:filedesc}}==
{{Information
 |description = {{bn|1=আজ ঢাকায় সভা।}}{{en|1=A meeting in Dhaka today.\
{{Auto-translated PID English description}}}}
 |date = {{Date-PID|2026-09-13 15:18:00}}
 |source = {{Source-PID | url=https://example.org/a.jpg}}
 |author = {{Institution:Press Information Department}}
 |permission =
 |other versions =
}}
=={{int:license-header}}==
{{PD-BDGov-PID}}
[[Category: Uploaded with pypan]]
[[Category:Some topic]]
"""


def test_reads_english_without_the_marker():
    english, marked = wikitext.read_english(PAGE)
    assert english == "A meeting in Dhaka today.", repr(english)
    assert marked is True
    assert wikitext.MARKER not in english


def test_editing_english_leaves_everything_else_alone():
    updated = wikitext.write_english(PAGE, "A cabinet meeting in Dhaka.", False)
    english, marked = wikitext.read_english(updated)

    assert english == "A cabinet meeting in Dhaka."
    assert marked is False, "reviewing must drop the auto-translated marker"
    # The Bengali, the date, the source and the licence are not ours to touch.
    for fragment in ("আজ ঢাকায় সভা।",
                     "Date-PID|2026-09-13", "Source-PID", "PD-BDGov-PID"):
        assert fragment in updated, fragment


def test_marker_can_be_put_back():
    once = wikitext.write_english(PAGE, "Rewritten.", False)
    twice = wikitext.write_english(once, "Rewritten.", True)
    assert wikitext.read_english(twice) == ("Rewritten.", True)


def test_refuses_to_mangle_a_page_it_cannot_parse():
    """Writing a broken description to Commons is worse than refusing."""
    for bad_page in ("no templates here", "{{en|1=unclosed", ""):
        try:
            wikitext.read_english(bad_page)
        except wikitext.Unparseable:
            continue
        raise AssertionError(f"parsed nonsense: {bad_page!r}")


def test_refuses_braces_and_empty_descriptions():
    for bad in ("", "   ", "text with {{a template}}"):
        try:
            wikitext.write_english(PAGE, bad, False)
        except wikitext.Unparseable:
            continue
        raise AssertionError(f"accepted dangerous description: {bad!r}")


def test_categories_separate_topic_from_automatic():
    assert wikitext.read_categories(PAGE) == ["Some topic"], \
        "template-driven categories must not look hand-editable"


def test_writing_categories_keeps_the_automatic_ones():
    updated = wikitext.write_categories(
        PAGE, ["Zubaida Rahman", "  Category:Novo Theatre  ", "", "Zubaida Rahman"])

    assert "[[Category: Uploaded with pypan]]" in updated, "automatic category dropped"
    assert wikitext.read_categories(updated) == ["Zubaida Rahman", "Novo Theatre"], \
        "expected de-duplication and the Category: prefix stripped"
    assert "Some topic" not in updated, "replaced topic category still present"


def test_category_names_cannot_smuggle_markup():
    try:
        wikitext.write_categories(PAGE, ["Fine", "Bad]]{{Delete}}"])
    except wikitext.Unparseable:
        return
    raise AssertionError("category markup injection was accepted")


def test_env_var_beats_the_key_file():
    """Secrets belong in envvars, off the shared filesystem — so $PANEL_KEY
    must win over a stale panel.key left behind on NFS."""
    with tempfile.TemporaryDirectory() as tmp:
        config.PANEL_KEY_PATH = os.path.join(tmp, "panel.key")
        with open(config.PANEL_KEY_PATH, "w", encoding="utf-8") as f:
            f.write("from-the-file")

        os.environ["PANEL_KEY"] = "from-the-environment"
        try:
            assert panel_app.panel_token() == "from-the-environment"
        finally:
            del os.environ["PANEL_KEY"]
        assert panel_app.panel_token() == "from-the-file", "file fallback broken"


def test_no_key_anywhere_disables_controls():
    with tempfile.TemporaryDirectory() as tmp:
        config.PANEL_KEY_PATH = os.path.join(tmp, "absent.key")
        os.environ.pop("PANEL_KEY", None)
        assert panel_app.panel_token() == ""


def test_every_page_carries_the_navbar():
    """Each area is a real page, not another card bolted onto the dashboard."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, token="correct-horse")
        for route in ("/", "/uploads", "/queue", "/replacements"):
            body = client.get(route).get_data(as_text=True)
            assert 'class="nav"' in body, f"{route} has no navbar"
            assert body.count('route active') == 1, \
                f"{route} did not mark exactly one nav item as current"


def test_source_proxy_refuses_hosts_it_does_not_scrape():
    """The proxy sits inside Toolforge's network. Anything but the PID source
    hosts must be refused, not fetched."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, token="correct-horse")
        for hostile in (
            "http://169.254.169.254/latest/meta-data/",   # cloud metadata
            "http://127.0.0.1:5173/healthz",              # itself
            "https://example.com/cat.jpg",
            "https://pressinform.gov.bd.evil.test/x.jpg",  # suffix smuggling
            "file:///etc/passwd",
        ):
            status = client.get("/source-image", query_string={"url": hostile}).status_code
            assert status == 400, f"proxy accepted {hostile} ({status})"


def test_source_proxy_allows_the_real_source_hosts():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, token="correct-horse")
        for ok in ("https://pressinform.gov.bd/a.jpg",
                   "https://objectstorage.ap-dcc-gazipur-1.oraclecloud15.com/n/x/a.jpg",
                   "https://web.archive.org/web/2026/https://pressinform.gov.bd/a.jpg"):
            assert panel_app._allowed_source(ok), f"proxy would refuse {ok}"


def test_wayback_retry_needs_the_key():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, token="correct-horse")
        assert client.post("/wayback/retry").status_code == 403


def test_gallery_survives_commons_being_down():
    """Commons is a third party; the panel must degrade, not 500."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp)
        broken = lambda *a, **k: (_ for _ in ()).throw(OSError("commons down"))
        original = panel_app.commons.recent_uploads
        panel_app.commons.recent_uploads = broken
        try:
            page = client.get("/partials/gallery")
        finally:
            panel_app.commons.recent_uploads = original
        assert page.status_code == 200, "a Commons outage took the panel down"
        assert b"reach Commons" in page.data, page.data[:200]


def test_zero_upload_runs_still_draw_a_tick():
    """A quiet hour is information: it must not look like missing data."""
    ticks = panel_app.heartbeat([
        {"started_at": None, "status": "succeeded", "uploaded": 0},
        {"started_at": None, "status": "succeeded", "uploaded": 10},
    ])
    assert ticks[0]["height"] > 0, "a zero-upload run rendered as a gap"
    assert ticks[1]["height"] == 100


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed")
