"""
Unit tests for grading-api's 3-stage Lung-RADS state machine (issue #8)
and its session-token authorization (README.md flagged the lack of it as
a real, serious gap: student_id used to be a bare, client-supplied
parameter nothing checked against the caller -- any request could act as
any student).

These exist to protect the pedagogically/security-critical invariants this
project keeps stating in prose (README.md, CLAUDE.md, main.py's own
docstring) but had never had automated, isolated coverage for:
  - a token is required everywhere, and only POST /session can mint one,
    itself gated by a coordinator-only secret;
  - an invalid, unknown, or expired token is rejected, not silently
    treated as some default identity;
  - minting a new token for a student_id revokes whatever token existed
    before it for that same student_id;
  - ground truth and reference reports are NEVER returned before the stage
    that's supposed to reveal them;
  - the test stage reveals nothing at all, unlike learning/assessment;
  - progress correctly advances case -> case, then stage -> stage, then
    to "complete";
  - two students' progress and submissions never leak into each other;
  - the input validation main.py already has (stage mismatches) actually
    behaves as documented;
  - time-on-task is computed server-side from when a case actually became
    active, never trusted from whatever the client sends (issue #29).

Every test gets its own empty, isolated SQLite file via the `client`
fixture in conftest.py -- no shared state between tests, no dependency on
a real running stack (unlike scripts/smoke-test.sh, which is an
end-to-end check against the real docker-compose services).

Assertions below are pinned to the exact seed data in app/db.py's
SEED_CASES (one case per stage; learning's ground truth is category "2",
assessment's is "3", test's is "4A" with modifier_s=1) -- if that seed
data ever changes, the specific values asserted here need updating too,
not just the mechanics being tested.
"""

import sqlite3

import pytest

from app import db as db_module
from app import main as main_module


def _case_id(client, token):
    return client.get("/case", headers={"X-Grading-Token": token}).json()["case_id"]


# ---- Session tokens: the actual authorization mechanism ----


def test_session_requires_the_coordinator_key(client):
    resp = client.post(
        "/session",
        json={"student_id": "stu_1", "session_id": "sess_1"},
        headers={"X-Coordinator-Key": "definitely-not-the-real-key"},
    )
    assert resp.status_code == 401


def test_session_with_no_coordinator_key_header_is_rejected(client):
    resp = client.post("/session", json={"student_id": "stu_1", "session_id": "sess_1"})
    # CR #121 taxonomy rule: a missing auth credential answers 401
    # AUTH_INVALID_COORDINATOR_KEY, not FastAPI's parameter-validation 422
    # (main.py guards the optional Header itself, before compare_digest).
    assert resp.status_code == 401
    assert resp.json() == {
        "error_code": "AUTH_INVALID_COORDINATOR_KEY",
        "message": "Missing X-Coordinator-Key header",
    }


def test_case_rejects_an_unknown_token(client):
    resp = client.get("/case", headers={"X-Grading-Token": "this-token-was-never-minted"})
    assert resp.status_code == 401


def test_case_rejects_an_expired_token(client, mint_token):
    token = mint_token("stu_2")
    conn = db_module.get_connection()
    try:
        # Simulate time passing rather than waiting out the real TTL --
        # directly age the row past its own expiry.
        conn.execute("UPDATE sessions SET expires_at = 0 WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/case", headers={"X-Grading-Token": token})
    assert resp.status_code == 401


def test_minting_a_new_token_revokes_the_previous_one_for_that_student(client, mint_token):
    old_token = mint_token("stu_3")
    assert client.get("/case", headers={"X-Grading-Token": old_token}).status_code == 200

    new_token = mint_token("stu_3")
    assert new_token != old_token
    assert client.get("/case", headers={"X-Grading-Token": old_token}).status_code == 401
    assert client.get("/case", headers={"X-Grading-Token": new_token}).status_code == 200


def test_two_students_get_independent_tokens_and_state(client, mint_token):
    """The actual point of the whole token system: two different callers
    can never act as each other, even by guessing at student IDs -- there's
    no student_id parameter left anywhere for one to guess."""
    token_a = mint_token("stu_a")
    token_b = mint_token("stu_b")

    client.post(
        "/submit",
        json={
            "token": token_a,
            "case_id": _case_id(client, token_a),
            "stage": "learning",
            "text": "x",
            "time_spent_seconds": 1,
        },
    )

    assert client.get("/case", headers={"X-Grading-Token": token_a}).json()["stage"] == "assessment"
    assert client.get("/case", headers={"X-Grading-Token": token_b}).json()["stage"] == "learning"


