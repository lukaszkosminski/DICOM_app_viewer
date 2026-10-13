"""Lung-RADS scale in bold (issue #201): behaviour tests for both panel pages.

The grading-api is replaced by Playwright route mocks, so these tests need only the viewer
(served at --base-url), not a database. They check the DOM, not pixels (pixels are the
visual-regression tests' job): which text is wrapped in <strong class="lr">, that the
wrapping can never inject markup from a report, and that the category choices are bold.
"""

import json

import pytest

PAGES = ["watermark.html", "grading-panel.html"]
REPORT = (
    "Lung-RADS 4A. Kategoria 3 jako zmiana pozorna; LR-4B S oraz kategorii 4X. "
    "Brak: Lung-RADS 5, 4Ax, category 2. <script>window.__pwned = 1</script> <b>x</b>"
)
LABELS = [["2", "Kategoria 2 – zmiany łagodne"], ["4A", "Kategoria 4A – podejrzane"]]


def _mock_api(page, stage, submit_result):
    case = {
        "complete": False,
        "case_id": 1,
        "stage": stage,
        "position": 1,
        "total_in_stage": 1,
        "title": "Przypadek testowy",
        "orthanc_study_uid": "1.2.3",
    }
    if stage != "learning":
        case["category_options"] = LABELS
    page.route("**/api/case", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(case)))
    page.route("**/api/submit", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(submit_result)))


def _open(page, base_url, path):
    page.goto(f"{base_url}/{path}?student_id=T&session_id=1#token=" + "t" * 43)
    page.wait_for_selector("#grading-panel button", timeout=15000)


@pytest.mark.parametrize("path", PAGES)
def test_reference_report_marks_the_scale_in_bold_and_nothing_else(page, base_url, path):
    _mock_api(page, "learning", {"reference_report": REPORT})
    _open(page, base_url, path)
    page.fill("#text-input", "moja ocena")
    page.click("#submit-btn")
    page.wait_for_selector(".answer-box.reference")
    bold = page.locator(".answer-box.reference strong.lr").all_inner_texts()
    assert bold == ["Lung-RADS 4A", "Kategoria 3", "LR-4B S", "kategorii 4X"]


@pytest.mark.parametrize("path", PAGES)
def test_markup_inside_a_report_stays_inert(page, base_url, path):
    _mock_api(page, "learning", {"reference_report": REPORT})
    _open(page, base_url, path)
    page.fill("#text-input", "x")
    page.click("#submit-btn")
    page.wait_for_selector(".answer-box.reference")
    assert page.evaluate("window.__pwned") is None
    assert page.locator(".answer-box.reference script").count() == 0
    assert page.locator(".answer-box.reference b").count() == 0
    assert "<script>" in page.locator(".answer-box.reference p").inner_text()  # shown as text


@pytest.mark.parametrize("path", PAGES)
def test_the_students_own_text_is_not_touched(page, base_url, path):
    _mock_api(page, "learning", {"reference_report": "x"})
    _open(page, base_url, path)
    page.fill("#text-input", "Lung-RADS 4A wedlug mnie")
    page.click("#submit-btn")
    page.wait_for_selector(".answer-box.reference")
    assert page.locator(".answer-box:not(.reference) strong").count() == 0


@pytest.mark.parametrize("path", PAGES)
def test_assessment_shows_the_reference_category_in_bold_and_the_choices_bold(page, base_url, path):
    _mock_api(page, "assessment", {"correct": False, "ground_truth_category": "4A", "ground_truth_modifier_s": 1})
    _open(page, base_url, path)
    weights = page.evaluate(
        "() => [getComputedStyle(document.querySelector('#category-input')).fontWeight,"
        " ...[...document.querySelectorAll('#category-input option')].map(o => getComputedStyle(o).fontWeight)]"
    )
    assert all(int(w) >= 700 for w in weights), weights
    page.select_option("#category-input", "2")
    page.click("#submit-btn")
    page.wait_for_selector("#next-btn")
    assert page.locator("strong.lr").all_inner_texts() == ["4A (S)"]
    assert int(page.evaluate("getComputedStyle(document.querySelector('strong.lr')).fontWeight")) >= 700
