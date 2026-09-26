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

def _panel_client(tmp, owner=None, granted=(), signed_in_as=None):
    """A test client, optionally with an owner, grants and a signed-in account.

    wikiauth is reloaded first: the workbench fakes replace its functions on the
    module itself and there is no teardown here, so without this a later test
    quietly runs against another test's stub and passes for the wrong reason.
    """
    importlib.reload(panel_app.wikiauth)
    config.SECRET_KEY_PATH = os.path.join(tmp, "secret.key")
    config.RUN_STATE_PATH = os.path.join(tmp, "run_state.json")
    config.MAINTAINERS_PATH = os.path.join(tmp, "maintainers.json")
    with open(config.SECRET_KEY_PATH, "w", encoding="utf-8") as f:
        f.write("test-cookie-key")
    with open(config.MAINTAINERS_PATH, "w", encoding="utf-8") as f:
        json.dump([{"user": u, "granted_by": owner, "at": ""} for u in granted], f)
    if owner is None:
        os.environ.pop("PANEL_OWNER", None)
    else:
        os.environ["PANEL_OWNER"] = owner
    importlib.reload(panel_app)
    client = panel_app.app.test_client()
    if signed_in_as:
        with client.session_transaction() as s:
            s["wiki_user"] = signed_in_as
            s["wiki_token"] = {"access_token": "t", "expires_at": 9e9}
    return client


def test_no_owner_disables_controls_rather_than_opening_them():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp)          # PANEL_OWNER unset
        assert client.post("/run").status_code == 503, "no owner must disable, not allow"
        assert client.post("/stop").status_code == 503


def test_signing_in_is_not_the_same_as_being_allowed():
    """Any Wikimedia account can complete OAuth; that must not run the job."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RandomPasserby")
        assert client.post("/run").status_code == 403,             "a non-maintainer was allowed to run the job"


def test_an_anonymous_visitor_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")
        assert client.post("/run").status_code == 403


def test_a_maintainer_passes_the_gate():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", granted=["Tausheef"], signed_in_as="RIFAT712")
        # Past the gate; the Jobs API is unreachable from here, so anything but
        # 403/503 means authorisation succeeded.
        assert client.post("/run").status_code not in (403, 503)


class FakeJobsApi:
    """The Jobs API after Stop deleted the job: GET 404s, calls are recorded."""
    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        from requests import HTTPError, Response
        r = Response()
        r.status_code = 404
        raise HTTPError(response=r)

    def post(self, url, **kw):
        self.calls.append(("post", url, kw.get("json")))
        return {}


def test_a_stopped_job_can_be_started_again_from_the_web():
    """Stop deletes the job, so Run now has nothing to restart. Start must
    re-create it from job.yaml and run it."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
        api = FakeJobsApi()
        panel_app.jobs_api = lambda: api
        page = client.get("/").get_data(as_text=True)
        assert "Start job" in page, "no way to start a stopped job from the web"
        assert client.post("/start").status_code == 302
        (verb, url, body), (verb2, url2, _) = api.calls
        assert url == f"/tool/{config.TOOL_NAME}/jobs/"
        assert body["name"] == config.JOB_NAME and body["cmd"] == "run-bot"
        assert body["schedule"] == "@hourly" and body["job_type"] == "scheduled"
        assert body["imagename"].endswith(":latest") and body["memory"] == "2Gi"
        assert url2.endswith(f"/jobs/{config.JOB_NAME}/restart"), "created but not run"


def test_usernames_match_the_way_mediawiki_matches_them():
    """MediaWiki treats Foo_Bar and Foo Bar as one account."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", granted=["Tausheef_Hassan"],
                               signed_in_as="Tausheef Hassan")
        assert client.post("/run").status_code not in (403, 503)


def test_read_only_views_stay_public():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")   # nobody signed in
        for route in ("/", "/uploads", "/queue", "/replacements",
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


def test_everything_written_on_the_page_is_editable():
    """If a category is in the wikitext, a person put it there and a person can
    take it off — including the one the bot writes at upload time."""
    assert wikitext.read_categories(PAGE) == ["Uploaded with pypan", "Some topic"], \
        wikitext.read_categories(PAGE)


def test_the_flag_is_the_only_category_the_box_will_not_touch():
    flagged = wikitext.tag_copyright(PAGE, "not-government")
    assert wikitext.CONCERNS_CATEGORY not in wikitext.read_categories(flagged), \
        "a flag must not come off as a side effect of tidying categories"

    kept = wikitext.write_categories(flagged, ["Zubaida Rahman"])
    assert wikitext.read_copyright_tag(kept), "rewriting categories dropped the flag"


def test_writing_categories_replaces_exactly_what_it_was_given():
    updated = wikitext.write_categories(
        PAGE, ["Zubaida Rahman", "  Category:Novo Theatre  ", "", "Zubaida Rahman"])

    assert wikitext.read_categories(updated) == ["Zubaida Rahman", "Novo Theatre"], \
        "expected de-duplication and the Category: prefix stripped"
    assert "Some topic" not in updated, "replaced topic category still present"
    # The panel posts back every chip it showed, so anything missing from the
    # list was taken off on purpose.
    assert "Uploaded with pypan" not in updated, \
        "a category left out of the list must not survive the write"


def test_category_names_cannot_smuggle_markup():
    try:
        wikitext.write_categories(PAGE, ["Fine", "Bad]]{{Delete}}"])
    except wikitext.Unparseable:
        return
    raise AssertionError("category markup injection was accepted")


def test_the_owner_grants_and_revokes_from_the_web():
    """The point of /admin: no bastion access needed to add a colleague."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
        assert panel_app.maintainers() == {"RIFAT712"}

        client.post("/admin", data={"user": "R1F4T"})
        assert "R1F4T" in panel_app.maintainers(), "granting from the web did nothing"

        client.post("/admin", data={"action": "revoke", "user": "R1F4T"})
        assert "R1F4T" not in panel_app.maintainers(), "revoking left them with access"