# ---- The 3-stage state machine itself (all via a minted token) ----


def test_fresh_student_starts_at_learning_stage(client, mint_token):
    token = mint_token("stu_fresh")
    resp = client.get("/case", headers={"X-Grading-Token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert data["complete"] is False
    assert data["stage"] == "learning"
    assert data["position"] == 1
    assert data["total_in_stage"] == 1
    # Learning stage has no structured category picker -- category_options
    # is only added for assessment/test (see main.py's get_case()).
    assert "category_options" not in data


# ---- _get_or_create_progress() directly (issue #43) ----
# Only ever exercised indirectly above, through higher-level state-machine
# tests -- these two target its own two behaviors directly.


def test_new_student_gets_case_assigned_at_stamped_at_creation(client, mint_token, monkeypatch):
    """A brand-new student_id gets a fresh progress row with case_assigned_at
    stamped at that exact moment (issue #29)."""
    monkeypatch.setattr(db_module, "now", lambda: 1_700_000_000.0)
    token = mint_token("stu_new")

    _case_id(client, token)  # triggers _get_or_create_progress

    conn = db_module.get_connection()
    try:
        row = conn.execute(
            "SELECT case_assigned_at FROM progress WHERE student_id = ?",
            ("stu_new",),
        ).fetchone()
    finally:
        conn.close()

    assert row["case_assigned_at"] == 1_700_000_000.0


def test_existing_students_progress_row_is_returned_as_is_not_recreated(client, mint_token, monkeypatch):
    """A second /case call for the same student must return the existing
    progress row untouched, not silently recreate/re-stamp it -- advance the
    clock between two calls and confirm case_assigned_at doesn't move."""
    monkeypatch.setattr(db_module, "now", lambda: 1_700_000_000.0)
    token = mint_token("stu_existing")
    _case_id(client, token)  # first call creates the row

    monkeypatch.setattr(db_module, "now", lambda: 1_700_000_999.0)
    _case_id(client, token)  # second call must not recreate/re-stamp it

    conn = db_module.get_connection()
    try:
        row = conn.execute(
            "SELECT case_assigned_at FROM progress WHERE student_id = ?",
            ("stu_existing",),
        ).fetchone()
    finally:
        conn.close()

    assert row["case_assigned_at"] == 1_700_000_000.0


def test_case_response_never_leaks_ground_truth_or_reference_report(client, mint_token):
    """The single most important invariant this whole feature exists to
    enforce, checked across all three stages generically (not just
    "learning doesn't leak") so a future field added to the /case response
    can't quietly reintroduce this."""
    token = mint_token("stu_leak_check")
    seen_stages = []
    while True:
        data = client.get("/case", headers={"X-Grading-Token": token}).json()
        if data.get("complete"):
            break
        seen_stages.append(data["stage"])
        assert "ground_truth_category" not in data
        assert "ground_truth_modifier_s" not in data
        assert "reference_report" not in data

        body = {
            "token": token,
            "case_id": data["case_id"],
            "stage": data["stage"],
            "time_spent_seconds": 5,
        }
        if data["stage"] == "learning":
            body["text"] = "impression"
        else:
            body["category"] = "2"
            body["modifier_s"] = False
        client.post("/submit", json=body)

    assert seen_stages == ["learning", "assessment", "test"]


def test_learning_submit_reveals_reference_report_only(client, mint_token):
    token = mint_token("stu_2")
    case_id = _case_id(client, token)
    resp = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": case_id,
            "stage": "learning",
            "text": "moja ocena",
            "time_spent_seconds": 30,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {"reference_report"}
    assert "Lung-RADS 2" in data["reference_report"]


def test_learning_submit_advances_to_assessment_stage(client, mint_token):
    token = mint_token("stu_3")
    case_id = _case_id(client, token)
    client.post(
        "/submit",
        json={
            "token": token,
            "case_id": case_id,
            "stage": "learning",
            "text": "x",
            "time_spent_seconds": 1,
        },
    )
    data = client.get("/case", headers={"X-Grading-Token": token}).json()
    assert data["stage"] == "assessment"
    assert data["position"] == 1
    assert data["category_options"]  # dropdown options present now


def test_assessment_submit_correct_category_reveals_ground_truth(client, mint_token):
    # Drive stu_4 through learning first (mechanics already covered above;
    # here we're only checking assessment's own reveal behavior).
    token = mint_token("stu_4")
    case_id = _case_id(client, token)
    client.post(
        "/submit",
        json={"token": token, "case_id": case_id, "stage": "learning", "text": "x", "time_spent_seconds": 1},
    )
    case_id = _case_id(client, token)
    resp = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": case_id,
            "stage": "assessment",
            "category": "3",
            "modifier_s": False,
            "time_spent_seconds": 10,
        },
    )
    data = resp.json()
    assert data["correct"] is True
    assert data["ground_truth_category"] == "3"
    assert data["ground_truth_modifier_s"] is False


