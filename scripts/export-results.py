#!/usr/bin/env python3
"""Export the doctors' exam results: a CSV of every answer plus a readable summary.

Why: the app exists to produce analytics (accuracy, time per case, common mistakes, completion) for a
scientific publication. The data is all in the grading database; this command makes it visible to the
coordinator and the radiologist. It is READ-ONLY (it never writes to the database) and it contains no
patient data: only the pseudonymous student ids the coordinator assigned.

Usage (from the repository root):
  python3 scripts/export-results.py                          # demo doctors (student ids DEMO_*), from the running stack
  python3 scripts/export-results.py --student-prefix STU_    # another group
  python3 scripts/export-results.py --all                    # every student in the database
  python3 scripts/export-results.py --db /path/grading.db    # a database file (for example a backup copy)
  python3 scripts/export-results.py --include-text           # also the free-text answers of the learning stage

Where the data comes from: by default it runs a small read-only query INSIDE the running grading-api
container (`docker exec`), because the database lives in a Docker volume. `--db FILE` reads a SQLite file
directly instead (opened read-only).

Output, in --out-dir (default local/exports/<UTC timestamp>/, created with mode 0700, files 0600, git-ignored):
  attempts.csv   one row per answer (see COLUMNS); the answer key (ground truth) is IN this file, so treat
                 it like the answer key itself: coordinator and researchers only
  summary.txt    the same summary that is printed:
                   1. completion: how many doctors started, finished, approved the review
                   2. per doctor: accuracy of the assessment and test stages, time per case
                   3. confusion matrix: ground-truth category (rows) against the doctor's category (columns)
                   4. per case: how many answered, % correct, median time
What counts as "correct" is the system's rule: the Lung-RADS CATEGORY matches the ground truth. Modifier S
is reported separately (agreement) because it is not part of the verdict. The learning stage is not graded.

Exit status: 0 = written; 1 = could not read the data (message on stderr); 2 = bad usage.
"""

import argparse
import csv
import json
import os
import sqlite3
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_CONTAINER = "ipcmc-grading-api"
DEFAULT_PREFIX = "DEMO_"
STAGE_ORDER = {"learning": 0, "assessment": 1, "test": 2}
CATEGORIES = ["0", "1", "2", "3", "4A", "4B", "4X"]

COLUMNS = [
    "student_id",
    "stage",
    "position",
    "case_title",
    "case_id",
    "case_version",
    "submitted_category",
    "submitted_modifier_s",
    "ground_truth_category",
    "ground_truth_modifier_s",
    "correct",
    "time_spent_seconds",
    "submitted_at_utc",
    "doctor_finished",
    "review_approved",
]


class ExportError(Exception):
    """A problem phrased for the person running the export."""


# ---- reading the data ---------------------------------------------------------------------------

QUERY_SUBMISSIONS = """
SELECT s.student_id, s.stage, c.order_index, c.title, s.case_id, s.case_version,
       s.submitted_category, s.submitted_modifier_s, s.submitted_text, s.is_correct,
       s.ground_truth_category, s.ground_truth_modifier_s,
       c.ground_truth_category AS case_category, c.ground_truth_modifier_s AS case_modifier_s,
       s.time_spent_seconds, s.submitted_at
FROM submissions s JOIN cases c ON c.id = s.case_id
ORDER BY s.student_id, s.submitted_at
"""

# Runs INSIDE the container (python is there; the code is passed on stdin, nothing is installed or written).
CONTAINER_SNIPPET = '''
import json, os, sqlite3, sys
path = os.environ.get("GRADING_DB_PATH", "/data/grading.db")
conn = sqlite3.connect("file:" + path + "?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
sub = [dict(r) for r in conn.execute(QUERY)]
cols = {r[1] for r in conn.execute("PRAGMA table_info(progress)")}
fin = "finalized_at" if "finalized_at" in cols else "NULL AS finalized_at"
prog = [dict(r) for r in conn.execute("SELECT student_id, stage, " + fin + " FROM progress")]
json.dump({"submissions": sub, "progress": prog}, sys.stdout)
'''