def test_only_the_owner_can_change_who_has_access():
    """A maintainer with a stolen session must not be able to promote anyone."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", granted=["Tausheef"],
                               signed_in_as="Tausheef")
        assert client.post("/admin", data={"user": "Attacker"}).status_code == 403
        assert "Attacker" not in panel_app.maintainers()


def test_the_owner_cannot_be_revoked_from_the_web():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
        client.post("/admin", data={"action": "revoke", "user": "RIFAT712"})
        assert "RIFAT712" in panel_app.maintainers(), "the owner locked themselves out"


def test_a_corrupt_maintainers_file_denies_rather_than_grants():
    with tempfile.TemporaryDirectory() as tmp:
        _panel_client(tmp, owner="RIFAT712", granted=["Tausheef"])
        with open(config.MAINTAINERS_PATH, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        assert panel_app.maintainers() == {"RIFAT712"},             "an unreadable list must fall back to the owner alone"


def test_every_page_carries_the_navbar():
    """Each area is a real page, not another card bolted onto the dashboard."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
        for route in ("/", "/uploads", "/queue", "/replacements"):
            body = client.get(route).get_data(as_text=True)
            assert 'class="tabs"' in body, f"{route} has no tabs"
            assert body.count('tab active') == 1, \
                f"{route} did not mark exactly one nav item as current"


def test_source_proxy_refuses_hosts_it_does_not_scrape():
    """The proxy sits inside Toolforge's network. Anything but the PID source
    hosts must be refused, not fetched."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
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
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
        for ok in ("https://pressinform.gov.bd/a.jpg",
                   "https://objectstorage.ap-dcc-gazipur-1.oraclecloud15.com/n/x/a.jpg",
                   "https://web.archive.org/web/2026/https://pressinform.gov.bd/a.jpg"):
            assert panel_app._allowed_source(ok), f"proxy would refuse {ok}"


def test_wayback_retry_needs_a_maintainer():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")   # nobody signed in
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



def test_mild_flag_files_the_page_and_refuses_a_second_one():
    flagged = wikitext.tag_copyright(PAGE, "not-government")
    assert "[[Category:PID files with copyright concerns]]" in flagged
    assert wikitext.read_copyright_tag(flagged), "flag not readable back"
    try:
        wikitext.tag_copyright(flagged, "derivative")
    except wikitext.Unparseable:
        return
    raise AssertionError("stacked a second copyright flag on one page")


def test_a_flag_survives_a_later_category_edit():
    """The flag is a category, and the category box rewrites categories."""
    flagged = wikitext.tag_copyright(PAGE, "unclear-source")
    assert "concerns" not in " ".join(wikitext.read_categories(flagged)),         "the flag showed up as a hand-editable topic category"
    edited = wikitext.write_categories(flagged, ["Zubaida Rahman"])
    assert wikitext.read_copyright_tag(edited),         "editing categories silently un-flagged the file"


def test_dated_template_lands_at_the_top_with_an_english_month():
    from datetime import date
    tagged = wikitext.tag_copyright(PAGE, "no-permission", today=date(2026, 9, 16))
    assert tagged.startswith(
        "{{No permission since|month=September|day=16|year=2026}}" + chr(10)), tagged[:90]
    assert "PD-BDGov-PID" in tagged, "the rest of the page was not preserved"


def test_severe_reasons_that_are_judgement_calls_demand_one():
    try:
        wikitext.tag_copyright(PAGE, "copyvio", note="   ")
    except wikitext.Unparseable:
        pass
    else:
        raise AssertionError("a copyright violation was filed with no reason")

    tagged = wikitext.tag_copyright(PAGE, "copyvio", note="Taken from AFP")
    assert tagged.startswith("{{Copyvio|1=Taken from AFP}}"), tagged[:60]


def test_a_flag_reason_cannot_smuggle_markup():
    for bad in ("{{Delete}}", "a|b", "[[File:x]]", "<ref>x</ref>"):
        try:
            wikitext.tag_copyright(PAGE, "wrong-license", note=bad)
        except wikitext.Unparseable:
            continue
        raise AssertionError(f"accepted markup in a flag reason: {bad!r}")


def test_unknown_reasons_are_refused():
    try:
        wikitext.tag_copyright(PAGE, "delete-it-all")
    except wikitext.Unparseable:
        return
    raise AssertionError("an unknown flag reason was accepted")



# ── 6. Commons workbench routes ───────────────────────────────────────────────

MARKED_PAGE = ("{{bn|1=x}}{{en|1=A meeting." + wikitext.MARKER + "}}" + chr(10)
               + "[[Category:Some topic]]" + chr(10))


def _workbench(tmp):
    """A signed-in panel client whose Commons calls are recorded, not made."""
    client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
    edits = []
    panel_app.wikiauth.fetch_wikitext = lambda title: (MARKED_PAGE, "2026-09-17T10:00:00Z")
    panel_app.wikiauth.edit_description = (
        lambda token, title, text, summary, basetimestamp=None:
            edits.append((title, text, summary, basetimestamp)))
    panel_app._queue_titles = lambda *a, **k: (("File:A.jpg", "File:B.jpg"), None, "", 2)
    with client.session_transaction() as s:
        s["wiki_user"] = "tester"
        s["wiki_token"] = {"access_token": "t", "expires_at": 9e9}
    return client, edits


def test_a_copyright_violation_needs_the_filename_typed_out():
    """The one flag an admin acts on within hours must not be a stray click."""
    with tempfile.TemporaryDirectory() as tmp:
        client, edits = _workbench(tmp)

        client.post("/file/File:A.jpg/tag",
                    data={"reason": "copyvio", "note": "From AFP", "confirm": ""})
        assert edits == [], "a copyvio was filed without confirmation"

        client.post("/file/File:A.jpg/tag",
                    data={"reason": "copyvio", "note": "From AFP", "confirm": "A.jpg"})
        assert len(edits) == 1, "typing the filename did not let the flag through"
        assert edits[0][1].startswith("{{Copyvio|1=From AFP}}"), edits[0][1][:60]
        assert "From AFP" in edits[0][2], "the reason is missing from the summary"


def test_flagging_needs_a_wikimedia_sign_in():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")   # nobody signed in
        panel_app._queue_titles = lambda *a, **k: ((), None, "", 0)
        assert client.post("/file/File:A.jpg/tag",
                           data={"reason": "not-government"}).status_code == 403


def test_only_the_marker_button_clears_the_marker():
    with tempfile.TemporaryDirectory() as tmp:
        client, edits = _workbench(tmp)
        form = {"english": "A meeting.", "categories": "Some topic", "caption": ""}

        client.post("/file/File:A.jpg", data=dict(form, action="save"))
        assert edits == [], "a plain save rewrote a page it had no changes for"

        client.post("/file/File:A.jpg",
                    data=dict(form, english="A cabinet meeting.", action="save"))
        assert wikitext.MARKER in edits[-1][1],             "editing the description silently dropped the marker"

        client.post("/file/File:A.jpg", data=dict(form, action="clear-next"))
        assert wikitext.MARKER not in edits[-1][1], "the marker button did not clear it"


def test_the_marker_button_lands_on_the_next_file():
    with tempfile.TemporaryDirectory() as tmp:
        client, _edits = _workbench(tmp)
        reply = client.post("/file/File:A.jpg",
                            data={"english": "A meeting.", "categories": "",
                                  "caption": "", "action": "clear-next",
                                  "next_title": "File:B.jpg"})
        assert "/file/File:B.jpg" in reply.headers["Location"], reply.headers["Location"]



def test_category_suggestions_come_from_commons_and_stay_quiet_when_short():
    """One keystroke must not become one Commons query."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
        asked = []

        def fake(prefix, bucket, limit=10):
            asked.append(prefix)
            return ("Dhaka", "Dhaka District") if len(prefix.strip()) >= 2 else ()

        panel_app.commons.suggest_categories = fake
        assert b"Dhaka District" in client.get("/categories/suggest?q=Dha").data
        assert b"No category" in client.get("/categories/suggest?q=D").data
        assert asked == ["Dha", "D"]



