"""Token survival across reloads, and the expired-session screen (issue #145).

The token arrives as a URL fragment, the page strips it from the address bar
(#94/#121) and keeps it in sessionStorage so F5 / Ctrl+R / a restored tab do not
strand the student on a raw HTTP 401. A missing or rejected token shows the owner-
approved message with no retry button and no automatic refresh. Every test also
proves the token never appears in a request URL (the #94 invariant).

Run via scripts/test-visual-regression.sh (it provisions the stack these tests
mint tokens against), like the visual tests.
"""

import json
import os
import urllib.error
import urllib.request

import pytest

GRADING_API_URL = os.environ.get("GRADING_API_URL", "http://localhost:8080/api")
COORDINATOR_KEY = os.environ["GRADING_COORDINATOR_KEY"]
EXPECTED_TEXT = "Sesja wygasła lub link jest nieprawidłowy. Zamknij okno i otwórz ponownie link otrzymany od koordynatora."
VIEWPORT = {"width": 1280, "height": 800}
UNKNOWN_TOKEN = "A" * 43  # right shape (token_urlsafe), never minted


def mint(student_id: str, session_id: str = "sess_token_test") -> str:
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


@pytest.fixture(params=["watermark.html", "grading-panel.html"])
def flow_page(request):
    return request.param


@pytest.fixture
def student(request, flow_page):
    name = (request.node.originalname or request.node.name).replace("test_", "")
    return f"TK_{name}_{flow_page.split('.')[0].replace('-', '_')}"[:128]


class Recorder:
    """Every request URL, every /api/case token header, console errors and page errors."""

    def __init__(self, page):
        self.urls, self.case_tokens, self.api_calls, self.errors = [], [], [], []
        page.on("request", self._on_request)
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" and "401" not in m.text else None)

    def _on_request(self, req):
        self.urls.append(req.url)
        if "/api/" in req.url:
            self.api_calls.append(req.url)
        if req.url.endswith("/api/case"):
            self.case_tokens.append(req.headers.get("x-grading-token"))

    def assert_token_never_in_a_url(self, *tokens):
        for t in tokens:
            assert not [u for u in self.urls if t in u], "a token appeared in a request URL"


def open_page(page, base_url, path, student, token=None, query_extra=""):
    page.set_viewport_size(VIEWPORT)
    frag = f"#token={token}" if token else ""
    page.goto(f"{base_url}/{path}?student_id={student}&session_id=sess_token_test{query_extra}{frag}")
    page.wait_for_selector("#grading-panel button, #session-expired", timeout=15000)


def stored_token(page):
    return page.evaluate("() => { try { return sessionStorage.getItem('grading_token'); } catch (e) { return 'BLOCKED'; } }")


def finish_flow(page):
    """learning -> assessment -> test -> the end-of-exam review (the stack's default cases);
    the doctor then approves it (issue #202) to reach the results."""
    page.fill("#text-input", "Odpowiedź testowa.")
    page.click("#submit-btn")
    page.wait_for_selector("#next-btn", timeout=15000)
    page.click("#next-btn")
    for _ in range(2):  # assessment, test
        page.wait_for_selector("#category-input", timeout=15000)
        page.select_option("#category-input", "2")
        page.click("#submit-btn")
        page.wait_for_selector("#next-btn", timeout=15000)
        page.click("#next-btn")
    page.wait_for_selector("#approve-btn", timeout=15000)


def test_reload_keeps_the_session(page, base_url, flow_page, student):
    rec = Recorder(page)
    token = mint(student)
    open_page(page, base_url, flow_page, student, token)
    page.wait_for_selector("#submit-btn")
    page.reload()
    page.wait_for_selector("#submit-btn", timeout=15000)
    assert page.locator("#session-expired").count() == 0
    assert page.evaluate("location.hash") == ""  # the address bar stays clean (#94/#121)
    rec.assert_token_never_in_a_url(token)
    assert rec.case_tokens[-1] == token  # the reload still authenticates, via the header
    assert not rec.errors