def read_sqlite(path: str) -> dict:
    """Read submissions and progress from a SQLite file, opened READ-ONLY."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ExportError(f"cannot open the database {path}: {exc}") from None
    conn.row_factory = sqlite3.Row
    try:
        submissions = [dict(r) for r in conn.execute(QUERY_SUBMISSIONS)]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(progress)")}
        fin = "finalized_at" if "finalized_at" in cols else "NULL AS finalized_at"
        progress = [dict(r) for r in conn.execute(f"SELECT student_id, stage, {fin} FROM progress")]
    except sqlite3.Error as exc:
        raise ExportError(f"cannot read the database {path}: {exc}") from None
    finally:
        conn.close()
    return {"submissions": submissions, "progress": progress}


def read_container(name: str, runner=subprocess.run) -> dict:
    code = f"QUERY = {QUERY_SUBMISSIONS!r}\n{CONTAINER_SNIPPET}"
    res = runner(["docker", "exec", "-i", name, "python", "-"], input=code, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        hint = "is the stack running (docker compose up -d)?" if "No such container" in (res.stderr or "") else ""
        raise ExportError(f"cannot read the database from container {name}: {(res.stderr or '').strip()[-300:]} {hint}".strip())
    try:
        return json.loads(res.stdout)
    except ValueError:
        raise ExportError("the container returned something that is not JSON") from None


# ---- shaping -----------------------------------------------------------------------------------


def iso_utc(epoch) -> str:
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def build_attempts(raw: dict, prefix: str | None) -> tuple:
    """Rows for the CSV and the statistics: filtered by student prefix, ordered by doctor, stage, case."""
    status = {}
    for p in raw["progress"]:
        status[p["student_id"]] = {"finished": p["stage"] == "complete", "approved": p.get("finalized_at") is not None}
    rows = []
    for s in raw["submissions"]:
        sid = s["student_id"]
        if prefix and not sid.startswith(prefix):
            continue
        graded = s["stage"] != "learning"
        # graded stages: the frozen snapshot (issue #96); the learning stage has none, use the case row
        gt = s["ground_truth_category"] if graded and s["ground_truth_category"] else s["case_category"]
        gt_s = s["ground_truth_modifier_s"] if graded and s["ground_truth_category"] else s["case_modifier_s"]
        st = status.get(sid, {})
        rows.append(
            {
                "student_id": sid,
                "stage": s["stage"],
                "position": (s["order_index"] or 0) + 1,
                "case_title": s["title"],
                "case_id": s["case_id"],
                "case_version": s["case_version"],
                "submitted_category": s["submitted_category"] or "",
                "submitted_modifier_s": "" if s["submitted_modifier_s"] is None else int(bool(s["submitted_modifier_s"])),
                "ground_truth_category": gt or "",
                "ground_truth_modifier_s": "" if gt_s is None else int(bool(gt_s)),
                "correct": "" if not graded or s["is_correct"] is None else int(bool(s["is_correct"])),
                "time_spent_seconds": "" if s["time_spent_seconds"] is None else round(float(s["time_spent_seconds"]), 1),
                "submitted_at_utc": iso_utc(s["submitted_at"]),
                "doctor_finished": int(st.get("finished", False)),
                "review_approved": int(st.get("approved", False)),
                "_text": s.get("submitted_text") or "",
            }
        )
    rows.sort(key=lambda r: (r["student_id"], STAGE_ORDER.get(r["stage"], 9), r["position"]))
    doctors = {}
    for p in raw["progress"]:
        sid = p["student_id"]
        if prefix and not sid.startswith(prefix):
            continue
        doctors[sid] = {"stage": p["stage"], "approved": p.get("finalized_at") is not None}
    return rows, doctors


def csv_cell(value):
    """A spreadsheet treats a cell that starts with = + - @ as a formula; neutralise it (CSV injection)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def write_csv(path: Path, rows: list, include_text: bool) -> None:
    columns = COLUMNS + (["learning_text"] if include_text else [])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for r in rows:
            out = [csv_cell(r[c]) for c in COLUMNS]
            if include_text:
                out.append(csv_cell(r["_text"] if r["stage"] == "learning" else ""))
            writer.writerow(out)