def test_oauth2_authorize_url_carries_what_mediawiki_needs():
    """1.0a against a 2.0 consumer is "Wrong OAuth version, E012", so pin the
    2.0 endpoint and its parameters — only live MediaWiki says otherwise."""
    from urllib.parse import parse_qs, urlparse
    from panel import wikiauth

    real = wikiauth.consumer
    wikiauth.consumer = lambda: ("client-id", "client-secret")
    try:
        url, state = wikiauth.start("https://tool.example/oauth/callback")
    finally:
        wikiauth.consumer = real

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.path.endswith("/rest.php/oauth2/authorize"), url
    # A commonswiki-only consumer is refused everywhere else, and meta issues a
    # token anyway — so the wiki is load-bearing, not cosmetic.
    assert parsed.netloc == "commons.wikimedia.org",         f"handshake must run on the wiki the consumer is for, got {parsed.netloc}"
    assert query["response_type"] == ["code"], query
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["https://tool.example/oauth/callback"]
    assert query["state"] == [state] and len(state) > 20, "state must be unguessable"


def test_a_callback_with_the_wrong_state_is_refused():
    """Without this check a crafted link could sign someone into another
    account. 2.0 has no request token, so state is the only guard."""
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")
        with client.session_transaction() as s:
            s["oauth_state"] = "the-real-one"
        reply = client.get("/oauth/callback?code=abc&state=forged")
        assert reply.status_code == 302
        with client.session_transaction() as s:
            assert "wiki_user" not in s, "a forged callback signed someone in"



def test_a_failed_oauth_step_reports_what_mediawiki_said():
    """The first version swallowed the reason and said only that a field was
    missing, which is the least useful thing it could have reported."""
    from panel import wikiauth

    class Reply:
        def __init__(self, payload, status=200):
            self._payload, self.status_code = payload, status

        def json(self):
            return self._payload

    for payload, status, expected in [
        ({"errorKey": "mwoauth-invalid-authorization",
          "messageTranslations": {"en": "The authorization headers in your "
                                        "request are not valid"},
          "httpCode": 403}, 403, "authorization headers"),
        ({"error": "access_denied",
          "error_description": "The resource owner or authorization server "
                               "denied the request.",
          "hint": 'Missing "Bearer" token'}, 401, "denied the request"),
    ]:
        try:
            wikiauth._rest_json(Reply(payload, status), "Reading your profile")
        except RuntimeError as e:
            assert expected in str(e), f"lost the reason: {e}"
            assert "Reading your profile" in str(e), f"lost the step: {e}"
            continue
        raise AssertionError(f"an HTTP {status} error was treated as success")

    ok = wikiauth._rest_json(Reply({"username": "RIFAT712"}), "Reading your profile")
    assert ok["username"] == "RIFAT712", "a good reply must pass straight through"



def test_username_suggestions_are_owner_only_and_come_from_commons():
    """Granting is owner-only, so the field feeding it should not be one more
    endpoint the world can ask about Commons accounts."""
    with tempfile.TemporaryDirectory() as tmp:
        panel_app.commons.suggest_users = lambda prefix, bucket, limit=10: (
            ("Tauseef 745", "Tauseef Ahmad") if len(prefix.strip()) >= 2 else ())

        stranger = _panel_client(tmp, owner="RIFAT712", signed_in_as="Nobody")
        assert stranger.get("/users/suggest?user=Tau").status_code == 403

        owner = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
        body = owner.get("/users/suggest?user=Tau").data
        assert b"Tauseef Ahmad" in body, body[:200]
        assert b"No account" in owner.get("/users/suggest?user=T").data



def test_the_bengali_is_readable_even_when_the_english_is_not():
    """It is what the translation gets checked against, so a page whose English
    half cannot be parsed must still show it.

    Compared NFC-normalised, and not as a nicety: Bengali য় exists both
    precomposed (U+09DF) and as য + nukta (U+09AF U+09BC). They render
    identically, so a literal written by hand can differ from OCR output in a
    way no one can see.
    """
    import unicodedata

    def same(a, b):
        return unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b)

    expected = "আজ ঢাকায় সভা।"
    assert same(wikitext.read_bengali(PAGE), expected), wikitext.read_bengali(PAGE)

    broken = "{{bn|1=" + expected + "}}{{en|1=unclosed"
    try:
        wikitext.read_english(broken)
        raise AssertionError("that English should not have parsed")
    except wikitext.Unparseable:
        pass
    assert same(wikitext.read_bengali(broken), expected), "Bengali lost with the English"

    assert wikitext.read_bengali("no templates here") == "", "absent Bengali is not an error"
    assert wikitext.read_bengali(None) == ""



def test_a_save_is_checked_against_the_revision_it_was_built_from():
    """The panel writes the whole page back, so an unguarded save silently
    reverts whoever edited in between."""
    with tempfile.TemporaryDirectory() as tmp:
        client, edits = _workbench(tmp)
        client.post("/file/File:A.jpg",
                    data={"english": "Rewritten.", "categories": "", "caption": "",
                          "action": "save", "base_revision": "2026-09-17T09:00:00Z"})
        assert edits, "nothing was saved"
        assert edits[-1][3] == "2026-09-17T09:00:00Z",             f"basetimestamp not sent: {edits[-1][3]!r}"


