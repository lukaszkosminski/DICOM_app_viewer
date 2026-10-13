"""
Ground truth stays invisible until it is legitimately revealed (issue #195, item 2; red-team
task RT-X-02 "answer key for unattempted cases").

Every stage gets SENTINEL values (title, study UID, reference report). A student walks through
the exam and, at every step, ALL endpoints they can reach are swept: nothing that belongs to a
stage they have not been through yet (and nothing from the test stage's truth before approval)
may appear in any response body, error body or log line. A leak fails with the sentinel's name.

What IS allowed at each step is the documented behaviour: /case shows the CURRENT case (title and
study UID); /submit reveals the report for learning and the category for assessment; the test
stage reveals nothing until POST /finalize.
"""

import json
import logging

import pytest

from app import db as db_module
from app.logging_config import _JsonFormatter

H = "X-Grading-Token"
STAGES = ("learning", "assessment", "test")
# per-stage sentinels (unique, grep-able, never appear in the seed data)
SENT = {
    stage: {
        "title": f"SENTINEL-TITLE-{stage.upper()}",
        "uid": f"9.9.{i}.{'7' * 12}",
        "report": f"SENTINEL-REPORT-{stage.upper()}-DO-NOT-LEAK",
    }
    for i, stage in enumerate(STAGES, start=1)
}


@pytest.fixture
def exam(client, mint_token):
    """A fresh database whose three cases carry the sentinels, and a student token."""
    conn = db_module.get_connection()
    try:
        for stage in STAGES:
            conn.execute(
                "UPDATE cases SET title = ?, orthanc_study_uid = ?, reference_report = ? WHERE stage = ?",
                (SENT[stage]["title"], SENT[stage]["uid"], SENT[stage]["report"], stage),
            )
        conn.commit()
    finally:
        conn.close()
    return mint_token("stu_invisible")


def _sweep(client, token):
    """Every GET a student can make, plus a few that fail; returns [(label, text)]."""
    seen = []
    for path in ("/case", "/review", "/results", "/healthz"):
        resp = client.get(path, headers={H: token})
        seen.append((f"GET {path} -> {resp.status_code}", resp.text))
    for label, resp in (
        ("GET /case without token", client.get("/case")),
        ("GET /case bad token", client.get("/case", headers={H: "nope"})),
        ("POST /submit bad stage", client.post("/submit", json={"token": token, "case_id": 1, "stage": "bogus"})),
        (
            "POST /submit bad category",
            client.post(
                "/submit", json={"token": token, "case_id": 1, "stage": "test", "category": "9", "modifier_s": False}
            ),
        ),
        ("POST /finalize too early", client.post("/finalize", json={"token": token})),
    ):
        seen.append((f"{label} -> {resp.status_code}", resp.text))
    return seen


def _assert_hidden(seen, hidden_stages, what, hide_report_of=()):
    """No sentinel of `hidden_stages` anywhere; and the reports of `hide_report_of` neither."""
    for label, text in seen:
        for stage in hidden_stages:
            for kind in ("title", "uid", "report"):
                assert SENT[stage][kind] not in text, f"{what}: {kind} of the {stage} case leaked in {label}"
        for stage in hide_report_of:
            assert SENT[stage]["report"] not in text, f"{what}: report of the {stage} case leaked in {label}"


def _submit(client, token, **body):
    case = client.get("/case", headers={H: token}).json()
    return client.post("/submit", json={"token": token, "case_id": case["case_id"], "stage": case["stage"], **body})


def test_nothing_of_later_stages_is_visible_while_learning(client, exam):
    seen = _sweep(client, exam)
    _assert_hidden(seen, hidden_stages=("assessment", "test"), what="learning stage")
    assert SENT["learning"]["uid"] in dict(seen)["GET /case -> 200"]  # the current case IS shown


