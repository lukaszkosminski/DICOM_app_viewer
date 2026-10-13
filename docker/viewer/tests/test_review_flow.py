"""End-of-exam review and approval in the panel (issue #202), both pages, API mocked.

Checks the DOM, not pixels: the review lists every answer read-only with NO correct answers,
the approve button is the only way to the reveal, a double click sends one request, errors keep
the review on screen, expiry shows the expiry message, long lists scroll, and nothing that comes
from the API can inject markup.
"""

import json

import pytest

PAGES = ["watermark.html", "grading-panel.html"]
TOKEN = "t" * 43
LONG = "Opis " + "bardzo długi " * 40  # > 200 characters


def _items(n_learning=1, n_assessment=1, n_test=1, with_truth=False):
    out = []
    for stage, n in (("learning", n_learning), ("assessment", n_assessment), ("test", n_test)):
        for i in range(1, n + 1):
            item = {
                "stage": stage,
                "position": i,
                "total_in_stage": n,
                "title": f"Przypadek {i} <b>x</b>",
                "answer": {"text": LONG} if stage == "learning" else {"category": "2", "modifier_s": i % 2 == 0},
            }
            if with_truth:
                item.update(
                    {
                        "ground_truth_category": "4A",
                        "ground_truth_modifier_s": True,
                        "correct": None if stage == "learning" else (i % 2 == 0),
                        "reference_report": "Raport: Lung-RADS 4A <script>window.__pwned = 1</script>",
                    }
                )
            out.append(item)
    return out


class Api:
    """Route mocks; counts calls so tests can assert what the panel did and did not request."""

    def __init__(self, page, finalized=False, finalize_status=200, n=1, reset_enabled=False):
        self.reset_enabled = reset_enabled
        self.finalized = finalized
        self.finalize_status = finalize_status
        self.n = n
        self.calls = []
        page.route("**/api/case", lambda r: self._json(r, {"complete": True}))
        page.route("**/api/review", self._review)
        page.route("**/api/results", self._results)
        page.route("**/api/finalize", self._finalize)

    def _json(self, route, body, status=200):
        self.calls.append(route.request.url.rsplit("/api/", 1)[1])
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def _review(self, route):
        self._json(route, {"complete": True, "finalized": self.finalized, "items": _items(self.n, self.n, self.n)})

    def _results(self, route):
        if not self.finalized:
            return self._json(route, {"complete": True, "finalized": False})
        self._json(
            route,
            {
                "complete": True,
                "finalized": True,
                "test_total": self.n,
                "test_correct": 0,
                "accuracy": 0.0,
                "breakdown": [],
                "reset_enabled": self.reset_enabled,
                "items": _items(self.n, self.n, self.n, with_truth=True),
            },
        )

    def _finalize(self, route):
        if self.finalize_status == 200:
            self.finalized = True
            return self._json(route, {"finalized": True, "already_finalized": False})
        self._json(route, {"error_code": "X", "message": "boom"}, status=self.finalize_status)


def _open(page, base_url, path):
    page.goto(f"{base_url}/{path}?student_id=T&session_id=1#token={TOKEN}")
    page.wait_for_selector("#grading-panel button, #grading-panel .error", timeout=15000)


@pytest.mark.parametrize("path", PAGES)
def test_the_review_lists_every_answer_and_shows_no_correct_answers(page, base_url, path):
    api = Api(page, n=2)
    _open(page, base_url, path)
    text = page.inner_text("#grading-panel")
    assert "Podsumowanie odpowiedzi" in text and "Zatwierdź i zakończ" in text
    assert page.locator("#grading-panel .answer-box").count() == 6  # 3 stages x 2 cases
    assert "Kategoria referencyjna" not in text and "Poprawnie" not in text and "Raport" not in text
    assert "results" not in api.calls  # the panel must not even ask before approval
    assert page.locator("#grading-panel textarea, #grading-panel select, #grading-panel input").count() == 0  # read-only


@pytest.mark.parametrize("path", PAGES)
def test_long_learning_answers_are_shortened_and_expandable(page, base_url, path):
    Api(page)
    _open(page, base_url, path)
    summary = page.locator("#grading-panel details summary").first
    assert len(summary.inner_text()) <= 202 and summary.inner_text().endswith("…")
    assert not page.locator("#grading-panel details p").first.is_visible()
    summary.click()
    assert page.locator("#grading-panel details p").first.is_visible()