def test_an_edit_conflict_is_explained_rather_than_forced():
    from panel import wikiauth
    importlib.reload(wikiauth)   # the workbench fakes leak; test the real thing

    class Reply:
        status_code = 200

        def json(self):
            return {"error": {"code": "editconflict", "info": "Edit conflict."}}

    class Stub:
        def get(self, *a, **kw):
            return type("R", (), {
                "status_code": 200,
                "json": lambda self: {"query": {"tokens": {"csrftoken": "abc"}}}})()

        def post(self, *a, **kw):
            return Reply()

    real, wikiauth.session = wikiauth.session, Stub()
    try:
        wikiauth.edit_description({"access_token": "t", "expires_at": 9e9},
                                  "File:A.jpg", "text", "summary",
                                  basetimestamp="2026-09-17T09:00:00Z")
        raise AssertionError("an edit conflict was reported as success")
    except RuntimeError as e:
        assert "edited this page after you opened it" in str(e), str(e)
    finally:
        wikiauth.session = real



def test_bulk_categorise_adds_to_each_file_and_keeps_going_after_a_failure():
    """One bad file in a batch of 48 must not cost the other 47."""
    with tempfile.TemporaryDirectory() as tmp:
        client, edits = _workbench(tmp)

        def flaky(title):
            if title == "File:Bad.jpg":
                return None, None
            return MARKED_PAGE, "2026-09-17T10:00:00Z"

        panel_app.wikiauth.fetch_wikitext = flaky
        reply = client.post("/uploads/categorise", data={
            "titles": ["File:A.jpg", "File:Bad.jpg", "File:C.jpg"],
            "categories": "Meetings in Dhaka" + chr(10) + "Zubaida Rahman"})

        assert reply.status_code == 302
        saved = [e[0] for e in edits]
        assert saved == ["File:A.jpg", "File:C.jpg"], saved
        for _title, text, summary, base in edits:
            assert "[[Category:Meetings in Dhaka]]" in text, text[-200:]
            assert "[[Category:Zubaida Rahman]]" in text
            assert "Meetings in Dhaka" in summary, summary
            assert base == "2026-09-17T10:00:00Z", "bulk writes must be guarded too"


def test_bulk_categorise_refuses_the_ways_it_could_do_nothing_useful():
    with tempfile.TemporaryDirectory() as tmp:
        client, edits = _workbench(tmp)

        client.post("/uploads/categorise",
                    data={"titles": ["File:A.jpg"], "categories": "   "})
        client.post("/uploads/categorise", data={"categories": "Meetings in Dhaka"})
        assert edits == [], "wrote something with no categories or no files"

        # Already has it: adding again should be a no-op, not a null edit.
        panel_app.wikiauth.fetch_wikitext = lambda t: (
            MARKED_PAGE + "[[Category:Meetings in Dhaka]]" + chr(10),
            "2026-09-17T10:00:00Z")
        client.post("/uploads/categorise", data={"titles": ["File:A.jpg"],
                                                 "categories": "Meetings in Dhaka"})
        assert edits == [], "re-saved a file that already had the category"


def test_bulk_categorise_needs_a_commons_sign_in():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")
        assert client.post("/uploads/categorise", data={
            "titles": ["File:A.jpg"], "categories": "X"}).status_code == 403



def _http_error(status):
    from requests import HTTPError, Response
    r = Response()
    r.status_code = status
    return HTTPError(f"{status} Client Error", response=r)


def test_a_missing_job_is_not_a_dead_control_plane():
    """404 means the API answered "no such job" — reachable, nothing loaded.

    Reporting that as unreachable sends whoever is on call hunting a control
    plane that is fine, while the panel hides the one fact that matters: the
    job is gone and no run will ever fire.
    """
    for exc, reachable in ((_http_error(404), True),
                           (_http_error(500), False),
                           (OSError("connection refused"), False)):
        real = panel_app.jobs_api
        panel_app.jobs_api = lambda: (_ for _ in ()).throw(exc)
        try:
            job, got, _ = panel_app.fetch_job()
        finally:
            panel_app.jobs_api = real
        assert job is None
        assert got is reachable, f"{exc!r} reported reachable={got}"


def test_a_missing_job_reads_as_not_loaded():
    with tempfile.TemporaryDirectory() as tmp:
        _panel_client(tmp, owner="RIFAT712")
        real = panel_app.fetch_job
        panel_app.fetch_job = lambda: (None, True, "")
        try:
            state = panel_app.page_context()["state"]
        finally:
            panel_app.fetch_job = real
        assert state == "no-job", f"a missing job rendered as {state!r}"



def test_replacements_live_where_both_pods_can_see_them():
    """The panel and the bot are separate pods built from one image.

    Only $TOOL_DATA_DIR is shared between them. A table resolved against
    SCRIPT_DIR is written into the webservice container and read by nobody —
    the operator's name corrections vanish on the next pod restart and never
    reach a single run.
    """
    # Computed from the rule rather than read off config.CREDS_DIR, which
    # earlier tests in this file mutate to a tmpdir and never restore.
    expected = os.path.join(config.TOOL_DATA_DIR or config.SCRIPT_DIR,
                            "translation_replacements.tsv")
    assert config.REPLACEMENTS_PATH == expected, (
        "replacements must live in $TOOL_DATA_DIR; SCRIPT_DIR is per-container "
        "image storage that the job pod never sees")


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


def test_the_prompts_forbid_swapping_in_a_remembered_officeholder():
    """Gemini knows who *used* to hold a post and will 'correct' the Bengali.

    The Bangla caption is authoritative; nothing in the pipeline re-checks a
    name after the model returns it, so the instruction is the only guard.
    """
    both = config.TRANSLATION_PROMPT + config.TITLE_PROMPT
    assert "authoritative" in both.lower()
    assert "previous governments" not in config.TITLE_PROMPT, (
        "asking the model to reason about which government is current is what "
        "invites it to swap in the officeholder it remembers")


# ── Wikidata name resolution ──────────────────────────────────────────────────