# ---- statistics --------------------------------------------------------------------------------


def pct(num: int, den: int) -> str:
    return "n/a" if den == 0 else f"{round(100 * num / den)}%"


def mmss(seconds) -> str:
    if seconds is None:
        return "n/a"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def median_time(rows) -> float | None:
    times = [float(r["time_spent_seconds"]) for r in rows if r["time_spent_seconds"] != ""]
    return statistics.median(times) if times else None


def compute(rows: list, doctors: dict) -> dict:
    graded = [r for r in rows if r["stage"] != "learning" and r["correct"] != ""]
    per_doctor = {}
    for sid in sorted(set(doctors) | {r["student_id"] for r in rows}):
        mine = [r for r in rows if r["student_id"] == sid]
        entry = {"stage": doctors.get(sid, {}).get("stage", "?"), "approved": doctors.get(sid, {}).get("approved", False)}
        for stage in ("assessment", "test"):
            g = [r for r in mine if r["stage"] == stage and r["correct"] != ""]
            entry[stage] = (sum(r["correct"] for r in g), len(g))
        entry["answers"] = len(mine)
        entry["median_time"] = median_time(mine)
        times = [float(r["time_spent_seconds"]) for r in mine if r["time_spent_seconds"] != ""]
        entry["total_time"] = sum(times) if times else None  # 0 seconds is a value, not "no data"
        per_doctor[sid] = entry
    confusion = Counter((r["ground_truth_category"], r["submitted_category"]) for r in graded)
    modifier = [(r["ground_truth_modifier_s"], r["submitted_modifier_s"]) for r in graded if r["submitted_modifier_s"] != ""]
    per_case = defaultdict(list)
    for r in rows:
        per_case[(STAGE_ORDER.get(r["stage"], 9), r["position"], r["case_title"], r["stage"])].append(r)
    return {
        "doctors": per_doctor,
        "started": len(per_doctor),
        "finished": sum(1 for d in per_doctor.values() if d["stage"] == "complete"),
        "approved": sum(1 for d in per_doctor.values() if d["approved"]),
        "graded": len(graded),
        "correct": sum(r["correct"] for r in graded),
        "confusion": confusion,
        "modifier_agree": (sum(1 for gt, sub in modifier if gt == sub), len(modifier)),
        "per_case": [
            (key[3], key[1], key[2], items) for key, items in sorted(per_case.items(), key=lambda kv: kv[0][:3])
        ],
        "median_time": median_time(rows),
    }


def table(header: list, body: list) -> list:
    widths = [max(len(str(x)) for x in col) for col in zip(header, *body)] if body else [len(h) for h in header]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    return [fmt.format(*header), fmt.format(*["-" * w for w in widths])] + [fmt.format(*map(str, r)) for r in body]


