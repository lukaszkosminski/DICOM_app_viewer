"""scripts/export-results.py: the coordinator's CSV and summary of the doctors' answers.

The database is built with the REAL grading-api schema and walked through with the REAL API (TestClient),
so a column rename in the app breaks these tests instead of silently breaking the export."""

import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "docker" / "grading-api"))
os.environ.setdefault("GRADING_COORDINATOR_KEY", "k" * 40)

from fastapi.testclient import TestClient  # noqa: E402

from app import db as db_module  # noqa: E402
from app.main import app  # noqa: E402

spec = importlib.util.spec_from_file_location("export_results", ROOT / "scripts" / "export-results.py")
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)

KEY = os.environ["GRADING_COORDINATOR_KEY"]
H = "X-Grading-Token"


@pytest.fixture
def exam_db(tmp_path, monkeypatch):
    """A database with 3 learning, 2 assessment and 2 test cases, and these doctors:
    DEMO_A  answered everything right, approved the review
    DEMO_B  finished, one wrong test answer (4A given for 4B), not approved
    DEMO_C  only the learning stage so far
    STU_X   another group, finished (must be left out by the default prefix)"""
    path = str(tmp_path / "grading.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    with TestClient(app) as client:
        conn = db_module.get_connection()
        conn.execute("UPDATE cases SET title = '=HYPERLINK(\"http://evil\")' WHERE stage = 'learning'")
        extra = [("learning", 1, "Uczenie 2", "2"), ("learning", 2, "Uczenie 3", "3"), ("assessment", 1, "Ocena 2", "4A"), ("test", 1, "Test 2", "4B")]
        for stage, order, title, gt in extra:
            conn.execute(
                "INSERT INTO cases (stage, order_index, orthanc_study_uid, title, ground_truth_category, ground_truth_modifier_s, reference_report, version)"
                " VALUES (?, ?, ?, ?, ?, 0, 'raport', 1)",
                (stage, order, f"1.2.{order}", title, gt),
            )
        conn.commit()
        conn.close()

        def mint(student):
            r = client.post("/session", json={"student_id": student, "session_id": "1"}, headers={"X-Coordinator-Key": KEY})
            return r.json()["token"]

        def walk(student, wrong_test=False, stop_after_learning=False, approve=False):
            token = mint(student)
            while True:
                case = client.get("/case", headers={H: token}).json()
                if case.get("complete"):
                    break
                if case["stage"] == "learning":
                    body = {"text": f"opis {student}"}
                else:
                    truth = {r[0]: r[1] for r in db_module.get_connection().execute("SELECT id, ground_truth_category FROM cases")}[case["case_id"]]
                    category = truth
                    if wrong_test and case["stage"] == "test" and truth == "4B":
                        category = "4A"
                    body = {"category": category, "modifier_s": False}
                r = client.post("/submit", json={"token": token, "case_id": case["case_id"], "stage": case["stage"], **body})
                assert r.status_code == 200, r.text
                if stop_after_learning and case["stage"] == "learning" and case["case_id"] and _learning_done(client, token):
                    return
            if approve:
                assert client.post("/finalize", json={"token": token}).status_code == 200

        def _learning_done(c, t):
            return c.get("/case", headers={H: t}).json().get("stage") != "learning"

        walk("DEMO_A", approve=True)
        walk("DEMO_B", wrong_test=True)
        walk("DEMO_C", stop_after_learning=True)
        walk("STU_X")
        conn = db_module.get_connection()
        conn.execute("UPDATE submissions SET time_spent_seconds = 60 WHERE student_id = 'DEMO_A'")
        conn.execute("UPDATE submissions SET time_spent_seconds = 120 WHERE student_id = 'DEMO_B'")
        conn.commit()
        conn.close()
    return path


def run(db, tmp_path, *extra):
    out = tmp_path / "out"
    code = export.main(["--db", db, "--out-dir", str(out), *extra])
    return code, out


def read_csv(out):
    with open(out / "attempts.csv", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_the_csv_has_one_row_per_answer_for_the_demo_doctors_only(exam_db, tmp_path):
    code, out = run(exam_db, tmp_path)
    assert code == 0
    rows = read_csv(out)
    assert {r["student_id"] for r in rows} == {"DEMO_A", "DEMO_B", "DEMO_C"}
    by = {s: [r for r in rows if r["student_id"] == s] for s in ("DEMO_A", "DEMO_B", "DEMO_C")}
    assert len(by["DEMO_A"]) == 7 and len(by["DEMO_B"]) == 7 and len(by["DEMO_C"]) == 3
    assert list(rows[0].keys()) == export.COLUMNS
    # ordered: doctor, then learning -> assessment -> test, then case position
    a = by["DEMO_A"]
    assert [r["stage"] for r in a] == ["learning"] * 3 + ["assessment"] * 2 + ["test"] * 2
    assert [r["position"] for r in a[3:5]] == ["1", "2"]


def test_all_students_can_be_requested_explicitly(exam_db, tmp_path):
    _, out = run(exam_db, tmp_path, "--all")
    assert "STU_X" in {r["student_id"] for r in read_csv(out)}
    _, out2 = run(exam_db, tmp_path / "p", "--student-prefix", "STU_")
    assert {r["student_id"] for r in read_csv(out2)} == {"STU_X"}


def test_verdicts_truth_and_status_columns(exam_db, tmp_path):
    _, out = run(exam_db, tmp_path)
    rows = read_csv(out)
    wrong = [r for r in rows if r["student_id"] == "DEMO_B" and r["correct"] == "0"]
    assert len(wrong) == 1 and wrong[0]["stage"] == "test"
    assert (wrong[0]["ground_truth_category"], wrong[0]["submitted_category"]) == ("4B", "4A")
    assert all(r["correct"] == "" for r in rows if r["stage"] == "learning")  # not graded
    a = [r for r in rows if r["student_id"] == "DEMO_A"]
    assert all(r["doctor_finished"] == "1" and r["review_approved"] == "1" for r in a)
    b = [r for r in rows if r["student_id"] == "DEMO_B"]
    assert all(r["doctor_finished"] == "1" and r["review_approved"] == "0" for r in b)
    c = [r for r in rows if r["student_id"] == "DEMO_C"]
    assert all(r["doctor_finished"] == "0" for r in c)
    assert all(r["submitted_at_utc"].endswith("Z") for r in rows)


def test_the_summary_numbers(exam_db, tmp_path, capsys):
    code, out = run(exam_db, tmp_path)
    text = (out / "summary.txt").read_text()
    assert capsys.readouterr().out.startswith("Exam results summary")  # also printed
    assert "doctors started: 3" in text
    assert "finished all stages: 2 (67%)" in text and "approved the final review: 1 (33%)" in text
    # per doctor
    line = {l.split()[0]: l for l in text.splitlines() if l.strip().startswith("DEMO_")}
    assert "2/2" in line["DEMO_A"] and "100%" in line["DEMO_A"] and "1:00" in line["DEMO_A"]
    assert "1/2" in line["DEMO_B"] and "50%" in line["DEMO_B"] and "2:00" in line["DEMO_B"]
    assert "n/a" in line["DEMO_C"]  # nothing graded yet: no division by zero
    # confusion: 8 graded answers, 7 correct; the single mistake is truth 4B answered 4A
    assert "graded answers: 8, correct 7 = 88%" in text
    matrix = {l.split()[0]: l.split()[1:] for l in text.splitlines() if l.startswith("   ") and l.split() and l.split()[0] in export.CATEGORIES}
    assert matrix["4B"] and "1" in matrix["4B"]
    assert set(matrix) == {"3", "4A", "4B"}  # rows are the TRUE categories that exist, nothing else
    # the walk-through always answers "no S"; the seeded test ground truth has S, so 6 of 8 agree
    assert "modifier S agrees with the ground truth in 6/8 graded answers (75%)" in text
    assert "0:00" in line["DEMO_C"] and line["DEMO_C"].split()[-2] != "n/a"  # zero seconds is a time, not missing data
    assert "Per case" in text and "Test 2" in text


def test_a_spreadsheet_formula_in_a_cell_is_neutralised(exam_db, tmp_path):
    _, out = run(exam_db, tmp_path)
    titles = {r["case_title"] for r in read_csv(out) if r["stage"] == "learning"}
    assert "'=HYPERLINK(\"http://evil\")" in titles
    assert not any(t.startswith("=") for t in titles)


def test_free_text_is_left_out_unless_asked(exam_db, tmp_path):
    _, out = run(exam_db, tmp_path)
    assert "learning_text" not in read_csv(out)[0]
    assert "opis DEMO_A" not in (out / "attempts.csv").read_text()
    _, out2 = run(exam_db, tmp_path / "t", "--include-text")
    rows = read_csv(out2)
    assert any(r["learning_text"] == "opis DEMO_A" for r in rows)
    assert all(r["learning_text"] == "" for r in rows if r["stage"] != "learning")


def test_files_are_private_nothing_is_overwritten_and_the_database_is_untouched(exam_db, tmp_path, capsys):
    before = hashlib.sha256(Path(exam_db).read_bytes()).hexdigest()
    _, out = run(exam_db, tmp_path)
    assert oct(out.stat().st_mode & 0o777) == "0o700"
    for name in ("attempts.csv", "summary.txt"):
        assert oct((out / name).stat().st_mode & 0o777) == "0o600"
    assert export.main(["--db", exam_db, "--out-dir", str(out)]) == 1  # second run into the same directory
    assert "nothing is overwritten" in capsys.readouterr().err
    assert hashlib.sha256(Path(exam_db).read_bytes()).hexdigest() == before  # read-only


def test_a_database_from_before_the_review_feature_still_exports(exam_db, tmp_path):
    import sqlite3

    c = sqlite3.connect(exam_db)
    c.execute("ALTER TABLE progress DROP COLUMN finalized_at")
    c.commit()
    c.close()
    code, out = run(exam_db, tmp_path)
    assert code == 0 and all(r["review_approved"] == "0" for r in read_csv(out))


def test_an_empty_database_gives_a_readable_summary_not_a_crash(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "empty.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    db_module.init_db()
    code, out = run(path, tmp_path)
    text = (out / "summary.txt").read_text()
    assert code == 0 and "doctors started: 0" in text and "(no doctors)" in text and "(no graded answers yet)" in text
    assert read_csv(out) == []


def test_unreadable_input_is_reported_clearly(tmp_path, capsys):
    assert export.main(["--db", str(tmp_path / "missing.db"), "--out-dir", str(tmp_path / "o")]) == 1
    assert "cannot open the database" in capsys.readouterr().err


# ---- reading from the running container ----


def fake_runner(stdout="", stderr="", code=0):
    calls = []

    def runner(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)

    runner.calls = calls
    return runner


def test_the_container_is_queried_read_only_through_docker_exec():
    payload = {"submissions": [], "progress": []}
    r = fake_runner(stdout=json.dumps(payload))
    assert export.read_container("ipcmc-grading-api", r) == payload
    argv, kw = r.calls[0]
    assert argv == ["docker", "exec", "-i", "ipcmc-grading-api", "python", "-"]
    assert "mode=ro" in kw["input"] and "INSERT" not in kw["input"].upper() and "UPDATE" not in kw["input"].upper()


def test_container_problems_say_what_to_do():
    with pytest.raises(export.ExportError, match="docker compose up"):
        export.read_container("x", fake_runner(stderr="Error: No such container: x", code=1))
    with pytest.raises(export.ExportError, match="not JSON"):
        export.read_container("x", fake_runner(stdout="garbage"))


def test_the_snippet_that_runs_in_the_container_works_against_a_real_file(exam_db):
    """The exact code sent to the container, executed locally against the test database."""
    code = f"QUERY = {export.QUERY_SUBMISSIONS!r}\n{export.CONTAINER_SNIPPET}"
    res = subprocess.run(
        [sys.executable, "-"], input=code, capture_output=True, text=True, env={**os.environ, "GRADING_DB_PATH": exam_db}, check=False
    )
    assert res.returncode == 0, res.stderr
    raw = json.loads(res.stdout)
    assert len(raw["submissions"]) == 7 * 3 + 3 and {p["student_id"] for p in raw["progress"]} >= {"DEMO_A", "STU_X"}