def test_the_learning_submit_reveals_only_the_learning_report(client, exam, caplog):
    with caplog.at_level(logging.INFO):
        resp = _submit(client, exam, text="moja ocena")
    assert resp.status_code == 200 and resp.json() == {"reference_report": SENT["learning"]["report"]}
    for stage in ("assessment", "test"):
        assert all(SENT[stage][k] not in resp.text for k in SENT[stage])
    rendered = "\n".join(_JsonFormatter().format(r) for r in caplog.records)
    assert not any(SENT[s][k] in rendered for s in STAGES for k in ("report", "title"))  # nothing in the logs
    _assert_hidden(_sweep(client, exam), hidden_stages=("test",), what="after learning", hide_report_of=("learning",))


def test_during_assessment_the_test_case_and_every_report_stay_hidden(client, exam):
    _submit(client, exam, text="x")
    seen = _sweep(client, exam)
    _assert_hidden(seen, hidden_stages=("test",), what="assessment stage", hide_report_of=("learning", "assessment"))
    assert SENT["assessment"]["uid"] in dict(seen)["GET /case -> 200"]


def test_the_assessment_submit_reveals_a_verdict_but_no_report_and_nothing_of_the_test(client, exam, caplog):
    _submit(client, exam, text="x")
    with caplog.at_level(logging.INFO):
        resp = _submit(client, exam, category="3", modifier_s=False)
    body = resp.json()
    assert "ground_truth_category" in body and "reference_report" not in body
    assert all(SENT["test"][k] not in resp.text for k in SENT["test"])
    assert not any(SENT[s]["report"] in "\n".join(_JsonFormatter().format(r) for r in caplog.records) for s in STAGES)
    # the TEST case is now the current case (its title and study UID are shown, by design), but no
    # report of any stage may be visible
    _assert_hidden(_sweep(client, exam), hidden_stages=(), what="after assessment", hide_report_of=STAGES)


def test_the_test_stage_reveals_nothing_in_submit_and_nothing_in_any_report_until_approval(client, exam, caplog):
    _submit(client, exam, text="x")
    _submit(client, exam, category="3", modifier_s=False)
    seen = _sweep(client, exam)
    _assert_hidden(seen, hidden_stages=(), what="test stage", hide_report_of=("test",))
    with caplog.at_level(logging.INFO):
        resp = _submit(client, exam, category="2", modifier_s=True)
    assert resp.status_code == 200 and resp.json() == {}  # no feedback at all in the test stage
    # complete but NOT approved: the truth of the graded stages is still sealed
    for label, text in _sweep(client, exam):
        for stage in ("assessment", "test"):
            assert SENT[stage]["report"] not in text, f"report of the {stage} case leaked in {label} before approval"
        assert "ground_truth" not in text and "reference_report" not in text, f"truth keys in {label}"
    rendered = "\n".join(_JsonFormatter().format(r) for r in caplog.records)
    assert SENT["test"]["report"] not in rendered


def test_after_approval_the_truth_appears_only_in_results(client, exam):
    for body in ({"text": "x"}, {"category": "3", "modifier_s": False}, {"category": "2", "modifier_s": True}):
        _submit(client, exam, **body)
    assert client.post("/finalize", json={"token": exam}).status_code == 200
    results = client.get("/results", headers={H: exam}).text
    assert all(SENT[s]["report"] in results for s in STAGES)  # now, and only here
    review = client.get("/review", headers={H: exam}).text
    assert not any(SENT[s]["report"] in review for s in STAGES) and "ground_truth" not in review


def test_another_students_progress_never_unlocks_anything(client, mint_token, exam):
    """A second student still in the learning stage must see nothing of the first student's
    later stages, whatever the first one has done."""
    for body in ({"text": "x"}, {"category": "3", "modifier_s": False}, {"category": "2", "modifier_s": True}):
        _submit(client, exam, **body)
    client.post("/finalize", json={"token": exam})
    other = mint_token("stu_other")
    _assert_hidden(_sweep(client, other), hidden_stages=("assessment", "test"), what="a second student")
    assert json.loads(client.get("/results", headers={H: other}).text) == {"complete": False}
