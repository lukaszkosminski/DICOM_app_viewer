"""Stage indicator + score summary behaviour (UI polish, PR #221): DOM-level tests, both pages.

The API is mocked through Playwright routes (same pattern as test_lung_rads_bold.py), so
only a served viewer at --base-url is needed, no database. Pixels stay the job of the
visual-regression suite; these tests pin the CONTRACT the restyle must not break:
exactly one emphasized chip per case screen, no indicator on non-case screens, the bar
width equal to the accuracy percentage, scale order in the per-category table with
unknown categories last, and nothing from the API rendering as markup.
"""

import json

import pytest

PAGES = ["watermark.html", "grading-panel.html"]
TOKEN = "t" * 43
LABELS = {"learning": "Nauka", "assessment": "Ocena", "test": "Test"}
OPTIONS = [["0", "Kategoria 0"], ["2", "Kategoria 2"], ["4A", "Kategoria 4A"]]


def _case_route(stage):
    case = {
        "complete": False,
        "case_id": 1,
        "stage": stage,
        "position": 1,
        "total_in_stage": 2,
        "title": "Przypadek testowy",
        "orthanc_study_uid": "1.2.3",
    }
    if stage != "learning":
        case["category_options"] = OPTIONS
    return json.dumps(case)


def _open(page, base_url, path, wait="#grading-panel button"):
    page.goto(f"{base_url}/{path}?student_id=T&session_id=1#token={TOKEN}")
    page.wait_for_selector(wait, timeout=15000)


def _mock_results_flow(page, results_body):
    """Drive the panel from the review screen to the results screen (approval flow)."""
    page.route(
        "**/api/case",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"complete": True})),
    )
    page.route(
        "**/api/review",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"complete": True, "finalized": False, "items": []}),
        ),
    )
    page.route(
        "**/api/finalize",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps({"finalized": True})),
    )
    page.route("**/api/results", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(results_body)))


@pytest.mark.parametrize("stage", ["learning", "assessment", "test"])
@pytest.mark.parametrize("path", PAGES)
def test_every_case_screen_shows_exactly_one_current_chip(page, base_url, path, stage):
    page.route("**/api/case", lambda r: r.fulfill(status=200, content_type="application/json", body=_case_route(stage)))
    _open(page, base_url, path)
    chips = page.locator("#grading-panel .stage-chip")
    assert chips.count() == 3
    current = page.locator("#grading-panel .stage-chip.current")
    assert current.count() == 1
    assert current.inner_text() == LABELS[stage]


@pytest.mark.parametrize("path", PAGES)
def test_feedback_keeps_the_stage_emphasized(page, base_url, path):
    page.route("**/api/case", lambda r: r.fulfill(status=200, content_type="application/json", body=_case_route("assessment")))
    page.route(
        "**/api/submit",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"correct": False, "ground_truth_category": "4A", "ground_truth_modifier_s": False}),
        ),
    )
    _open(page, base_url, path)
    page.select_option("#category-input", "2")
    page.click('input[name="modifier-s"][value="no"]')
    page.click("#submit-btn")
    page.wait_for_selector(".feedback-incorrect, .feedback-correct", timeout=15000)
    current = page.locator("#grading-panel .stage-chip.current")
    assert current.count() == 1 and current.inner_text() == "Ocena"


@pytest.mark.parametrize("path", PAGES)
def test_no_stage_bar_on_review_results_or_expired(page, base_url, path):
    _mock_results_flow(
        page,
        {
            "complete": True,
            "finalized": True,
            "test_total": 1,
            "test_correct": 1,
            "accuracy": 1.0,
            "breakdown": [{"ground_truth": "4A", "correct": 1}],
            "reset_enabled": False,
            "items": [],
        },
    )
    _open(page, base_url, path, wait="#approve-btn")
    assert page.locator("#grading-panel .stage-bar").count() == 0  # review screen
    page.click("#approve-btn")
    page.wait_for_selector("#results-title", timeout=15000)
    assert page.locator("#grading-panel .stage-bar").count() == 0  # results screen
    page.route("**/api/case", lambda r: r.fulfill(status=401, content_type="application/json", body="{}"))


