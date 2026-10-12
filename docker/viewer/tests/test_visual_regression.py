"""
Visual regression tests for the two grading-panel pages
(watermark.html -- Chrome flow, grading-panel.html -- Weasis flow): catch
any change to what a student actually sees, across every meaningfully
distinct panel state (learning/assessment/test forms, their feedback, and
the final results screen).

Uses pytest-playwright-visual-snapshot (pixelmatch under the hood, the
same engine Playwright's own JS visual comparisons use) -- not a hand-
rolled diff, and not the commercial SaaS tool a recent internal reference
article on UI/UX testing pointed at; this is the actively-maintained
open-source equivalent that fits this repo's existing Python+Playwright
stack.

Determinism, the hard part of screenshotting a page with a live-updating
watermark (STUDENT_ID | SESSION_ID | timestamp, re-rendered every second
via setInterval -- see watermark.html/grading-panel.html's own comments):
Playwright's Clock API (`page.clock.set_fixed_time`) freezes what
Date.now() returns *before* the page's own script ever runs, so every
tick of that setInterval renders the exact same string -- no masking
needed, and the watermark itself becomes part of the deterministic
baseline instead of a problem to work around.

watermark.html's own screenshot is deliberately scoped to the
`#grading-panel` element only, not the full page: the DICOM viewer
iframe next to it renders Orthanc's own Explorer2 UI, whose content
depends on Orthanc's actual current data (which sample studies happen to
be loaded) -- real, inherent instability this repo doesn't control,
unrelated to whether *our* UI changed. grading-panel.html has no such
iframe (Weasis shows the images in its own separate window instead, see
README's "Two viewer flows" section), so its snapshot covers the full
page -- the panel_locator fixture below returns the right target for
either page transparently, so tests never need to know the difference.

student_id per test is derived from the test's own (parametrized) node
name -- unique per (test method, flow_page) so watermark.html and
grading-panel.html's own runs of the *same* test method never share one
student_id and silently interfere with each other's progress, but
deliberately fixed/stable across separate suite runs, NOT run-varying
(no timestamp/random suffix): student_id flows straight into the visible
watermark text (STUDENT_ID | SESSION_ID | timestamp), so anything that
changes between runs there would defeat the whole point of a stable
baseline. This is only safe because minting a token does NOT reset a
student's progress, but scripts/test-visual-regression.sh always tears
down the grading-db volume before running -- every real invocation starts
every student genuinely fresh. Found the hard way once, briefly, by
mis-designing this the other way around (a run-unique ID "fixed" a
collision from manual iterative debugging against a live stack, but broke
determinism for everyone else) -- see git history if curious.

Run via scripts/test-visual-regression.sh (self-contained: provisions the
stack, mints tokens, runs this inside the official Playwright docker
image, tears down on exit) -- not directly with a bare `pytest`, unless
the stack is already up (see that script's own header comment).
"""

import datetime
import json
import os
import re
import urllib.error
import urllib.request

import pytest

GRADING_API_URL = os.environ.get("GRADING_API_URL", "http://localhost:8080/api")
COORDINATOR_KEY = os.environ["GRADING_COORDINATOR_KEY"]
VIEWPORT = {"width": 1280, "height": 800}
# Arbitrary, fixed -- the actual value never matters, only that it's the
# same on every run so the watermark text it feeds into is deterministic.
FROZEN_TIME = datetime.datetime(2026, 1, 1, 12, 0, 0)