def test_reload_on_the_review_and_results_screens_keeps_the_session(page, base_url, flow_page, student):
    """Issue #145 + #202: after the last case the doctor is on the review screen; a reload keeps
    them there (token survives), and after approval a reload goes straight to the results."""
    rec = Recorder(page)
    token = mint(student)
    open_page(page, base_url, flow_page, student, token)
    finish_flow(page)
    page.wait_for_selector("#approve-btn", timeout=15000)
    page.reload()
    page.wait_for_selector("#approve-btn", timeout=15000)
    assert page.locator("#session-expired").count() == 0
    page.click("#approve-btn")
    page.wait_for_selector("#restart-btn", timeout=15000)
    page.reload()
    page.wait_for_selector("#restart-btn", timeout=15000)
    assert page.locator("#session-expired").count() == 0
    rec.assert_token_never_in_a_url(token)


def test_a_new_tab_without_a_fragment_shows_the_expired_message(page, context, base_url, flow_page, student):
    token = mint(student)
    open_page(page, base_url, flow_page, student, token)  # tab 1 holds a stored token
    tab2 = context.new_page()
    rec = Recorder(tab2)
    open_page(tab2, base_url, flow_page, student)  # tab 2: sessionStorage is per tab -> empty
    assert tab2.inner_text("#session-expired") == EXPECTED_TEXT
    assert tab2.locator("#retry-btn, #back-btn").count() == 0  # nothing to retry
    assert "HTTP 401" not in tab2.inner_text("#grading-panel")
    assert not rec.api_calls  # with no token the page does not even call the API
    assert stored_token(tab2) is None  # and a new tab never inherits tab 1's token (localStorage would)


def test_an_unknown_token_shows_the_message_and_clears_storage(page, base_url, flow_page, student):
    rec = Recorder(page)
    open_page(page, base_url, flow_page, student, UNKNOWN_TOKEN)
    assert page.inner_text("#session-expired") == EXPECTED_TEXT
    assert page.locator("#retry-btn").count() == 0
    assert stored_token(page) is None
    rec.assert_token_never_in_a_url(UNKNOWN_TOKEN)


@pytest.mark.parametrize("garbage", ["short", "has spaces in it and more", "x" * 200, "tok/en+with=bad*chars!!"])
def test_a_malformed_fragment_is_never_stored_or_sent(page, base_url, flow_page, student, garbage):
    rec = Recorder(page)
    page.set_viewport_size(VIEWPORT)
    page.goto(f"{base_url}/{flow_page}?student_id={student}&session_id=s#token={garbage}")
    page.wait_for_selector("#session-expired", timeout=15000)
    assert stored_token(page) is None
    assert not rec.api_calls


def test_a_new_fragment_wins_over_the_stored_token(page, base_url, flow_page, student):
    rec = Recorder(page)
    first = mint(student)
    open_page(page, base_url, flow_page, student, first)
    second = mint(student)  # minting again revokes `first`
    open_page(page, base_url, flow_page, student, second, query_extra="&second=1")  # a real navigation, not a hash change
    page.wait_for_selector("#submit-btn", timeout=15000)
    assert rec.case_tokens[-1] == second
    assert stored_token(page) == second
    rec.assert_token_never_in_a_url(first, second)


def test_storage_blocked_works_in_memory_and_expires_on_reload(page, context, base_url, flow_page, student):
    context.add_init_script("Object.defineProperty(window, 'sessionStorage', { get() { throw new Error('blocked'); } });")
    rec = Recorder(page)
    token = mint(student)
    open_page(page, base_url, flow_page, student, token)
    page.wait_for_selector("#submit-btn")  # the flow works from memory
    page.reload()
    page.wait_for_selector("#session-expired", timeout=15000)  # nothing survived the reload
    assert page.inner_text("#session-expired") == EXPECTED_TEXT
    assert not rec.errors  # no uncaught exception from the blocked storage
    rec.assert_token_never_in_a_url(token)


def test_a_token_revoked_mid_session_shows_the_message_on_submit(page, base_url, flow_page, student):
    rec = Recorder(page)
    token = mint(student)
    open_page(page, base_url, flow_page, student, token)
    page.wait_for_selector("#text-input")
    mint(student)  # revokes `token` server-side, like an expiry or a logout/re-issue
    page.fill("#text-input", "Odpowiedź po wygaśnięciu tokenu.")
    page.click("#submit-btn")
    page.wait_for_selector("#session-expired", timeout=15000)
    assert page.inner_text("#session-expired") == EXPECTED_TEXT
    assert page.locator("#back-btn, #retry-btn").count() == 0
    assert stored_token(page) is None
    rec.assert_token_never_in_a_url(token)
