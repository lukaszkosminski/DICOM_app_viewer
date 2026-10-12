"""
End-of-exam review and approval (issue #202).

The radiologist's request (modelled on Google Forms): at the end of the exam the doctor sees a
summary of their own answers; only after approving does the app show the correct answers.
This is a rule of the exam, so the SERVER enforces it:
  GET  /review    own answers of all stages, read-only, NO ground truth / report / verdict
  POST /finalize  one-way, idempotent approval; 409 until the exam is complete
  GET  /results   reveals ground truth, reference reports and the score only after /finalize
"""

import glob
import json
import sqlite3

from app import db as db_module

H = "X-Grading-Token"
ANSWERS = (
    ("learning", {"text": "Moja ocena: guzek w prawym plucu"}),
    ("assessment", {"category": "3", "modifier_s": False}),
    ("test", {"category": "2", "modifier_s": True}),  # deliberately wrong (the seeded truth is 4A)
)


def _walk_to_complete(client, token):
    for expected_stage, body in ANSWERS:
        case = client.get("/case", headers={H: token}).json()
        assert case["complete"] is False and case["stage"] == expected_stage
        resp = client.post("/submit", json={"token": token, "case_id": case["case_id"], "stage": case["stage"], **body})
        assert resp.status_code == 200, resp.text


def _finalize(client, token):
    return client.post("/finalize", json={"token": token})


# ---- before the exam is complete ----


def test_review_and_results_before_completion_say_not_complete(client, mint_token):
    token = mint_token("stu_rev_early")
    assert client.get("/review", headers={H: token}).json() == {"complete": False}
    assert client.get("/results", headers={H: token}).json() == {"complete": False}


def test_finalize_before_completion_is_409_and_changes_nothing(client, mint_token):
    token = mint_token("stu_fin_early")
    resp = _finalize(client, token)
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "FINALIZE_NOT_COMPLETE"
    _walk_to_complete(client, token)
    assert client.get("/review", headers={H: token}).json()["finalized"] is False


# ---- the review: own answers only ----


def test_review_lists_all_answers_in_order_and_reveals_nothing(client, mint_token):
    token = mint_token("stu_review")
    _walk_to_complete(client, token)
    data = client.get("/review", headers={H: token}).json()
    assert data["complete"] is True and data["finalized"] is False
    assert [(i["stage"], i["position"], i["total_in_stage"]) for i in data["items"]] == [
        ("learning", 1, 1),
        ("assessment", 1, 1),
        ("test", 1, 1),
    ]
    assert data["items"][0]["answer"] == {"text": ANSWERS[0][1]["text"]}
    assert data["items"][1]["answer"] == {"category": "3", "modifier_s": False}
    assert data["items"][2]["answer"] == {"category": "2", "modifier_s": True}
    # nothing about the truth: no key and no seeded reference text anywhere in the response
    dump = json.dumps(data, ensure_ascii=False)
    for forbidden in ("ground_truth", "reference_report", "correct", "PLACEHOLDER"):
        assert forbidden not in dump, forbidden


def test_results_before_approval_reveal_nothing(client, mint_token):
    token = mint_token("stu_noreveal")
    _walk_to_complete(client, token)
    data = client.get("/results", headers={H: token}).json()
    assert data == {"complete": True, "finalized": False}


# ---- approval ----


def test_finalize_is_idempotent_and_keeps_the_first_timestamp(client, mint_token):
    token = mint_token("stu_idem")
    _walk_to_complete(client, token)
    first = _finalize(client, token)
    assert first.status_code == 200 and first.json() == {"finalized": True, "already_finalized": False}

    def stamp():
        conn = db_module.get_connection()
        try:
            return conn.execute("SELECT finalized_at FROM progress WHERE student_id = 'stu_idem'").fetchone()[0]
        finally:
            conn.close()

    t1 = stamp()
    assert t1 is not None
    second = _finalize(client, token)
    assert second.json() == {"finalized": True, "already_finalized": True}
    assert stamp() == t1
    assert client.get("/review", headers={H: token}).json()["finalized"] is True


def test_results_after_approval_reveal_everything_per_case(client, mint_token):
    token = mint_token("stu_reveal")
    _walk_to_complete(client, token)
    _finalize(client, token)
    data = client.get("/results", headers={H: token}).json()
    assert data["finalized"] is True
    assert data["test_total"] == 1 and data["test_correct"] == 0 and data["accuracy"] == 0.0
    learning, assessment, test = data["items"]
    assert learning["stage"] == "learning" and learning["correct"] is None
    assert learning["reference_report"] and learning["answer"] == {"text": ANSWERS[0][1]["text"]}
    assert assessment["correct"] is True  # category 3 is the seeded truth
    assert assessment["ground_truth_category"] == "3"
    assert test["correct"] is False
    assert test["ground_truth_category"] == "4A" and test["ground_truth_modifier_s"] is True
    assert test["answer"] == {"category": "2", "modifier_s": True}
    assert all(i["reference_report"] and i["title"] for i in data["items"])


def test_approval_belongs_to_one_student(client, mint_token):
    a, b = mint_token("stu_a"), mint_token("stu_b")
    _walk_to_complete(client, a)
    _walk_to_complete(client, b)
    _finalize(client, a)
    assert client.get("/results", headers={H: a}).json()["finalized"] is True
    assert client.get("/results", headers={H: b}).json() == {"complete": True, "finalized": False}


def test_reset_clears_the_approval_so_a_new_run_starts_unapproved(client, mint_token):
    token = mint_token("stu_reset")
    _walk_to_complete(client, token)
    _finalize(client, token)
    assert client.post("/reset", json={"token": token}).status_code == 200
    assert client.get("/review", headers={H: token}).json() == {"complete": False}
    _walk_to_complete(client, token)
    assert client.get("/results", headers={H: token}).json() == {"complete": True, "finalized": False}


# ---- authentication ----


def test_review_and_finalize_need_a_valid_token(client):
    assert client.get("/review").status_code == 401
    assert client.get("/review", headers={H: "nope"}).status_code == 401
    assert client.post("/finalize", json={"token": "nope"}).status_code == 401
    # query-string tokens stay rejected (issue #94)
    assert client.get("/review?token=x").status_code == 401


# ---- migration ----


def test_migration_adds_the_column_after_a_backup_and_keeps_existing_rows(tmp_path, monkeypatch):
    path = str(tmp_path / "grading-pre202.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    db_module.init_db()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO progress (student_id, stage, case_order_index, case_assigned_at) "
        "VALUES ('stu_old', 'complete', 0, 1.0)"
    )
    conn.execute("ALTER TABLE progress DROP COLUMN finalized_at")  # the database as it was before #202
    conn.commit()
    conn.close()

    db_module.init_db()

    assert len(glob.glob(f"{path}.pre-migration-*.sqlite.bak")) == 1  # real data is copied first (issue #85)
    conn = sqlite3.connect(path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(progress)")}
        row = conn.execute("SELECT stage, finalized_at FROM progress WHERE student_id = 'stu_old'").fetchone()
    finally:
        conn.close()
    assert "finalized_at" in cols
    assert row == ("complete", None)  # untouched, simply not approved yet