def _mint_token(student_id: str, session_id: str) -> str:
    req = urllib.request.Request(
        f"{GRADING_API_URL.rstrip('/')}/session",
        data=json.dumps({"student_id": student_id, "session_id": session_id}).encode(),
        headers={"Content-Type": "application/json", "X-Coordinator-Key": COORDINATOR_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())["token"]
    except urllib.error.HTTPError as e:
        pytest.fail(f"could not mint a grading-api token: {e.code} {e.read().decode()}")


def _open(page, base_url: str, path: str, token: str, student_id: str, session_id: str):
    page.clock.set_fixed_time(FROZEN_TIME)
    page.set_viewport_size(VIEWPORT)
    page.goto(f"{base_url}/{path}?student_id={student_id}&session_id={session_id}#token={token}")
    page.wait_for_selector("#grading-panel button", timeout=15000)
    # Issue #168 flake: the selector above lands as soon as the DOM is
    # built, which on CI runners can still be BEFORE web fonts have
    # finished loading -- metrics swap under the first snapshot and the
    # diff shows pure text re-layout. Wait for the font set to settle
    # (document.fonts.ready), then two animation frames so any post-font
    # reflow has been painted before the capture.
    page.evaluate("() => document.fonts.ready.then(() => true)")
    page.evaluate("new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))")


def _submit_learning(page):
    page.fill("#text-input", "Przykładowa ocena studenta -- tekst wolny, nieoceniany.")
    page.click("#submit-btn")
    page.wait_for_selector("#next-btn", timeout=15000)


def _submit_structured(page):
    page.select_option("#category-input", "2")
    page.check("input[name='modifier-s'][value='no']")
    page.click("#submit-btn")
    page.wait_for_timeout(500)  # feedback render is synchronous, but give layout a moment to settle


def _advance_to_next_case(page):
    page.click("#next-btn")
    page.wait_for_selector("#grading-panel button", timeout=15000)


@pytest.fixture(params=["watermark.html", "grading-panel.html"])
def flow_page(request):
    """Both pages share the same panel-rendering JS and the same 3-stage
    flow -- parameterizing over both means every snapshot below runs for
    each, with no duplicated flow-driving code."""
    return request.param


@pytest.fixture
def student_id(request, flow_page):
    """A student_id unique to (this test method, this flow_page) --
    fixed/stable across separate runs, deliberately NOT run-unique: it
    flows straight into the visible watermark text
    (STUDENT_ID | SESSION_ID | timestamp), so anything that varies run to
    run there (a timestamp, a random suffix) defeats the whole point of a
    stable baseline. Safe to keep fixed because
    scripts/test-visual-regression.sh always tears down the grading-db
    volume before running -- every real invocation starts from a
    genuinely empty database. Only a problem for manual iterative
    debugging against an already-running stack (bypassing that script);
    tear down with `docker compose down -v` between iterations instead of
    reaching for a run-varying ID here."""
    safe_name = re.sub(r"[^A-Za-z0-9]", "_", request.node.originalname or request.node.name)
    safe_flow = flow_page.replace(".html", "").replace("-", "_")
    return f"VR_{safe_name}_{safe_flow}"[:128]


@pytest.fixture
def token(student_id):
    return _mint_token(student_id, "vr")


@pytest.fixture
def panel_locator(page, flow_page):
    """watermark.html: scope to #grading-panel (see module docstring for
    why the iframe next to it is excluded). grading-panel.html: the panel
    *is* the whole page, but returning the same kind of screenshot target
    either way keeps every test below identical regardless of which flow
    it's parameterized against."""
    return page.locator("#grading-panel") if flow_page == "watermark.html" else page


class TestGradingPanelStates:
    """One test per meaningfully distinct thing a student can see. Each
    gets its own student_id (see the student_id fixture) so they can run
    in any order, or repeatedly against the same stack, without one
    run's progress bleeding into another's snapshot."""

    def test_learning_stage_form(self, page, flow_page, student_id, token, panel_locator, assert_snapshot, base_url):
        _open(page, base_url, flow_page, token, student_id, "vr")
        assert_snapshot(panel_locator)

    def test_learning_stage_feedback(
        self, page, flow_page, student_id, token, panel_locator, assert_snapshot, base_url
    ):
        _open(page, base_url, flow_page, token, student_id, "vr")
        _submit_learning(page)
        assert_snapshot(panel_locator)

    def test_assessment_stage_form(
        self, page, flow_page, student_id, token, panel_locator, assert_snapshot, base_url
    ):
        _open(page, base_url, flow_page, token, student_id, "vr")
        _submit_learning(page)
        _advance_to_next_case(page)
        assert_snapshot(panel_locator)

    def test_assessment_stage_feedback(
        self, page, flow_page, student_id, token, panel_locator, assert_snapshot, base_url
    ):
        _open(page, base_url, flow_page, token, student_id, "vr")
        _submit_learning(page)
        _advance_to_next_case(page)
        _submit_structured(page)
        assert_snapshot(panel_locator)

    def test_test_stage_form(self, page, flow_page, student_id, token, panel_locator, assert_snapshot, base_url):
        _open(page, base_url, flow_page, token, student_id, "vr")
        _submit_learning(page)
        _advance_to_next_case(page)
        _submit_structured(page)
        _advance_to_next_case(page)
        assert_snapshot(panel_locator)

    def test_test_stage_feedback(self, page, flow_page, student_id, token, panel_locator, assert_snapshot, base_url):
        """The test stage's own defining feature: no reveal at all (see
        main.py's module docstring) -- "Odpowiedź zapisana." only. Worth
        its own snapshot specifically because a future change accidentally
        *adding* a reveal here would be a real regression (issue #27's own
        cheating-vector concern), not just a cosmetic one."""
        _open(page, base_url, flow_page, token, student_id, "vr")
        _submit_learning(page)
        _advance_to_next_case(page)
        _submit_structured(page)
        _advance_to_next_case(page)
        _submit_structured(page)
        assert_snapshot(panel_locator)

    def test_results_screen(self, page, flow_page, student_id, token, panel_locator, assert_snapshot, base_url):
        _open(page, base_url, flow_page, token, student_id, "vr")
        _submit_learning(page)
        _advance_to_next_case(page)
        _submit_structured(page)
        _advance_to_next_case(page)
        _submit_structured(page)
        page.click("#next-btn")
        page.wait_for_timeout(1000)  # results screen has no distinctive selector to wait on
        assert_snapshot(panel_locator)


    def test_session_expired_screen(self, page, flow_page, student_id, panel_locator, assert_snapshot, base_url):
        """Issue #145: no token anywhere (fresh tab, no fragment) -> the approved message, no retry button."""
        page.clock.set_fixed_time(FROZEN_TIME)
        page.set_viewport_size(VIEWPORT)
        page.goto(f"{base_url}/{flow_page}?student_id={student_id}&session_id=vr")
        page.wait_for_selector("#session-expired", timeout=15000)
        assert_snapshot(panel_locator)


class TestSubmitErrorRecovery:
    """Not a visual-snapshot test (no assert_snapshot here) -- a
    functional check of onSubmit()'s error handling itself (issue #61).

    A 409 VALIDATION_CASE_MISMATCH/DUPLICATE_SUBMISSION from /api/submit
    means the server's own progress has moved on, or this case was already
    answered, since this page last loaded it -- never something the
    student did knowingly (this page only ever submits
    currentCase.case_id, whatever /api/case last handed it). The fix must
    resync automatically (a real, unmocked GET /api/case) rather than
    stranding the student behind a raw HTTP error requiring a manual
    click. /api/submit itself is mocked here (page.route) purely to force
    this response deterministically, without needing to fabricate the
    exact backend race that would produce it for real -- that scenario is
    already covered against the real backend by
    docker/grading-api/tests/test_state_machine.py's own
    test_submit_a_later_case_in_the_same_stage_returns_409_and_leaves_progress_unchanged.
    """

    def test_case_mismatch_409_auto_resyncs_without_a_manual_click(self, page, flow_page, student_id, token, base_url):
        _open(page, base_url, flow_page, token, student_id, "vr")

        page.route(
            "**/api/submit",
            lambda route: route.fulfill(
                status=409,
                json={"error_code": "VALIDATION_CASE_MISMATCH", "message": "case_id doesn't match the currently assigned case"},
            ),
            times=1,
        )
        page.fill("#text-input", "Odpowiedź wysłana ze stanem, który serwer odrzuci jako nieaktualny.")

        # Synchronize on the actual mocked network response landing, not on
        # a guessed DOM-timing window -- two DOM-race approaches were tried
        # and both proved flaky: checking #submit-btn right after the click
        # can pass trivially before onSubmit()'s async fetch/catch has done
        # anything at all (it can still be the pre-click button), and
        # requiring it to detach first can *also* false-fail if the mocked
        # response and the resulting resync both complete faster than this
        # test's own next line executes. expect_response removes the
        # guesswork: this blocks until that exact response is observed,
        # so everything checked afterward is genuinely after the mock fired.
        with page.expect_response(lambda r: r.url.endswith("/api/submit") and r.status == 409):
            page.click("#submit-btn")

        # No #back-btn click anywhere above -- if onSubmit() regressed to
        # the old generic-error path (which only reaches a fresh case form
        # again via a manual #back-btn click), this would time out.
        page.wait_for_selector("#submit-btn", timeout=15000)
