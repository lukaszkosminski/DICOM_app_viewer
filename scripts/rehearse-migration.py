#!/usr/bin/env python3
"""Rehearse grading-api's schema migration (issue #96) on a COPY of a database.

Never touches the source: the file is copied with SQLite's online backup API
(consistent even for a live WAL database), and every check runs on the copy,
which is deleted afterwards unless --keep is given.

Usage:
    python3 scripts/rehearse-migration.py --db /path/to/grading.db
    python3 scripts/rehearse-migration.py --synthetic        # built-in legacy fixture

Exit code 0 = every check passed; 1 = at least one failed (the report says which).
Run this on a copy of the real database BEFORE any production rollout; it is
the evidence the rollout sign-off should attach. It does not replace the
backup procedure from issue #85.
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "docker" / "grading-api"))

# The pre-#96 schema, as shipped at master 6dab554 (db.py init_db): cases without
# `version`, submissions without the ground-truth snapshot columns.
LEGACY_SCHEMA = """
CREATE TABLE cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT NOT NULL, order_index INTEGER NOT NULL,
    orthanc_study_uid TEXT NOT NULL, title TEXT NOT NULL, ground_truth_category TEXT NOT NULL,
    ground_truth_modifier_s INTEGER NOT NULL DEFAULT 0, reference_report TEXT NOT NULL,
    UNIQUE(stage, order_index));
CREATE TABLE progress (
    student_id TEXT PRIMARY KEY, stage TEXT NOT NULL, case_order_index INTEGER NOT NULL,
    case_assigned_at REAL NOT NULL);
CREATE TABLE submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, student_id TEXT NOT NULL,
    case_id INTEGER NOT NULL REFERENCES cases(id), stage TEXT NOT NULL,
    submitted_category TEXT, submitted_modifier_s INTEGER, submitted_text TEXT,
    is_correct INTEGER, time_spent_seconds REAL, submitted_at REAL NOT NULL,
    UNIQUE(student_id, case_id, stage));