def test_assessment_submit_incorrect_category_still_reveals_ground_truth(client, mint_token):
    """Assessment always reveals ground truth, correct or not -- only the
    test stage withholds it entirely. Getting this backwards (e.g. hiding
    ground truth on a wrong answer) would break the stage's whole
    cross-check purpose."""
    token = mint_token("stu_5")
    case_id = _case_id(client, token)
    client.post(
        "/submit",
        json={"token": token, "case_id": case_id, "stage": "learning", "text": "x", "time_spent_seconds": 1},
    )
    case_id = _case_id(client, token)
    resp = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": case_id,
            "stage": "assessment",
            "category": "1",
            "modifier_s": False,
            "time_spent_seconds": 10,
        },
    )
    data = resp.json()
    assert data["correct"] is False
    assert data["ground_truth_category"] == "3"


def test_test_stage_submit_reveals_nothing(client, mint_token):
    """The other core invariant: no correct/incorrect feedback, no ground
    truth, nothing -- an empty object, not just a missing key or two."""
    token = mint_token("stu_6")
    for stage in ("learning", "assessment"):
        case_id = _case_id(client, token)
        body = {"token": token, "case_id": case_id, "stage": stage, "time_spent_seconds": 1}
        if stage == "learning":
            body["text"] = "x"
        else:
            body["category"] = "3"
            body["modifier_s"] = False
        client.post("/submit", json=body)

    case_id = _case_id(client, token)
    resp = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": case_id,
            "stage": "test",
            "category": "4A",
            "modifier_s": True,
            "time_spent_seconds": 10,
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {}


def test_completing_all_stages_marks_complete(client, mint_token):
    token = mint_token("stu_7")
    for stage in ("learning", "assessment", "test"):
        case_id = _case_id(client, token)
        body = {"token": token, "case_id": case_id, "stage": stage, "time_spent_seconds": 1}
        if stage == "learning":
            body["text"] = "x"
        else:
            body["category"] = "3"
            body["modifier_s"] = False
        client.post("/submit", json=body)

    data = client.get("/case", headers={"X-Grading-Token": token}).json()
    assert data == {"complete": True}


def test_results_before_completion(client, mint_token):
    token = mint_token("stu_8")
    resp = client.get("/results", headers={"X-Grading-Token": token})
    assert resp.json() == {"complete": False}


def test_results_after_completion_reports_accuracy(client, mint_token):
    token = mint_token("stu_9")
    for stage, category in (("learning", None), ("assessment", "3"), ("test", "4A")):
        case_id = _case_id(client, token)
        body = {"token": token, "case_id": case_id, "stage": stage, "time_spent_seconds": 1}
        if stage == "learning":
            body["text"] = "x"
        else:
            body["category"] = category  # both submitted correctly here
            body["modifier_s"] = stage == "test"
        client.post("/submit", json=body)

    # issue #202: the correct answers and the score appear only after the doctor approves
    assert client.post("/finalize", json={"token": token}).status_code == 200
    data = client.get("/results", headers={"X-Grading-Token": token}).json()
    assert data["complete"] is True
    assert data["test_total"] == 1
    assert data["test_correct"] == 1
    assert data["accuracy"] == 1.0
    assert data["breakdown"] == [{"ground_truth": "4A", "submitted": "4A", "correct": True}]


def test_submit_stage_mismatch_returns_409(client, mint_token):
    token = mint_token("stu_10")
    case_id = _case_id(client, token)  # student is actually at "learning"
    resp = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": case_id,
            "stage": "assessment",
            "category": "3",
            "modifier_s": False,
            "time_spent_seconds": 1,
        },
    )
    assert resp.status_code == 409