@pytest.mark.parametrize("path", PAGES)
def test_expired_session_screen_carries_no_indicator(page, base_url, path):
    page.route("**/api/case", lambda r: r.fulfill(status=401, content_type="application/json", body="{}"))
    _open(page, base_url, path, wait="#grading-panel .error, #grading-panel button")
    assert page.locator("#grading-panel .stage-bar").count() == 0


@pytest.mark.parametrize("path", PAGES)
def test_score_bar_width_and_label_match_the_accuracy(page, base_url, path):
    _mock_results_flow(
        page,
        {
            "complete": True,
            "finalized": True,
            "test_total": 4,
            "test_correct": 3,
            "accuracy": 0.75,
            "breakdown": [],
            "reset_enabled": False,
            "items": [],
        },
    )
    _open(page, base_url, path, wait="#approve-btn")
    page.click("#approve-btn")
    page.wait_for_selector("#results-title", timeout=15000)
    fill = page.locator("#grading-panel .score-fill")
    assert fill.count() == 1
    assert "width:75%" in fill.get_attribute("style").replace(" ", "")
    assert page.locator("#grading-panel .score-pct").inner_text() == "75%"


@pytest.mark.parametrize("path", PAGES)
def test_category_rows_follow_the_scale_order_and_unknowns_come_last(page, base_url, path):
    _mock_results_flow(
        page,
        {
            "complete": True,
            "finalized": True,
            "test_total": 6,
            "test_correct": 3,
            "accuracy": 0.5,
            "breakdown": [
                {"ground_truth": "4B", "correct": 1},
                {"ground_truth": "0", "correct": 1},
                {"ground_truth": "weird", "correct": 0},
                {"ground_truth": "4A", "correct": 0},
                {"ground_truth": "2", "correct": 1},
            ],
            "reset_enabled": False,
            "items": [],
        },
    )
    _open(page, base_url, path, wait="#approve-btn")
    page.click("#approve-btn")
    page.wait_for_selector("#results-title", timeout=15000)
    rows = page.locator("#grading-panel .score-cats td.lr").all_inner_texts()
    assert rows == ["0", "2", "4A", "4B", "weird"]  # scale order first, unknown last


@pytest.mark.parametrize("path", PAGES)
def test_category_labels_are_escaped(page, base_url, path):
    _mock_results_flow(
        page,
        {
            "complete": True,
            "finalized": True,
            "test_total": 1,
            "test_correct": 0,
            "accuracy": 0.0,
            "breakdown": [{"ground_truth": "4A<img src=x onerror=window.__pwned=1>", "correct": 0}],
            "reset_enabled": False,
            "items": [],
        },
    )
    _open(page, base_url, path, wait="#approve-btn")
    page.click("#approve-btn")
    page.wait_for_selector("#results-title", timeout=15000)
    assert page.locator("#grading-panel .score-cats img").count() == 0
    assert page.evaluate("window.__pwned") is None
    assert "<img" in page.locator("#grading-panel .score-cats td.lr").inner_text()


@pytest.mark.parametrize("path", PAGES)
def test_null_accuracy_hides_the_bar_and_empty_breakdown_hides_the_table(page, base_url, path):
    _mock_results_flow(
        page,
        {
            "complete": True,
            "finalized": True,
            "test_total": 0,
            "test_correct": 0,
            "accuracy": None,
            "breakdown": [],
            "reset_enabled": False,
            "items": [],
        },
    )
    _open(page, base_url, path, wait="#approve-btn")
    page.click("#approve-btn")
    page.wait_for_selector("#results-title", timeout=15000)
    assert page.locator("#grading-panel .score-bar").count() == 0
    assert page.locator("#grading-panel .score-cats").count() == 0