def _resolve_with(names, text):
    """Run resolve_names against a fake replica; returns (text, matches, keys queried)."""
    from src import name_resolver
    asked = []
    real = name_resolver._lookup
    def fake(keys):
        asked.extend(keys)
        return [(bn, qid, en, 1) for bn, (en, qid) in names.items()]
    name_resolver._lookup = fake
    os.environ.setdefault("TOOL_REPLICA_USER", "test")
    try:
        return (*name_resolver.resolve_names(text), asked)
    finally:
        name_resolver._lookup = real


def test_name_with_a_case_ending_is_replaced_and_keeps_the_ending():
    out, matches, _ = _resolve_with(
        {"মুহাম্মদ ইউনূস": ("Muhammad Yunus", "Q1")},
        "প্রধান উপদেষ্টা মুহাম্মদ ইউনূসের সাথে বৈঠক")
    assert out == "প্রধান উপদেষ্টা Muhammad Yunusের সাথে বৈঠক", out
    assert matches == [("মুহাম্মদ ইউনূস", "Muhammad Yunus", "Q1")]


def test_the_longest_name_wins_an_overlap():
    out, _, _ = _resolve_with(
        {"আব্দুল হামিদ": ("Abdul Hamid", "Q2"),
         "মোহাম্মদ আব্দুল হামিদ": ("Mohammad Abdul Hamid", "Q3")},
        "রাষ্ট্রপতি মোহাম্মদ আব্দুল হামিদ আজ")
    assert out == "রাষ্ট্রপতি Mohammad Abdul Hamid আজ", out


def test_single_words_and_windows_across_punctuation_are_never_looked_up():
    _, _, asked = _resolve_with({}, "হাসিনা, ইউনূস ঢাকায়")
    assert all(" " in k for k in asked), asked
    assert "হাসিনা ইউনূস" not in asked, "matched across a comma"


def test_ambiguous_label_goes_to_the_most_sitelinked_person():
    from src.name_resolver import _choose
    rows = [("শেখ হাসিনা", "Q10", "Sheikh Hasina", 150),
            ("শেখ হাসিনা", "Q11", "Sheikh Hasina (singer)", 2)]
    assert _choose(rows) == {"শেখ হাসিনা": ("Sheikh Hasina", "Q10")}


def test_replica_failure_passes_the_text_through():
    from src import name_resolver
    real = name_resolver._lookup
    def boom(keys):
        raise RuntimeError("replica down")
    name_resolver._lookup = boom
    os.environ.setdefault("TOOL_REPLICA_USER", "test")
    try:
        text = "মুহাম্মদ ইউনূস ঢাকায়"
        assert name_resolver.resolve_names(text) == (text, [])
    finally:
        name_resolver._lookup = real


def test_cropper_is_unchanged():
    """The cropper is tuned over thousands of PID images. If you changed it on
    purpose, update the hash; if not, revert src/cropper.py."""
    import hashlib
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "src", "cropper.py"), "rb") as f:
        digest = hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()
    assert digest == "6c8f9931497da40dd97981e1dfb4b47432ab72ce492ae4fd1350ef6c23fac9e9", (
        "src/cropper.py changed")


# ── Panel redesign ────────────────────────────────────────────────────────────

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
        assert "can&#39;t reach Toolforge" in page or "can't reach Toolforge" in page, page[:800]
        assert "certificate expired" in page
        assert "Jobs API" not in page and "Stderr" not in page


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


def test_bengali_names_are_highlighted_safely():
    out = str(panel_app.highlight_names("<b>মুহাম্মদ ইউনূসের সাথে</b>",
                                        [("মুহাম্মদ ইউনূস", "Muhammad Yunus", "Q1")]))
    assert "<b>" not in out and "&lt;b&gt;" in out, "caption HTML was injected"
    assert '<mark class="wd" title="Muhammad Yunus (Q1)">মুহাম্মদ ইউনূস</mark>ের' in out, out
    assert str(panel_app.highlight_names("কিছু না", [])) == "কিছু না"


def _file_page_client(tmp, page):
    """A client signed in as the owner, with every Commons call faked.
    Returns (client, edits, restore)."""
    client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
    edits = []
    panel_app.wikiauth.fetch_wikitext = lambda title: (page, "2026-09-17T10:00:00Z")
    panel_app.wikiauth.edit_description = (
        lambda token, title, text, summary, basetimestamp=None:
            edits.append((title, text, summary)))
    panel_app._queue_titles = lambda *a, **k: (("File:X.jpg",), None, "", 1)
    real = {k: getattr(panel_app.commons, k) for k in ("caption", "categories_of")}
    panel_app.commons.caption = lambda *a, **k: ""
    panel_app.commons.categories_of = lambda *a, **k: []
    def restore():
        for k, v in real.items():
            setattr(panel_app.commons, k, v)
    return client, edits, restore


def test_remembered_name_fixes_reach_the_table_once():
    from panel import corrections
    with tempfile.TemporaryDirectory() as tmp:
        page = "{{bn|1=ক খ}}{{en|1=Ka Kha" + wikitext.MARKER + "}}\n"
        client, _, restore = _file_page_client(tmp, page)
        real = config.REPLACEMENTS_PATH
        config.REPLACEMENTS_PATH = os.path.join(tmp, "t.tsv")
        try:
            for _ in range(2):
                client.post("/file/File:X.jpg", data={
                    "english": "Ka Kha fixed", "categories": "", "action": "save",
                    "name_fix": ["ক খ|||Ka Kha Fixed"], "remember_names": "on"})
            rows = corrections.rows(config.REPLACEMENTS_PATH)
        finally:
            config.REPLACEMENTS_PATH = real
            restore()
        assert [(r["bn"], r["en"], r["user"]) for r in rows] == \
            [("ক খ", "Ka Kha Fixed", "RIFAT712")], rows


def test_file_page_marks_wikidata_names_and_survives_the_replica_being_down():
    from src import name_resolver
    with tempfile.TemporaryDirectory() as tmp:
        page = "{{bn|1=মুহাম্মদ ইউনূসের সাথে}}{{en|1=With Muhammad Yunus}}\n"
        client, _, restore = _file_page_client(tmp, page)
        real = name_resolver._lookup
        os.environ.setdefault("TOOL_REPLICA_USER", "test")
        try:
            name_resolver._lookup = lambda keys: [("মুহাম্মদ ইউনূস", "Q1", "Muhammad Yunus", 9)]
            body = client.get("/file/File:X.jpg").get_data(as_text=True)
            assert '<mark class="wd"' in body and "wikidata.org/wiki/Q1" in body, body[:400]
            assert "1 name confirmed on Wikidata" in body

            name_resolver._lookup = lambda keys: (_ for _ in ()).throw(RuntimeError("down"))
            r = client.get("/file/File:X.jpg")
            assert r.status_code == 200 and "মুহাম্মদ ইউনূসের" in r.get_data(as_text=True)
        finally:
            name_resolver._lookup = real
            restore()