CREATE TABLE sessions (
    token TEXT PRIMARY KEY, student_id TEXT NOT NULL, session_id TEXT NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE INDEX idx_submissions_student ON submissions(student_id);
CREATE INDEX idx_cases_stage ON cases(stage);
"""


def build_synthetic(path: str) -> None:
    c = sqlite3.connect(path)
    c.executescript(LEGACY_SCHEMA)
    c.executemany(
        "INSERT INTO cases (stage, order_index, orthanc_study_uid, title, ground_truth_category,"
        " ground_truth_modifier_s, reference_report) VALUES (?,?,?,?,?,?,?)",
        [
            ("learning", 0, "uid-l0", "L0", "2", 0, "r"),
            ("assessment", 0, "uid-a0", "A0", "3", 0, "r"),
            ("test", 0, "uid-t0", "T0", "4A", 1, "r"),
        ],
    )
    for n in range(1, 6):
        sid = f"stu{n}"
        c.execute("INSERT INTO progress VALUES (?, 'complete', 0, 1.0)", (sid,))
        c.execute("INSERT INTO sessions VALUES (?, ?, 's', 1.0, 9e9)", (f"tok{n}", sid))
        for case_id, stage, ans in (
            (1, "learning", "2"),
            (2, "assessment", "3"),
            (3, "test", "4A" if n % 2 else "4B"),
        ):
            correct = 1 if ans == {1: "2", 2: "3", 3: "4A"}[case_id] else 0
            c.execute(
                "INSERT INTO submissions (student_id, case_id, stage, submitted_category,"
                " submitted_modifier_s, submitted_text, is_correct, time_spent_seconds, submitted_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, case_id, stage, ans, 0, "txt", correct, 1.5, float(n)),
            )
    c.commit()
    c.close()


def online_copy(src: str, dst: str) -> None:
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    s.backup(d)
    d.close()
    s.close()


def snapshot(path: str) -> dict:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    tables = {
        r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    counts = {
        t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        for t in sorted(tables)
    }
    sub = []
    if "submissions" in tables:
        sub = [
            tuple(r)
            for r in c.execute(
                "SELECT id, student_id, case_id, stage, submitted_category, submitted_modifier_s,"
                " submitted_text, is_correct, time_spent_seconds, submitted_at FROM submissions ORDER BY id"
            )
        ]
    c.close()
    return {"counts": counts, "submissions": sub}


class Report:
    def __init__(self):
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(
            f"{'PASS' if ok else 'FAIL'}: {name}"
            + (f" -- {detail}" if detail and not ok else "")
        )
        if not ok:
            self.failed += 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--db", help="path to a grading.db (read-only; a copy is migrated)")
    g.add_argument(
        "--synthetic",
        action="store_true",
        help="use the built-in pre-#96 legacy fixture",
    )
    ap.add_argument(
        "--keep", action="store_true", help="keep the migrated copy and print its path"
    )
    args = ap.parse_args()

    work = tempfile.mkdtemp(prefix="rehearse-migration-")
    copy = os.path.join(work, "copy.db")
    try:
        if args.synthetic:
            build_synthetic(copy)
        else:
            if not os.path.isfile(args.db):
                print(f"FAIL: {args.db} does not exist")
                return 1
            online_copy(args.db, copy)

        os.environ["GRADING_DB_PATH"] = copy
        os.environ.setdefault(
            "GRADING_COORDINATOR_KEY", "rehearsal-only-key-" + "x" * 32
        )
        from app import db  # imported only now: DB_PATH is read at import time

        r = Report()
        before = snapshot(copy)
        pc = sqlite3.connect(copy)
        r.check(
            "source copy passes integrity_check",
            pc.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
        )
        pc.close()

        db.init_db()  # the migration under test (SystemExit on failure = FAIL)
        after = snapshot(copy)

        c = sqlite3.connect(copy)
        c.execute("PRAGMA foreign_keys=ON")
        r.check(
            "integrity_check ok after migration",
            c.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
        )
        fk = c.execute("PRAGMA foreign_key_check").fetchall()
        r.check("foreign_key_check is empty", not fk, f"{len(fk)} violation(s)")
        # \w+_old as a whole identifier (case-sensitive): a bare LIKE '%_old%' would also match
        # the trigger's own `OLD.id` pseudo-row.
        bad_refs = [
            row[0]
            for row in c.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('table','trigger','index','view')"
            )
            if row[1] and re.search(r"\b\w+_old\b", row[1])
        ]
        r.check(
            "no schema object references a *_old table",
            not bad_refs,
            ", ".join(bad_refs),
        )
        # issue #202: progress.finalized_at is added, nullable, and no existing student is
        # marked approved (a doctor who had finished before simply has not approved yet)
        prog_cols = {row[1] for row in c.execute("PRAGMA table_info(progress)")}
        r.check(
            "progress has the finalized_at column (issue #202)",
            "finalized_at" in prog_cols,
        )
        if "finalized_at" in prog_cols:
            approved = c.execute(
                "SELECT COUNT(*) FROM progress WHERE finalized_at IS NOT NULL"
            ).fetchone()[0]
            r.check(
                "no existing student is marked approved by the migration",
                approved == 0,
                f"{approved} row(s)",
            )
        r.check(
            "no leftover *_old tables",
            not [t for t in after["counts"] if t.endswith("_old")],
            str([t for t in after["counts"] if t.endswith("_old")]),
        )
        for t in ("cases", "progress", "sessions"):
            if t in before["counts"]:
                r.check(
                    f"row count preserved: {t}",
                    after["counts"].get(t) == before["counts"][t],
                    f"{before['counts'][t]} -> {after['counts'].get(t)}",
                )
        pre_sub = {s[0]: s for s in before["submissions"]}
        post_sub = {s[0]: s for s in after["submissions"]}
        # a legacy db with pre-constraint duplicates legitimately loses the older ones (db.py)
        lost = [i for i in pre_sub if i not in post_sub]
        r.check(
            "no submission lost (except documented duplicate collapse)",
            not lost or _only_duplicates(before, lost),
            str(lost),
        )
        changed = [i for i in post_sub if i in pre_sub and post_sub[i] != pre_sub[i]]
        r.check(
            "legacy submission columns byte-identical",
            not changed,
            f"ids {changed[:5]}",
        )

        cols = {row[1] for row in c.execute("PRAGMA table_info(submissions)")}
        need = {
            "ground_truth_category",
            "ground_truth_modifier_s",
            "case_version",
            "gt_backfilled",
        }
        r.check("submissions has snapshot columns", need <= cols, str(need - cols))
        n_unflagged = c.execute(
            "SELECT COUNT(*) FROM submissions WHERE ground_truth_category IS NULL"
        ).fetchone()[0]
        r.check(
            "every migrated submission has a frozen ground truth",
            n_unflagged == 0,
            f"{n_unflagged} NULL",
        )
        n_back = c.execute(
            "SELECT COUNT(*) FROM submissions WHERE gt_backfilled=1"
        ).fetchone()[0]
        print(
            f"INFO: {n_back} submission(s) backfilled from CURRENT cases (gt_backfilled=1): analysis-time caveat"
        )
        r.check(
            "trigger cases_ground_truth_frozen exists",
            c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='cases_ground_truth_frozen'"
            ).fetchone()
            is not None,
        )

        # The probe that would have caught the "cases_old" FK bug: write through the FK.
        probe_ok, err = True, ""
        try:
            case_id = c.execute("SELECT id FROM cases LIMIT 1").fetchone()[0]
            c.execute("SAVEPOINT probe")
            c.execute(
                "INSERT INTO submissions (student_id, case_id, stage, submitted_at) VALUES ('__probe__', ?, 'learning', 0)",
                (case_id,),
            )
        except sqlite3.Error as exc:
            probe_ok, err = False, str(exc)
        finally:
            try:
                c.execute("ROLLBACK TO probe")
                c.execute("RELEASE probe")
            except sqlite3.Error:
                pass
        r.check(
            "INSERT into submissions works after migration (FK probe)", probe_ok, err
        )

        trig_ok, terr = False, "no submission to test against"
        row = c.execute("SELECT case_id FROM submissions LIMIT 1").fetchone()
        if row:
            try:
                c.execute("SAVEPOINT trig")
                c.execute(
                    "UPDATE cases SET ground_truth_category='ZZ' WHERE id=?", (row[0],)
                )
                trig_ok, terr = (
                    False,
                    "UPDATE of a submitted case's ground truth was NOT blocked",
                )
            except sqlite3.IntegrityError:
                trig_ok, terr = True, ""
            finally:
                c.execute("ROLLBACK TO trig")
                c.execute("RELEASE trig")
        r.check("frozen-ground-truth trigger blocks an edit", trig_ok, terr)
        c.close()

        backups = sorted(Path(work).glob("copy.db.pre-migration-*.sqlite.bak"))
        r.check(
            "exactly one pre-migration backup written", len(backups) == 1, str(backups)
        )
        if backups:
            b = snapshot(str(backups[0]))
            r.check(
                "backup row counts equal the pre-migration counts",
                b["counts"] == before["counts"],
                f"{b['counts']} vs {before['counts']}",
            )

        db.init_db()  # second start must be a no-op
        again = sorted(Path(work).glob("copy.db.pre-migration-*.sqlite.bak"))
        r.check(
            "second start is idempotent (no second backup, same data)",
            len(again) == len(backups) and snapshot(copy) == after,
        )

        print(
            "\nREHEARSAL",
            "PASSED" if r.failed == 0 else f"FAILED ({r.failed} check(s))",
        )
        if args.keep:
            print(f"kept: {work}")
        return 0 if r.failed == 0 else 1
    except SystemExit as exc:
        print(f"FAIL: migration aborted with SystemExit: {exc}")
        return 1
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)


def _only_duplicates(before: dict, lost_ids: list) -> bool:
    keep = {}
    for s in before["submissions"]:
        keep.setdefault((s[1], s[2], s[3]), []).append(s[0])
    return all(
        any(i in ids and i != max(ids) for ids in keep.values()) for i in lost_ids
    )


if __name__ == "__main__":
    sys.exit(main())