def test_submit_case_id_from_a_different_stage_returns_409(client, mint_token):
    """issue #61: case_id is now always resolved against progress's own
    position (_get_case(conn, progress["stage"], progress["case_order_index"])),
    never looked up by the client-supplied id directly -- a case_id
    belonging to a different stage entirely can no longer reach a distinct
    400 path (that VALIDATION_CASE_MISMATCH/400 codepath doesn't exist
    anymore); it's just another way body.case_id fails to match the one
    case that's actually assigned, so it's the same 409 as any other
    mismatch now (this test used to assert 400 -- see the test below for
    the same-stage, wrong-position case this issue actually targets)."""
    token = mint_token("stu_11")
    # case_id 2 is the seeded assessment-stage case, not learning's.
    resp = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": 2,
            "stage": "learning",
            "text": "x",
            "time_spent_seconds": 1,
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "VALIDATION_CASE_MISMATCH"


def test_submit_a_later_case_in_the_same_stage_returns_409_and_leaves_progress_unchanged(client, mint_token):
    """issue #61: submit() checked that body.case_id belonged to the
    submitted stage, but not that it was the case at
    progress["case_order_index"] specifically -- a client could submit an
    answer for a LATER case in the same stage. That would silently skip
    the actually-assigned case (never answered), _advance_progress() would
    still move on from the current index regardless, and when progress
    later reached the skipped case, its submission would already exist
    from this out-of-order request -- the same "stuck behind a 409"
    symptom as issue #60. In the assessment stage it would also hand back
    the ground truth of a case the student hasn't actually been shown yet.

    Only one case per stage exists in the current seed data (db.py's own
    SEED_CASES) -- a second "learning" case is inserted directly here,
    same technique already used by test_submit_duplicate_for_same_case_
    stage_returns_409 for a scenario seed data alone doesn't cover."""
    token = mint_token("stu_61")
    real_case_id = _case_id(client, token)  # the actually-assigned learning case (order_index 0)

    conn = db_module.get_connection()
    try:
        conn.execute(
            """INSERT INTO cases
               (stage, order_index, orthanc_study_uid, title, ground_truth_category,
                ground_truth_modifier_s, reference_report)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("learning", 1, "1.2.3.4.5.6", "Przypadek 2 (nauka)", "0", 0, "PLACEHOLDER"),
        )
        conn.commit()
        later_case_id = conn.execute("SELECT id FROM cases WHERE stage = 'learning' AND order_index = 1").fetchone()[0]
    finally:
        conn.close()

    resp = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": later_case_id,
            "stage": "learning",
            "text": "answering out of order",
            "time_spent_seconds": 1,
        },
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "VALIDATION_CASE_MISMATCH"

    # Progress must not have advanced, and neither case's submission should
    # exist -- the rejected request must leave no trace at all.
    data = client.get("/case", headers={"X-Grading-Token": token}).json()
    assert data["case_id"] == real_case_id
    assert data["position"] == 1

    conn = db_module.get_connection()
    try:
        submission_count = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE student_id = ?", ("stu_61",)
        ).fetchone()[0]
        assert submission_count == 0
    finally:
        conn.close()

    # The real, currently-assigned case must still be submittable normally
    # afterward -- the rejected out-of-order attempt didn't corrupt state.
    resp2 = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": real_case_id,
            "stage": "learning",
            "text": "answering in order",
            "time_spent_seconds": 1,
        },
    )
    assert resp2.status_code == 200


def test_submit_rejects_an_oversized_text_field(client, mint_token):
    """text is genuinely open-ended prose but not unbounded -- a client
    sending an absurdly large payload should get a clean 422 from Pydantic's
    own validation, not have it land in the DB."""
    token = mint_token("stu_oversized_text")
    case_id = _case_id(client, token)
    resp = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": case_id,
            "stage": "learning",
            "text": "x" * 10_001,
            "time_spent_seconds": 1,
        },
    )
    assert resp.status_code == 422


def test_submit_rejects_an_oversized_category_field(client, mint_token):
    """Real category values are at most 2 chars ("4A"/"4X") -- a much larger
    string should be rejected before it ever reaches the DB or the
    correctness comparison."""
    token = mint_token("stu_oversized_category")
    client.post(
        "/submit",
        json={
            "token": token,
            "case_id": _case_id(client, token),
            "stage": "learning",
            "text": "x",
            "time_spent_seconds": 1,
        },
    )  # advance past learning so this student is at "assessment"
    case_id = _case_id(client, token)
    resp = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": case_id,
            "stage": "assessment",
            "category": "x" * 11,
            "modifier_s": False,
            "time_spent_seconds": 1,
        },
    )
    assert resp.status_code == 422


def test_submit_rejects_an_oversized_stage_field(client, mint_token):
    """Real stage values are at most 10 chars ("assessment") -- a much
    larger string should be rejected rather than falling through to the
    409/400 stage-mismatch checks."""
    token = mint_token("stu_oversized_stage")
    case_id = _case_id(client, token)
    resp = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": case_id,
            "stage": "x" * 21,
            "text": "x",
            "time_spent_seconds": 1,
        },
    )
    assert resp.status_code == 422


def test_submit_computes_time_spent_seconds_server_side(client, mint_token, monkeypatch):
    """issue #29: time-on-task must come from progress.case_assigned_at
    (stamped server-side when the case became active), never from
    whatever the client sends -- directly manipulate case_assigned_at to
    simulate real elapsed time, then confirm the recorded submission
    matches that, not any client-supplied value.

    issue #42: db.now() is pinned to a fixed value for the whole test
    (rather than letting real wall-clock time pass between setting up
    case_assigned_at and the /submit call), so the expected
    time_spent_seconds can be asserted exactly instead of within a
    tolerance window."""
    monkeypatch.setattr(db_module, "now", lambda: 1_700_000_000.0)

    token = mint_token("stu_17")
    case_id = _case_id(client, token)

    conn = db_module.get_connection()
    try:
        conn.execute(
            "UPDATE progress SET case_assigned_at = ? WHERE student_id = ?",
            (db_module.now() - 42, "stu_17"),
        )
        conn.commit()
    finally:
        conn.close()

    client.post(
        "/submit",
        json={
            "token": token,
            "case_id": case_id,
            "stage": "learning",
            "text": "x",
            "time_spent_seconds": 99999,  # a lie -- must be ignored
        },
    )

    conn = db_module.get_connection()
    try:
        row = conn.execute(
            "SELECT time_spent_seconds FROM submissions WHERE student_id = ?",
            ("stu_17",),
        ).fetchone()
    finally:
        conn.close()

    assert row["time_spent_seconds"] == 42
    assert row["time_spent_seconds"] != 99999


def test_submit_clamps_time_spent_seconds_to_zero_if_the_clock_moves_backwards(client, mint_token):
    """issue #44: a server clock adjustment (e.g. an NTP correction) between
    case assignment and submission could otherwise make
    db.now() - case_assigned_at negative -- confirm it's clamped to 0
    instead of landing a negative value in submissions."""
    token = mint_token("stu_clock_skew")
    case_id = _case_id(client, token)

    conn = db_module.get_connection()
    try:
        conn.execute(
            "UPDATE progress SET case_assigned_at = ? WHERE student_id = ?",
            (db_module.now() + 3600, "stu_clock_skew"),
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.post(
        "/submit",
        json={
            "token": token,
            "case_id": case_id,
            "stage": "learning",
            "text": "x",
        },
    )
    assert resp.status_code == 200

    conn = db_module.get_connection()
    try:
        row = conn.execute(
            "SELECT time_spent_seconds FROM submissions WHERE student_id = ?",
            ("stu_clock_skew",),
        ).fetchone()
    finally:
        conn.close()

    assert row["time_spent_seconds"] == 0


def test_submit_resets_the_clock_for_the_next_case(client, mint_token):
    """Each case gets its own independently-measured time-on-task --
    submitting case 1 must not let its elapsed time leak into case 2's
    measurement."""
    token = mint_token("stu_18")
    case_id = _case_id(client, token)

    conn = db_module.get_connection()
    try:
        conn.execute(
            "UPDATE progress SET case_assigned_at = ? WHERE student_id = ?",
            (db_module.now() - 100, "stu_18"),
        )
        conn.commit()
    finally:
        conn.close()

    client.post(
        "/submit",
        json={"token": token, "case_id": case_id, "stage": "learning", "text": "x"},
    )

    case_id = _case_id(client, token)  # now at assessment
    client.post(
        "/submit",
        json={"token": token, "case_id": case_id, "stage": "assessment", "category": "3", "modifier_s": False},
    )

    conn = db_module.get_connection()
    try:
        row = conn.execute(
            "SELECT time_spent_seconds FROM submissions WHERE student_id = ? AND stage = 'assessment'",
            ("stu_18",),
        ).fetchone()
    finally:
        conn.close()

    assert row["time_spent_seconds"] < 5  # freshly stamped, not ~100s inherited from case 1


def test_reset_clears_progress_and_submissions(client, mint_token, reset_on):
    token = mint_token("stu_13")
    case_id = _case_id(client, token)
    client.post(
        "/submit",
        json={"token": token, "case_id": case_id, "stage": "learning", "text": "x", "time_spent_seconds": 1},
    )
    assert client.get("/case", headers={"X-Grading-Token": token}).json()["stage"] == "assessment"

    resp = client.post("/reset", json={"token": token})
    assert resp.status_code == 200
    assert resp.json() == {"reset": True}

    # Back to a fresh learning-stage case, as if this student had never
    # submitted anything.
    data = client.get("/case", headers={"X-Grading-Token": token}).json()
    assert data["stage"] == "learning"
    assert data["position"] == 1


def test_submit_duplicate_for_same_case_stage_returns_409(client, mint_token):
    """Exercises the UNIQUE(student_id, case_id, stage) constraint issue
    #28 added, by directly reproducing the end state of the race it
    protects against -- two requests both passing the stage check before
    either commits -- without needing real thread concurrency: insert a
    submission row directly, then attempt a normal /submit call for that
    same student/case/stage while progress hasn't advanced past it yet."""
    token = mint_token("stu_16")
    case_id = _case_id(client, token)

    conn = db_module.get_connection()
    try:
        conn.execute(
            """INSERT INTO submissions
               (student_id, case_id, stage, submitted_category, submitted_modifier_s,
                submitted_text, is_correct, time_spent_seconds, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("stu_16", case_id, "learning", None, None, "already submitted", None, 1, db_module.now()),
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.post(
        "/submit",
        json={"token": token, "case_id": case_id, "stage": "learning", "text": "x", "time_spent_seconds": 1},
    )
    assert resp.status_code == 409


def test_reset_is_blocked_during_test_stage(client, mint_token, reset_on):
    """The actual cheating vector issue #27 exists to close: without this
    guard, a student partway through the graded exam who doesn't like how
    it's going could reset and retry with a case sequence they now
    remember. Drive stu_14 all the way to the test stage, then confirm
    reset is refused and progress is left completely untouched."""
    token = mint_token("stu_14")
    for stage in ("learning", "assessment"):
        case_id = _case_id(client, token)
        body = {"token": token, "case_id": case_id, "stage": stage, "time_spent_seconds": 1}
        if stage == "learning":
            body["text"] = "x"
        else:
            body["category"] = "3"
            body["modifier_s"] = False
        client.post("/submit", json=body)

    assert client.get("/case", headers={"X-Grading-Token": token}).json()["stage"] == "test"

    resp = client.post("/reset", json={"token": token})
    assert resp.status_code == 403

    # Still exactly where it was -- the blocked attempt didn't partially
    # apply (e.g. clearing submissions but not progress, or vice versa).
    data = client.get("/case", headers={"X-Grading-Token": token}).json()
    assert data["stage"] == "test"
    assert data["position"] == 1


def test_reset_still_allowed_after_completion(client, mint_token, reset_on):
    """The one existing, legitimate use of reset (per main.py's own
    docstring: "once a student reaches 'complete', this is the way back
    to a fresh learning-stage case") must keep working -- the test-stage
    guard should be specific to "test", not accidentally block
    "complete" too."""
    token = mint_token("stu_15")
    for stage in ("learning", "assessment", "test"):
        case_id = _case_id(client, token)
        body = {"token": token, "case_id": case_id, "stage": stage, "time_spent_seconds": 1}
        if stage == "learning":
            body["text"] = "x"
        else:
            body["category"] = "3"
            body["modifier_s"] = False
        client.post("/submit", json=body)

    assert client.get("/case", headers={"X-Grading-Token": token}).json() == {"complete": True}

    resp = client.post("/reset", json={"token": token})
    assert resp.status_code == 200
    assert resp.json() == {"reset": True}
    assert client.get("/case", headers={"X-Grading-Token": token}).json()["stage"] == "learning"


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_database_uses_wal_mode(client):
    """Scaling concern raised while discussing a real multi-VM cohort
    deployment: confirms the connection actually ends up in WAL mode
    (allows concurrent reads during a write, reducing lock contention
    under many students submitting around the same moment), not just
    that the PRAGMA call is present in the source."""
    conn = db_module.get_connection()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_migration_adds_case_assigned_at_to_existing_progress_table(tmp_path, monkeypatch):
    """Reproduces a real bug found the hard way: rebuilding grading-api
    against a real, previously-running database (predating issue #29)
    broke every /case and /submit call outright with "no such column:
    case_assigned_at" -- CREATE TABLE IF NOT EXISTS never retroactively
    adds a column to a table that already exists. Builds an old-schema
    (3-column) progress table by hand, then confirms db.init_db()
    migrates it in place rather than crashing."""
    db_path = str(tmp_path / "old-schema-progress.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE progress (student_id TEXT PRIMARY KEY, stage TEXT NOT NULL, case_order_index INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO progress (student_id, stage, case_order_index) VALUES ('stu_old', 'learning', 0)")
    conn.commit()
    conn.close()

    db_module.init_db()  # must not raise

    conn = db_module.get_connection()
    try:
        row = conn.execute("SELECT case_assigned_at FROM progress WHERE student_id = 'stu_old'").fetchone()
        assert row["case_assigned_at"] is not None
    finally:
        conn.close()


def test_migration_adds_unique_constraint_to_existing_submissions_table(tmp_path, monkeypatch):
    """Same bug, the other schema change (issue #28): builds an
    old-schema submissions table with a pre-existing duplicate row (the
    exact situation the UNIQUE constraint didn't exist yet to prevent),
    confirms db.init_db() migrates it without crashing, keeps only the
    latest duplicate, and the constraint is genuinely active afterward."""
    db_path = str(tmp_path / "old-schema-submissions.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, student_id TEXT NOT NULL,
            case_id INTEGER NOT NULL, stage TEXT NOT NULL,
            submitted_category TEXT, submitted_modifier_s INTEGER,
            submitted_text TEXT, is_correct INTEGER,
            time_spent_seconds REAL, submitted_at REAL NOT NULL
        )"""
    )
    # A pre-constraint duplicate -- exactly what the constraint exists to
    # prevent going forward, but might already exist in real data.
    conn.execute(
        "INSERT INTO submissions (student_id, case_id, stage, submitted_text, submitted_at) "
        "VALUES ('stu_dup', 1, 'learning', 'first', 1.0)"
    )
    conn.execute(
        "INSERT INTO submissions (student_id, case_id, stage, submitted_text, submitted_at) "
        "VALUES ('stu_dup', 1, 'learning', 'second (latest)', 2.0)"
    )
    conn.commit()
    conn.close()

    db_module.init_db()  # must not raise

    conn = db_module.get_connection()
    try:
        rows = conn.execute("SELECT submitted_text FROM submissions WHERE student_id = 'stu_dup'").fetchall()
        assert len(rows) == 1
        assert rows[0]["submitted_text"] == "second (latest)"

        # Confirm the constraint is genuinely active now, not just that
        # the pre-existing duplicate got cleaned up once.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO submissions (student_id, case_id, stage, submitted_text, submitted_at) "
                "VALUES ('stu_dup', 1, 'learning', 'third', 3.0)"
            )
    finally:
        conn.close()


class _ForceNoRowOnFirstSelect:
    """Wraps a real sqlite3 connection so its first SELECT returns no
    row, even if one already exists in the database -- deterministically
    simulates the exact race window issue #41's fix protects against:
    this "request" already ran its own SELECT (seeing nothing) before
    another concurrent request's INSERT landed for the same student_id.
    Every other call (commit, later executes) passes through to the real
    connection unchanged."""

    def __init__(self, real_conn):
        self._real = real_conn
        self._forced = False

    def execute(self, sql, params=()):
        if not self._forced and sql.strip().upper().startswith("SELECT"):
            self._forced = True

            class _EmptyCursor:
                def fetchone(self_inner):
                    return None

            return _EmptyCursor()
        return self._real.execute(sql, params)

    def commit(self):
        self._real.commit()


def test_get_or_create_progress_survives_a_concurrent_insert_race(client, mint_token):
    """issue #41: two concurrent requests for the same brand-new
    student_id can both see "no row exists" before either INSERT
    commits -- student_id is the PRIMARY KEY, so the second INSERT then
    raises sqlite3.IntegrityError. Deterministically reproduces the exact
    interleaving (not a real, timing-dependent thread race) by wrapping
    the connection so its own SELECT is forced to see nothing, while a
    row for the same student_id already exists -- exactly what the
    "other" concurrent request would have already committed. Confirms
    _get_or_create_progress() catches it and returns the existing row
    cleanly, not an unhandled 500."""
    mint_token("stu_race")  # creates the sessions row; progress does not exist yet

    conn = db_module.get_connection()
    try:
        # The "other" concurrent request's INSERT, already committed.
        conn.execute(
            "INSERT INTO progress (student_id, stage, case_order_index, case_assigned_at) VALUES (?, 'learning', 0, ?)",
            ("stu_race", db_module.now()),
        )
        conn.commit()

        wrapped = _ForceNoRowOnFirstSelect(conn)
        row = main_module._get_or_create_progress(wrapped, "stu_race")

        assert row is not None
        assert row["stage"] == "learning"
        assert row["case_order_index"] == 0

        # Exactly one row exists -- the fix didn't create a duplicate or
        # leave the table in an inconsistent state.
        count = conn.execute("SELECT COUNT(*) FROM progress WHERE student_id = ?", ("stu_race",)).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_submit_rolls_back_the_submission_if_advance_progress_fails(client, mint_token, monkeypatch):
    """issue #60: the submission INSERT and _advance_progress()'s own
    UPDATE(s) must land as one transaction. Forces _advance_progress() to
    raise -- simulating the realistic real-world trigger described in the
    issue's own comment thread (SQLite writer-lock contention under
    concurrent submissions near a shared stage deadline, since
    db.get_connection() sets no PRAGMA busy_timeout) -- and confirms
    neither the submission nor the progress advance was persisted. Then
    confirms a plain retry of the exact same submission succeeds cleanly:
    that's the actual bug this closes -- before this fix, the retry would
    hit UNIQUE(student_id, case_id, stage) and return 409 forever, with no
    way for the student to move on."""
    token = mint_token("stu_atomic")
    case_id = _case_id(client, token)

    real_advance_progress = main_module._advance_progress

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main_module, "_advance_progress", _boom)

    # The `client` fixture's TestClient uses raise_server_exceptions=True
    # (Starlette's own default) -- confirmed with an isolated repro that
    # this re-raises the original exception even though the app's own
    # registered Exception handler (unhandled_exception_handler) already
    # produced a correct 500 response; that handler's own behavior is
    # unaffected and unrelated to this fix, not something broken by it.
    with pytest.raises(sqlite3.OperationalError):
        client.post(
            "/submit",
            json={
                "token": token,
                "case_id": case_id,
                "stage": "learning",
                "text": "first attempt",
                "time_spent_seconds": 1,
            },
        )

    conn = db_module.get_connection()
    try:
        submission_count = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE student_id = ? AND case_id = ? AND stage = ?",
            ("stu_atomic", case_id, "learning"),
        ).fetchone()[0]
        assert submission_count == 0, "the submission must not survive a failed _advance_progress()"

        progress = conn.execute(
            "SELECT stage, case_order_index FROM progress WHERE student_id = ?", ("stu_atomic",)
        ).fetchone()
        assert progress["stage"] == "learning"
        assert progress["case_order_index"] == 0
    finally:
        conn.close()

    # Restore the real _advance_progress before retrying -- this is the
    # part that actually proves the fix: before it, this exact retry would
    # 409 forever (UNIQUE(student_id, case_id, stage) already satisfied by
    # a submission that should never have survived the rollback above).
    #
    # A second explicit setattr, not monkeypatch.undo(): undo() reverts
    # *every* patch made via this test's monkeypatch fixture, including
    # the `client` fixture's own db_module.DB_PATH patch (same shared
    # fixture instance) -- confirmed the hard way, undo() here broke the
    # retry below with "unable to open database file" the first time this
    # test was written.
    monkeypatch.setattr(main_module, "_advance_progress", real_advance_progress)

    retry = client.post(
        "/submit",
        json={"token": token, "case_id": case_id, "stage": "learning", "text": "retry", "time_spent_seconds": 1},
    )
    assert retry.status_code == 200, (
        "a retry after the rolled-back failure above must succeed cleanly, "
        "not 409 DUPLICATE_SUBMISSION -- that stuck-forever state is "
        "exactly the bug issue #60 reports"
    )
    assert client.get("/case", headers={"X-Grading-Token": token}).json()["stage"] == "assessment"