def test_overview_links_recent_uploads_to_their_file_page_and_hides_the_log():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")
        real = panel_app.commons.recent_uploads
        panel_app.commons.recent_uploads = lambda limit=24: [
            {"filename": "A b.jpg", "unique_id": "u1"}][:limit]
        try:
            strip = client.get("/partials/gallery").get_data(as_text=True)
            page = client.get("/").get_data(as_text=True)
        finally:
            panel_app.commons.recent_uploads = real
        assert "/file/File:A%20b.jpg" in strip or "/file/File:A b.jpg" in strip, strip
        assert "<details" in page and "Technical log" in page
        assert "Stderr" not in page


def test_old_upload_links_land_on_the_file_page():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")
        real = panel_app.commons.find_upload
        panel_app.commons.find_upload = lambda uid: {"filename": "A.jpg", "unique_id": uid}
        try:
            r = client.get("/upload/u1")
        finally:
            panel_app.commons.find_upload = real
        assert r.status_code == 302 and "/file/File:A.jpg" in r.headers["Location"], r.headers


# ── Final review fixes ────────────────────────────────────────────────────────

def test_pausing_mid_run_offers_resume_and_a_second_pause_keeps_the_schedule():
    running_paused = panel_app.status_view("running", True, True, {"status": "running"})
    assert "resume" in running_paused["actions"] and "pause" not in running_paused["actions"]
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712", signed_in_as="RIFAT712")
        patched = []
        class Api:
            def patch(self, url, **kw):
                patched.append(kw.get("json"))
        panel_app.jobs_api = lambda: Api()
        panel_app.fetch_job = lambda: ({"schedule": panel_app.NEVER_FIRES}, True, "")
        with client.session_transaction() as s:
            s["schedule_before_pause"] = "@hourly"
        client.post("/pause")
        with client.session_transaction() as s:
            assert s.get("schedule_before_pause") == "@hourly", "real schedule overwritten"
        assert patched == [], "paused an already-paused job"


def test_editing_a_correction_cannot_duplicate_another_rows_bengali():
    from panel import corrections
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "t.tsv")
        corrections.add(p, "ক খ", "A", "u")
        corrections.add(p, "গ ঘ", "B", "u")
        assert corrections.update(p, "গ ঘ", "ক খ", "B", "u") is False
        assert [r["bn"] for r in corrections.rows(p)] == ["ক খ", "গ ঘ"]


def test_the_file_page_asks_the_replica_with_a_short_timeout():
    from src import name_resolver
    seen = {}
    real = name_resolver.pymysql.connect
    def connect(**kw):
        seen.update(kw)
        raise RuntimeError("down")
    name_resolver.pymysql.connect = connect
    name_resolver._local.conn = None
    os.environ.setdefault("TOOL_REPLICA_USER", "test")
    os.environ.setdefault("TOOL_REPLICA_PASSWORD", "test")
    try:
        name_resolver.resolve_names("মুহাম্মদ ইউনূস ঢাকায়", timeout=3)
    finally:
        name_resolver.pymysql.connect = real
    assert seen["connect_timeout"] <= 3 and seen["read_timeout"] <= 3, seen
    with tempfile.TemporaryDirectory() as tmp:
        page = "{{bn|1=মুহাম্মদ ইউনূস}}{{en|1=x}}\n"
        client, _, restore = _file_page_client(tmp, page)
        asked = {}
        panel_app.resolve_names = lambda text, **kw: (asked.update(kw) or (text, []))
        try:
            client.get("/file/File:X.jpg")
        finally:
            restore()
        assert asked.get("timeout", 99) <= 3, asked


def test_a_name_appearing_twice_is_counted_and_marked_once_each():
    m = ("মুহাম্মদ ইউনূস", "Muhammad Yunus", "Q1")
    out = str(panel_app.highlight_names("মুহাম্মদ ইউনূস এবং মুহাম্মদ ইউনূস", [m, m]))
    assert out.count("<mark") == 2 and "<mark class=\"wd\" title=\"Muhammad Yunus (Q1)\"><mark" not in out, out
    tricky = [("ক খ", "ক খ গ", "Q2"), ("ক খ গ", "Longer", "Q3")]
    out = str(panel_app.highlight_names("ক খ গ ঘ", tricky))
    assert out.count("<mark") == 1 and out.count("</mark>") == 1, out
    from src import name_resolver
    with tempfile.TemporaryDirectory() as tmp:
        page = "{{bn|1=মুহাম্মদ ইউনূস এবং মুহাম্মদ ইউনূস}}{{en|1=x}}\n"
        client, _, restore = _file_page_client(tmp, page)
        real = name_resolver._lookup
        name_resolver._lookup = lambda keys: [("মুহাম্মদ ইউনূস", "Q1", "Muhammad Yunus", 9)]
        os.environ.setdefault("TOOL_REPLICA_USER", "test")
        try:
            body = client.get("/file/File:X.jpg").get_data(as_text=True)
        finally:
            name_resolver._lookup = real
            restore()
        assert "1 name confirmed" in body, "a repeated name was counted twice"


def test_session_cookie_is_same_site():
    assert panel_app.app.config.get("SESSION_COOKIE_SAMESITE") == "Lax"


def test_corrections_are_written_atomically():
    """The bot reads this file over NFS; a half-written table means wrong names."""
    from panel import corrections
    calls = []
    real = corrections.os.replace
    corrections.os.replace = lambda a, b: (calls.append((a, b)), real(a, b))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "t.tsv")
            corrections.add(p, "ক খ", "A", "u")
            assert corrections.rows(p)[0]["en"] == "A"
    finally:
        corrections.os.replace = real
    assert calls and calls[0][1] == p, calls