def render_summary(stats: dict, prefix: str | None, generated: str) -> str:
    out = []
    scope = f"student ids starting with {prefix}" if prefix else "all students"
    out.append(f"Exam results summary ({scope}), generated {generated}")
    out.append("")
    out.append("1. Completion")
    out.append(f"   doctors started: {stats['started']}")
    out.append(f"   finished all stages: {stats['finished']} ({pct(stats['finished'], stats['started'])})")
    out.append(f"   approved the final review: {stats['approved']} ({pct(stats['approved'], stats['started'])})")
    out.append("")
    out.append("2. Per doctor (a verdict counts the Lung-RADS category; the learning stage is not graded)")
    body = []
    for sid, d in stats["doctors"].items():
        a, t = d["assessment"], d["test"]
        body.append([sid, d["stage"], f"{a[0]}/{a[1]}", f"{t[0]}/{t[1]}", pct(t[0], t[1]), mmss(d["median_time"]), mmss(d["total_time"]), "yes" if d["approved"] else "no"])
    out += ["   " + line for line in table(["doctor", "stage now", "assessment", "test", "test %", "median/case", "total", "approved"], body)] if body else ["   (no doctors)"]
    out.append("")
    out.append(f"3. Confusion matrix (graded answers: {stats['graded']}, correct {stats['correct']} = {pct(stats['correct'], stats['graded'])})")
    out.append("   rows = ground truth, columns = the doctor's category")
    truths = [c for c in CATEGORIES if any(gt == c for gt, _ in stats["confusion"])]
    columns = [c for c in CATEGORIES if any(gt == c or sub == c for gt, sub in stats["confusion"])]
    if truths:
        body = [[gt] + [stats["confusion"].get((gt, sub), 0) or "." for sub in columns] for gt in truths]
        out += ["   " + line for line in table(["truth \\ answer"] + columns, body)]
    else:
        out.append("   (no graded answers yet)")
    agree, total = stats["modifier_agree"]
    out.append(f"   modifier S agrees with the ground truth in {agree}/{total} graded answers ({pct(agree, total)}); it is not part of the verdict")
    out.append("")
    out.append("4. Per case")
    body = []
    for stage, position, title, items in stats["per_case"]:
        g = [r for r in items if r["correct"] != ""]
        ok = sum(r["correct"] for r in g)
        body.append([stage, position, title[:34], len(items), pct(ok, len(g)) if g else "not graded", mmss(median_time(items))])
    out += ["   " + line for line in table(["stage", "#", "case", "answers", "correct", "median time"], body)] if body else ["   (no answers)"]
    out.append("")
    out.append("Median time per answer over everything: " + mmss(stats["median_time"]))
    return "\n".join(out) + "\n"


# ---- main ----------------------------------------------------------------------------------------


def main(argv=None, runner=subprocess.run, now=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="read this SQLite file (read-only) instead of the running container")
    ap.add_argument("--container", default=DEFAULT_CONTAINER, help=f"grading-api container (default {DEFAULT_CONTAINER})")
    ap.add_argument("--student-prefix", default=DEFAULT_PREFIX, help=f"only student ids starting with this (default {DEFAULT_PREFIX})")
    ap.add_argument("--all", action="store_true", help="every student, ignoring the prefix")
    ap.add_argument("--include-text", action="store_true", help="add the learning-stage free-text answers to the CSV")
    ap.add_argument("--out-dir", help="output directory (default local/exports/<UTC timestamp>)")
    args = ap.parse_args(argv)

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    prefix = None if args.all else args.student_prefix
    try:
        raw = read_sqlite(args.db) if args.db else read_container(args.container, runner)
        rows, doctors = build_attempts(raw, prefix)
        stats = compute(rows, doctors)
        out_dir = Path(args.out_dir) if args.out_dir else REPO / "local" / "exports" / stamp
        out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            write_csv(out_dir / "attempts.csv", rows, args.include_text)
            summary = render_summary(stats, prefix, stamp)
            fd = os.open(out_dir / "summary.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(summary)
        except FileExistsError:
            raise ExportError(f"{out_dir} already holds an export; choose another --out-dir (nothing is overwritten)") from None
    except ExportError as exc:
        print(f"export-results: {exc}", file=sys.stderr)
        return 1
    print(summary)
    print(f"Files (mode 0600): {out_dir / 'attempts.csv'}  {out_dir / 'summary.txt'}")
    print("attempts.csv contains the answer key (ground truth): coordinator and researchers only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