@pytest.mark.parametrize("path", PAGES)
def test_approval_reveals_the_correct_answers_and_the_score(page, base_url, path):
    api = Api(page, n=2)  # two cases per stage: one right, one wrong
    _open(page, base_url, path)
    page.click("#approve-btn")
    page.wait_for_selector("#results-title")
    text = page.inner_text("#grading-panel")
    assert "Wyniki końcowe" in text and "Wynik testu: 0 / 2" in text
    assert "Kategoria referencyjna" in text and "Niepoprawnie" in text and "Poprawnie" in text
    assert "finalize" in api.calls
    assert page.locator("#grading-panel strong.lr").count() >= 2


@pytest.mark.parametrize("path", PAGES)
def test_a_double_click_sends_exactly_one_approval(page, base_url, path):
    api = Api(page)
    _open(page, base_url, path)
    page.dblclick("#approve-btn")
    page.wait_for_selector("#results-title")
    assert api.calls.count("finalize") == 1


@pytest.mark.parametrize("path", PAGES)
def test_a_failed_approval_keeps_the_review_and_can_be_retried(page, base_url, path):
    api = Api(page, finalize_status=500)
    _open(page, base_url, path)
    page.click("#approve-btn")
    page.wait_for_function("document.querySelector('#approve-error').textContent.length > 0")
    assert "Nie udało się zatwierdzić" in page.inner_text("#approve-error")
    assert page.locator("#approve-btn").is_enabled()
    assert "Podsumowanie odpowiedzi" in page.inner_text("#grading-panel")
    api.finalize_status = 200
    page.click("#approve-btn")
    page.wait_for_selector("#results-title")


@pytest.mark.parametrize("path", PAGES)
def test_an_expired_session_during_approval_shows_the_expiry_message(page, base_url, path):
    Api(page, finalize_status=401)
    _open(page, base_url, path)
    page.click("#approve-btn")
    page.wait_for_selector("#session-expired")


@pytest.mark.parametrize("path", PAGES)
def test_already_approved_goes_straight_to_the_reveal_after_a_reload(page, base_url, path):
    Api(page, finalized=True)
    page.goto(f"{base_url}/{path}?student_id=T&session_id=1#token={TOKEN}")
    page.wait_for_selector("#results-title", timeout=15000)
    assert "Wyniki końcowe" in page.inner_text("#grading-panel")


@pytest.mark.parametrize("path", PAGES)
def test_api_data_never_injects_markup(page, base_url, path):
    Api(page, finalized=True)
    page.goto(f"{base_url}/{path}?student_id=T&session_id=1#token={TOKEN}")
    page.wait_for_selector("#results-title", timeout=15000)
    assert page.evaluate("window.__pwned") is None
    assert page.locator("#grading-panel script").count() == 0
    assert page.locator("#grading-panel b").count() == 0
    assert "<b>x</b>" in page.inner_text("#grading-panel")  # a title is shown as text


@pytest.mark.parametrize("path", PAGES)
def test_a_130_case_exam_scrolls_inside_the_panel(page, base_url, path):
    Api(page, n=43)  # 129 cases
    _open(page, base_url, path)
    page.set_viewport_size({"width": 420, "height": 700})
    box = page.evaluate(
        "() => { const p = document.querySelector('#grading-panel');"
        " return [p.scrollHeight, p.clientHeight, getComputedStyle(p).overflowY]; }"
    )
    assert box[0] > box[1] and box[2] in ("auto", "scroll")
    page.locator("#approve-btn").scroll_into_view_if_needed()
    assert page.locator("#approve-btn").is_visible()


@pytest.mark.parametrize("path", PAGES)
def test_the_restart_button_exists_only_when_the_server_allows_starting_over(page, base_url, path):
    """Issue #98: starting over deletes the recorded answers, so it is off unless the server says so."""
    Api(page, finalized=True, reset_enabled=False)
    page.goto(f"{base_url}/{path}?student_id=T&session_id=1#token={TOKEN}")
    page.wait_for_selector("#results-title", timeout=15000)
    assert page.locator("#restart-btn").count() == 0
    assert "Rozpocznij od początku" not in page.inner_text("#grading-panel")


@pytest.mark.parametrize("path", PAGES)
def test_the_restart_button_appears_when_enabled(page, base_url, path):
    Api(page, finalized=True, reset_enabled=True)
    page.goto(f"{base_url}/{path}?student_id=T&session_id=1#token={TOKEN}")
    page.wait_for_selector("#restart-btn", timeout=15000)