def test_a_name_fix_that_cannot_be_saved_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        page = "{{bn|1=ক খ}}{{en|1=Ka Kha" + wikitext.MARKER + "}}\n"
        client, _, restore = _file_page_client(tmp, page)
        real = config.REPLACEMENTS_PATH
        config.REPLACEMENTS_PATH = os.path.join(tmp, "t.tsv")
        try:
            r = client.post("/file/File:X.jpg", data={
                "english": "Ka Kha", "categories": "", "action": "save",
                "name_fix": ["ক\nখ|||Ka Kha"], "remember_names": "on"},
                follow_redirects=True)
        finally:
            config.REPLACEMENTS_PATH = real
            restore()
        assert "not remembered" in r.get_data(as_text=True)


# ── Search ────────────────────────────────────────────────────────────────────

def test_search_text_is_combined_with_each_lists_filter():
    sq = panel_app.search_query
    assert sq("all", "", "Yunus") == 'hastemplate:"PD-BDGov-PID" Yunus'
    assert sq("all", "", "") == 'hastemplate:"PD-BDGov-PID"'
    assert sq("review", "PID-BD images from September 2026", "Yunus") == \
        'hastemplate:"Auto-translated PID English description" ' \
        'incategory:"PID-BD images from September 2026" Yunus'
    assert sq("uncategorised", "", "Yunus") == \
        'incategory:"Press Information Department images without category" Yunus'
    assert sq("month", "PID-BD images from May 2025", "  Japan   ambassador ") == \
        'incategory:"PID-BD images from May 2025" Japan ambassador'
    assert sq("uncategorised", "", "") is None, "empty search should use the category listing"


def test_an_unclosed_quote_cannot_swallow_the_filter():
    assert panel_app.search_query("all", "", 'Japan "ambassador') == \
        'hastemplate:"PD-BDGov-PID" Japan ambassador'
    assert panel_app.search_query("all", "", '"Japan ambassador"') == \
        'hastemplate:"PD-BDGov-PID" "Japan ambassador"'


def test_searching_uploads_uses_commons_search_and_keeps_the_query():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp, owner="RIFAT712")
        asked = []
        real = {k: getattr(panel_app.commons, k) for k in ("search_page", "category_size")}
        panel_app.commons.search_page = lambda query, offset, bucket, limit=48: (
            asked.append(query) or (("File:Yunus meets.jpg",), 1))
        panel_app.commons.category_size = lambda *a, **k: 0
        try:
            body = client.get("/uploads?queue=all&q=Yunus").get_data(as_text=True)
        finally:
            for k, v in real.items():
                setattr(panel_app.commons, k, v)
        assert asked == ['hastemplate:"PD-BDGov-PID" Yunus'], asked
        assert 'value="Yunus"' in body, "search box lost the query"
        assert "q=Yunus" in body, "file links don't carry the search"


def test_the_file_page_walks_the_search_results():
    with tempfile.TemporaryDirectory() as tmp:
        client, _, restore = _file_page_client(tmp, "{{bn|1=x}}{{en|1=y}}\n")
        panel_app.wikiauth.consumer = lambda: object()   # sign-in set up, so the form renders
        seen = []
        panel_app._queue_titles = lambda *a, **k: (seen.append((a, k)) or
                                                   (("File:X.jpg", "File:Y.jpg"), None, "", 2))
        try:
            body = client.get("/file/File:X.jpg?queue=all&q=Yunus").get_data(as_text=True)
        finally:
            restore()
        assert any("Yunus" in list(a) + list(k.values()) for a, k in seen), seen
        assert "q=Yunus" in body, "next/previous links drop the search"
        assert 'name="q" value="Yunus"' in body, "saving would drop the search"


# ── Stats ─────────────────────────────────────────────────────────────────────

def test_failures_are_sorted_into_causes():
    from src.stats import failure_cause
    ok = {"upload_status": "Success"}
    assert failure_cause(ok) is None
    assert failure_cause({"upload_status": "Skipped (checksum duplicate)"}) is None
    assert failure_cause({"ocr_status": "No URL"}) is None
    assert failure_cause({"ocr_status": "404 error - no snapshot"}) == "download"
    assert failure_cause({"ocr_status": "Download failed: timeout"}) == "download"
    assert failure_cause({"ocr_status": "OCR failed"}) == "ocr"
    assert failure_cause({"ocr_status": "No text detected"}) == "ocr"
    assert failure_cause({"ocr_status": "Success", "translation_status": "Error: all models failed"}) == "translation"
    assert failure_cause({"ocr_status": "Success", "translation_status": "Success",
                          "filename_status": "Error: x"}) == "title"
    assert failure_cause({"ocr_status": "Success", "translation_status": "Success",
                          "filename_status": "Success", "upload_status": "Failed: abuse filter"}) == "upload"
    assert failure_cause({"ocr_status": "Success", "upload_status": "Exception: boom"}) == "other"


def test_runs_add_up_per_day_and_backlog_is_the_latest():
    from src import stats
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "s.json")
        with open(p, "w") as f:
            f.write("not json")
        stats.record_run(3, 1, {"ocr": 1}, {"free": 5, "paid": 1, "free_limit": 2},
                         {"review": 100, "uncategorised": 50}, path=p, today="2026-09-26")
        stats.record_run(2, 0, {"ocr": 1, "upload": 1}, {"free": 4, "paid": 0, "free_limit": 0},
                         {"review": 98, "uncategorised": 51}, path=p, today="2026-09-26")
        stats.record_run(1, 0, {}, {"free": 1, "paid": 0, "free_limit": 0}, None,
                         path=p, today="2026-09-26")
        day = stats.load(p)["2026-09-26"]
        assert day["runs"] == 3 and day["uploaded"] == 6 and day["duplicates"] == 1
        assert day["failed"]["ocr"] == 2 and day["failed"]["upload"] == 1 and day["failed"]["title"] == 0
        assert day["gemini"] == {"free": 10, "paid": 1, "free_limit": 2}
        assert day["backlog"] == {"review": 98, "uncategorised": 51}, "a failed fetch erased the backlog"
        assert not os.path.exists(p + ".tmp")


def test_gemini_usage_counts_free_paid_and_free_limit_hits():
    translator.sleep = lambda s: None
    translator.reset_usage()

    class Resp:
        text = "ok"

    class Models:
        def __init__(self, fail_first_with=None):
            self.fail = fail_first_with
        def generate_content(self, **kw):
            if self.fail:
                e, self.fail = self.fail, None
                raise e
            return Resp()

    class Client:
        def __init__(self, fail=None):
            self.models = Models(fail)

    translator._gemini_text(Client(RuntimeError("429 RESOURCE_EXHAUSTED")), Client(), "p", 10, 1,
                            lambda t: t)
    translator._gemini_text(Client(RuntimeError("400 bad request")), Client(), "p", 10, 2,
                            lambda t: t)
    assert translator.usage() == {"free": 1, "paid": 1, "free_limit": 1}, translator.usage()
    translator.reset_usage()
    assert translator.usage() == {"free": 0, "paid": 0, "free_limit": 0}


