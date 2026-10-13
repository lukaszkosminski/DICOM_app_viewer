"""
Tests for the stable error taxonomy (issue #74): every error response,
from every endpoint, has the same {"error_code": ..., "message": ...}
shape, and the two catch-all handlers (validation errors, truly
unanticipated exceptions) never leak implementation details to the
client -- this is also where issue #62 (the /healthz exception-text
leak) gets its regression coverage, generalized to any endpoint, not
just /healthz specifically.
"""

import sqlite3

from fastapi.testclient import TestClient

from app import db as db_module
from app import main as main_module
from app.main import app


def _assert_error_shape(resp, status_code, error_code):
    assert resp.status_code == status_code, resp.text
    data = resp.json()
    assert set(data.keys()) == {"error_code", "message"}
    assert data["error_code"] == error_code
    assert isinstance(data["message"], str) and data["message"]


def test_invalid_coordinator_key_has_stable_error_shape(client):
    resp = client.post(
        "/session",
        json={"student_id": "stu_1", "session_id": "sess_1"},
        headers={"X-Coordinator-Key": "definitely-not-the-real-key"},
    )
    _assert_error_shape(resp, 401, "AUTH_INVALID_COORDINATOR_KEY")


def test_unknown_token_has_stable_error_shape(client):
    resp = client.get("/case", headers={"X-Grading-Token": "this-token-was-never-minted"})
    _assert_error_shape(resp, 401, "AUTH_INVALID_TOKEN")


def test_expired_token_has_stable_error_shape(client, mint_token):
    token = mint_token("stu_expired")
    conn = db_module.get_connection()
    try:
        conn.execute("UPDATE sessions SET expires_at = 0 WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()

    resp = client.get("/case", headers={"X-Grading-Token": token})
    _assert_error_shape(resp, 401, "AUTH_TOKEN_EXPIRED")


def test_submit_stage_mismatch_has_stable_error_shape(client, mint_token):
    token = mint_token("stu_stage_mismatch")
    case_id = client.get("/case", headers={"X-Grading-Token": token}).json()["case_id"]
    resp = client.post(
        "/submit",
        json={"token": token, "case_id": case_id, "stage": "assessment", "category": "3", "modifier_s": False},
    )
    _assert_error_shape(resp, 409, "VALIDATION_STAGE_MISMATCH")


def test_submit_case_mismatch_has_stable_error_shape(client, mint_token):
    """issue #61: case_id is now always resolved against progress's own
    position, never looked up by the client-supplied id directly -- any
    mismatch (wrong stage entirely, or right stage but wrong position)
    is the same 409 VALIDATION_CASE_MISMATCH now, not a separate 400."""
    token = mint_token("stu_case_mismatch")
    # case_id 2 is the seeded assessment-stage case, not learning's.
    resp = client.post(
        "/submit",
        json={"token": token, "case_id": 2, "stage": "learning", "text": "x"},
    )
    _assert_error_shape(resp, 409, "VALIDATION_CASE_MISMATCH")


def test_duplicate_submission_has_stable_error_shape(client, mint_token):
    """Same technique as test_state_machine.py's own
    test_submit_duplicate_for_same_case_stage_returns_409: a normal
    sequential double-submit doesn't hit this path at all (the first
    submission already advances progress past that case, so a second
    identical one hits VALIDATION_STAGE_MISMATCH instead) -- this only
    fires for the real race issue #28 guards against, reproduced here by
    inserting the "other request already won" row directly."""
    token = mint_token("stu_duplicate")
    case_id = client.get("/case", headers={"X-Grading-Token": token}).json()["case_id"]

    conn = db_module.get_connection()
    try:
        conn.execute(
            """INSERT INTO submissions
               (student_id, case_id, stage, submitted_category, submitted_modifier_s,
                submitted_text, is_correct, time_spent_seconds, submitted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("stu_duplicate", case_id, "learning", None, None, "already submitted", None, 1, db_module.now()),
        )
        conn.commit()
    finally:
        conn.close()

    resp = client.post(
        "/submit",
        json={"token": token, "case_id": case_id, "stage": "learning", "text": "x"},
    )
    _assert_error_shape(resp, 409, "DUPLICATE_SUBMISSION")


def test_reset_during_test_stage_has_stable_error_shape(client, mint_token, reset_on):
    token = mint_token("stu_reset_blocked")
    # Walk through learning + assessment to reach the test stage.
    for _ in range(2):
        data = client.get("/case", headers={"X-Grading-Token": token}).json()
        body = {"token": token, "case_id": data["case_id"], "stage": data["stage"]}
        if data["stage"] == "learning":
            body["text"] = "x"
        else:
            body["category"] = "2"
            body["modifier_s"] = False
        client.post("/submit", json=body)

    resp = client.post("/reset", json={"token": token})
    _assert_error_shape(resp, 403, "RESET_BLOCKED_DURING_TEST")


def test_oversized_field_gives_generic_validation_error_without_echoing_input(client, mint_token):
    """issue #74's own reasoning: pydantic's raw validation errors can
    include the actual rejected value -- for a max_length violation that
    means potentially echoing a huge payload straight back. The client
    should get a small, generic body regardless of how large the
    rejected input was."""
    token = mint_token("stu_oversized")
    case_id = client.get("/case", headers={"X-Grading-Token": token}).json()["case_id"]
    huge_marker = "X" * 10_001
    resp = client.post(
        "/submit",
        json={"token": token, "case_id": case_id, "stage": "learning", "text": huge_marker},
    )
    assert resp.status_code == 422
    data = resp.json()
    assert data == {"error_code": "VALIDATION_ERROR", "message": "Invalid request"}
    assert huge_marker not in resp.text


def test_healthz_database_error_never_leaks_exception_text(client, monkeypatch):
    """issue #62's actual regression test: the exception text (which can
    include sqlite file paths/internals) must never reach the client --
    only a generic message, with the real detail logged server-side
    instead (see test_logging.py's own coverage of that half)."""
    sensitive_detail = "unable to open database file: /a/sensitive/internal/path/grading.db"

    def _boom():
        raise sqlite3.OperationalError(sensitive_detail)

    monkeypatch.setattr(main_module.db, "get_connection", _boom)

    resp = client.get("/healthz")
    _assert_error_shape(resp, 503, "DATABASE_UNAVAILABLE")
    assert sensitive_detail not in resp.text


def test_truly_unanticipated_exception_gets_generic_server_error(client, mint_token, monkeypatch):
    """The catch-all handler: even a bug this taxonomy never anticipated
    (a plain RuntimeError, not a deliberate AppError) still gets the same
    stable shape and never leaks the real exception message.

    Uses its own TestClient with raise_server_exceptions=False -- the
    shared `client` fixture's default (True) is deliberately test-
    friendly noise-surfacing for *other* tests (an unexpected bug should
    fail the test loudly, not hide behind a formatted response), but
    that's exactly the behavior this one test needs to disable to
    observe what a real client actually receives."""
    token = mint_token("stu_unexpected_bug")
    sensitive_detail = "some internal implementation detail that must never reach a client"

    def _boom(conn, student_id):
        raise RuntimeError(sensitive_detail)

    monkeypatch.setattr(main_module, "_get_or_create_progress", _boom)

    with TestClient(app, raise_server_exceptions=False) as non_raising_client:
        resp = non_raising_client.get("/case", headers={"X-Grading-Token": token})
    _assert_error_shape(resp, 500, "SERVER_ERROR")
    assert sensitive_detail not in resp.text