def test_stats_buckets_days_and_compares_with_the_period_before():
    from datetime import date
    from panel import stats_view
    uploads = [{"date": "2026-09-24", "filename": "e.jpg"},
               {"date": "2026-09-25 10:00", "filename": "a.jpg"},
               {"date": "2026-09-26", "filename": "b.jpg"},     # today: not compared
               {"date": "2026-09-26", "filename": ""},          # duplicate registration
               {"date": "garbage", "filename": "c.jpg"},        # malformed: skipped
               {"date": "2026-09-18", "filename": "d.jpg"}]     # previous 7-day period
    v = stats_view.build({}, uploads, "7d", date(2026, 9, 26))
    assert v["labels"][-1] == "2026-09-26" and len(v["labels"]) == 7
    assert v["uploads"]["values"][-2:] == [1, 1]
    assert "3 uploaded in the last 7 days" in v["uploads"]["summary"]
    assert "100% more" in v["uploads"]["summary"], v["uploads"]["summary"]
    assert v["failures"]["since"] is None and "No data yet" in v["failures"]["summary"]


def test_stats_monthly_range_crosses_the_year_and_reads_the_daily_file():
    from datetime import date
    from panel import stats_view
    daily = {"2026-01-03": {"runs": 1, "uploaded": 2, "duplicates": 0,
                            "failed": {"ocr": 2, "translation": 1},
                            "gemini": {"free": 9, "paid": 1, "free_limit": 1},
                            "backlog": {"review": 500, "uncategorised": 40}},
             "2025-12-30": {"runs": 1},                                  # missing keys
             "not-a-date": {"runs": 1}}
    v = stats_view.build(daily, [], "12m", date(2026, 1, 15))
    assert v["labels"][0] == "2025-02" and v["labels"][-1] == "2026-01" and len(v["labels"]) == 12
    assert v["failures"]["series"]["ocr"][-1] == 2 and v["failures"]["series"]["upload"][-1] == 0
    assert v["gemini"]["paid"][-1] == 1 and v["gemini"]["free"][-1] == 9
    assert v["backlog"]["review"][-1] == 500 and v["backlog"]["review"][0] is None
    assert v["failures"]["since"] == "2025-12-30"
    assert "10%" in v["gemini"]["summary"], v["gemini"]["summary"]


def test_stats_page_renders_with_its_data_and_no_external_scripts():
    with tempfile.TemporaryDirectory() as tmp:
        client = _panel_client(tmp)
        real_path, config.STATS_PATH = config.STATS_PATH, os.path.join(tmp, "stats_daily.json")
        real = panel_app.commons._uploads_raw
        panel_app.commons._uploads_raw = lambda year, bucket: (
            (("date", "2026-09-26"), ("filename", "a.jpg")),)
        try:
            body = client.get("/stats?range=7d").get_data(as_text=True)
        finally:
            panel_app.commons._uploads_raw = real
            config.STATS_PATH = real_path
        assert 'id="statsdata"' in body and 'chartjs/chart.umd.min.js' in body
        for heading in ("Uploads", "Failures", "Review backlog", "Gemini"):
            assert heading in body, heading
        assert "No data yet" in body
        assert 'src="http' not in body, "external script"
        assert ">Stats</a>" in body


BAD_DAY = {"runs": "x", "failed": ["x"], "gemini": "free",
           "backlog": {"review": "lots", "uncategorised": [1]}}


def test_a_malformed_day_entry_neither_breaks_the_page_nor_stops_recording():
    from datetime import date
    from panel import stats_view
    from src import stats
    daily = {"2026-09-25": BAD_DAY,
             "2026-09-24": {"failed": {"ocr": "abc"}, "gemini": {"free": [1]}}}
    v = stats_view.build(daily, [], "30d", date(2026, 9, 26))     # must not raise
    assert v["backlog"]["review"][-2] is None
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "s.json")
        with open(p, "w") as f:
            json.dump({"2026-09-26": BAD_DAY}, f)
        stats.record_run(2, 0, {"ocr": 1}, {"free": 3}, None, path=p, today="2026-09-26")
        day = stats.load(p)["2026-09-26"]
        assert day["uploaded"] == 2 and day["failed"]["ocr"] == 1 and day["gemini"]["free"] == 3, day


def test_the_comparison_leaves_out_the_unfinished_day():
    """Today is still running; comparing it as a full day always reads 'fewer'."""
    from datetime import date
    from panel import stats_view
    today = date(2026, 9, 26)
    uploads = [{"date": (today.fromordinal(today.toordinal() - k)).isoformat(), "filename": "x"}
               for k in range(1, 14)]            # one a day on every complete day, none yet today
    v = stats_view.build({}, uploads, "7d", today)
    assert "the same as" in v["uploads"]["summary"], v["uploads"]["summary"]


# ── Image processing ──────────────────────────────────────────────────────────

def test_a_photo_recovered_from_the_wayback_machine_is_processed():
    """PID deleted the original; the archived copy must still be cropped and read."""
    import numpy as np
    from src.image_processor import ImageProcessor
    ip = ImageProcessor()
    img = np.full((400, 300, 3), 200, dtype=np.uint8)
    ip.download_image = lambda url: (img, "jpg", None, b"raw", "Retrieved from Wayback Machine")
    ip.perform_ocr = lambda section: "আজ ঢাকায় সভা"
    result = ip.process_image(1, "https://pressinform.gov.bd/x.jpg")
    assert result["image"] is not None, result["status"]
    assert result["ocr_text"] == "আজ ঢাকায় সভা"
    assert result["status"].startswith("Success"), result["status"]
    assert "archive" in result["status"].lower(), "the archive origin should stay visible"


def test_a_failed_download_still_stops_processing():
    from src.image_processor import ImageProcessor
    ip = ImageProcessor()
    ip.download_image = lambda url: (None, None, None, None, "404 error - no snapshot")
    result = ip.process_image(1, "https://pressinform.gov.bd/x.jpg")
    assert result["image"] is None and result["status"] == "404 error - no snapshot"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} checks passed")
