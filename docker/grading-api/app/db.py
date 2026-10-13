"""
SQLite schema + seed data for the Lung-RADS grading mechanics.

Seed data is PLACEHOLDER content: none of the three sample studies
(CT_small/MR_small/BRAINIX -- see /README.md and scripts/load-sample-studies.sh)
are actually lung CTs. This is here to prove the 3-stage mechanics work end
to end, not to be clinically meaningful. Real curated content (500 studies,
real ground truth, real reference reports from a radiologist) is separate
work, not yet started.
"""

import os
import sqlite3
import sys
import time

# main.py reads db.TOKEN_TTL_SECONDS; validation lives in config.py (#97).
from .cases_file import CasesFileError, load_cases
from .config import TOKEN_TTL_SECONDS  # noqa: F401  re-export, call sites unchanged
from .logging_config import get_logger

DB_PATH = os.environ.get("GRADING_DB_PATH", "/data/grading.db")

logger = get_logger(__name__)


def _fatal_config_error(message):
    """Clean one-line operator abort, same contract as config.py's import-time
    handler (issue #160 review N1): stdout stays reserved for structured JSON
    logs, the message goes to stderr, exit code is 2. Raising a bare
    SystemExit(str) from inside init_db() -- which runs in lifespan(), inside
    uvicorn's task group -- resurfaces it as a BaseExceptionGroup traceback
    with exit code 1: the message is still there but buried, and scripts
    cannot distinguish "bad configuration" (2) from "crash" (1).
    """
    print(message, file=sys.stderr)
    raise SystemExit(2) from None


# How long a minted session token stays valid -- see config.py for the
# (now validated) reading of GRADING_TOKEN_TTL_SECONDS; re-exported above
# so every existing db.TOKEN_TTL_SECONDS call site keeps working.

STAGES = ["learning", "assessment", "test"]

# Single source of truth for category labels -- both the API responses and
# (indirectly, since the frontend just renders what /case returns) the UI
# pull from here, so the Polish text from the PM's spec lives in exactly
# one place.
CATEGORY_LABELS = [
    ("0", "Kategoria 0 – badanie niekompletne / wymagane porównanie z badaniami poprzednimi"),
    ("1", "Kategoria 1 – wynik negatywny, brak guzków lub zmiany jednoznacznie łagodne"),
    ("2", "Kategoria 2 – zmiany łagodne, bardzo niskie ryzyko złośliwości"),
    ("3", "Kategoria 3 – zmiany prawdopodobnie łagodne, niskie ryzyko, zalecana kontrola krótkoterminowa"),
    ("4A", "Kategoria 4A – podejrzane, umiarkowane ryzyko złośliwości"),
    ("4B", "Kategoria 4B – wysoce podejrzane, wysokie ryzyko złośliwości"),
    ("4X", "Kategoria 4X – kategoria 3/4 z dodatkowymi cechami zwiększającymi podejrzenie złośliwości"),
]

SEED_CASES = [
    # (stage, order_index, orthanc_study_uid, title, ground_truth_category, ground_truth_modifier_s, reference_report)
    (
        "learning",
        0,
        "1.3.6.1.4.1.5962.1.2.1.20040119072730.12322",  # CT_small
        "Przypadek 1 (nauka)",
        "2",
        0,
        "PLACEHOLDER — nie jest to prawdziwy opis kliniczny. "
        "Badanie TK klatki piersiowej bez cech guzków podejrzanych. "
        "Widoczna drobna zmiana łagodna o niskim ryzyku złośliwości. "
        "Kategoria referencyjna: Lung-RADS 2.",
    ),
    (
        "assessment",
        0,
        "1.3.6.1.4.1.5962.1.2.4.20040826185059.5457",  # MR_small
        "Przypadek 1 (ocena)",
        "3",
        0,
        "PLACEHOLDER — nie jest to prawdziwy opis kliniczny. "
        "Zmiana prawdopodobnie łagodna, zalecana kontrola krótkoterminowa. "
        "Kategoria referencyjna: Lung-RADS 3.",
    ),
    (
        "test",
        0,
        "2.16.840.1.113669.632.20.1211.10000357775",  # BRAINIX
        "Przypadek 1 (test)",
        "4A",
        1,
        "PLACEHOLDER — nie jest to prawdziwy opis kliniczny. "
        "Zmiana podejrzana, umiarkowane ryzyko złośliwości, obecna dodatkowo "
        "zmiana istotna klinicznie spoza płuc. Kategoria referencyjna: Lung-RADS 4A, modyfikator S.",
    ),
]


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL mode (scaling concern raised when discussing a real multi-VM
    # cohort deployment): SQLite's default rollback-journal mode blocks
    # ALL readers for the duration of a write -- under concurrent
    # submissions (many students finishing a case around the same
    # moment), that serializes requests that don't need to touch each
    # other's data at all. WAL allows readers to proceed concurrently
    # with a single in-progress writer, which is the actual contention
    # pattern here (many /case reads, occasional /submit writes) -- a
    # meaningfully higher concurrency ceiling for a 50-user pilot without
    # the bigger step of migrating off SQLite entirely (still the right
    # move before a real 300-user cohort, per CLAUDE.md/README).
    # `journal_mode=WAL` is a database-file-level setting, not per-
    # connection -- calling it here is idempotent (a no-op once already
    # set) and self-migrating: it takes effect on an existing pre-WAL
    # database file the same way, no separate migration step needed.
    # `synchronous=NORMAL` is WAL's own documented pairing -- WAL's
    # write-ahead log already provides the durability guarantee that
    # makes the stricter (and slower) FULL setting unnecessary.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    # issue #70: WAL lets readers run alongside ONE writer, but two
    # simultaneous writers still collide (SUBMIT-heavy moments: a whole
    # cohort clicking through cases at the same time). Make the lock wait
    # EXPLICIT. CPython's sqlite3.connect(timeout=5.0) default already
    # installs busy_timeout=5000 under the hood -- which is exactly the
    # problem: the guarantee this app's 500s-free operation relies on was
    # an invisible stdlib default, one `connect(..., timeout=0)` refactor
    # away from turning every lock collision into an immediate
    # OperationalError -> spurious student-facing 500. Explicit PRAGMA =
    # visible contract (db.get_connection().execute("PRAGMA
    # busy_timeout") reads back 5000 regardless of connect kwargs) +
    # test_db_busy_timeout.py pins the actual wait-under-contention
    # behavior, with a negative control proving the test could see the
    # difference. 5s stays: normal submit transactions are single-digit
    # milliseconds, so five seconds is pure headroom -- while a much
    # larger value would just convert a wedged external lockholder into
    # hung requests instead of a visible (and loggable) busy error.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _backup_before_migration(conn):
    """issue #96 / #85 hook: any deploy against an EXISTING database
    migrates (rewrites) real rows at container start, silently -- so the
    migration gets a consistent file copy first. VACUUM INTO, not `cp`
    (#85's own warning): it captures a transactionally consistent
    snapshot INCLUDING in-flight WAL contents, which `cp` of a live
    WAL-mode file does not.

    Stays on-premise on the same volume (CLAUDE.md); rotation/
    encryption/retention of these files is #85's job, not this hook's.
    Failure to back up aborts startup: migrating without a copy is the
    irreversible path this hook exists to prevent."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup_path = f"{DB_PATH}.pre-migration-{stamp}.sqlite.bak"
    try:
        conn.execute("VACUUM INTO ?", (backup_path,))
        # issue #96 CR: this copy holds real student answers -- VACUUM INTO
        # creates the file under the process umask (0644 in the default
        # container), readable by any local account. Tighten it before
        # anything else can open it; failing to do so is as bad as failing
        # to copy at all. Rotation/retention/encryption stay #85's job.
        os.chmod(backup_path, 0o600)
    except (sqlite3.Error, OSError):
        logger.exception("pre_migration_backup_failed")
        _fatal_config_error(
            f"CONFIGURATION ERROR: could not write the pre-migration backup "
            f"({backup_path}); refusing to migrate real data without a copy (issue #85)."
        )
    logger.info("pre_migration_backup_written", extra={"backup_path": backup_path})


def _migrate_existing_schema(conn):
    """Handles a database file that already existed before a later schema
    change -- CREATE TABLE IF NOT EXISTS is a genuine no-op once a table
    already exists in ANY form, not a schema-reconciliation step. Found
    the hard way: rebuilding grading-api against a real, previously-
    running /data/grading.db (predating issues #28/#29) broke every
    /case and /submit call outright with "no such column:
    case_assigned_at" -- the column/constraint additions below had never
    actually been verified against an existing database, only fresh ones
    (every test uses an empty tmp_path file via conftest.py's `client`
    fixture, which never exercises this path at all).
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "cases" in tables:
        cases_cols = {row[1] for row in conn.execute("PRAGMA table_info(cases)").fetchall()}
        if "version" not in cases_cols:
            # issue #96: ground truth becomes versionable. SQLite can't
            # drop the inline UNIQUE(stage, order_index) in place, so
            # rebuild under UNIQUE(stage, order_index, version).
            #
            # PARENT-table rebuild: `ALTER TABLE cases RENAME TO cases_old`
            # first is WRONG here -- since SQLite 3.26 RENAME rewrites
            # FOREIGN KEY clauses in CHILD tables, so submissions would
            # point at "cases_old" and the later DROP leaves EVERY /submit
            # on a migrated database dying with "no such table:
            # main.cases_old" (CR on #120, reproduced on sqlite 3.46 --
            # invisible to tests until one inserts AFTER migrating; see
            # test_after_migration_submissions...). Follow SQLite's
            # documented rebuild order instead: build cases_new, copy with
            # EXPLICIT columns (INSERT...SELECT * is column-order
            # dependent; executescript's implicit COMMIT is forbidden --
            # manual BEGIN IMMEDIATE per statement, reviewer note on #96),
            # DROP the old table, RENAME into place. Children reference
            # "cases" BY NAME and resolve against the new table again;
            # legacy_alter_table=ON keeps RENAME touching only the renamed
            # table's own references and tolerates the transient dangling
            # child reference mid-swap. foreign_key_check runs BEFORE
            # COMMIT -- anything it reports rolls the whole swap back
            # instead of committing a schema every /submit will hit.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("PRAGMA legacy_alter_table=ON")
            conn.isolation_level = None  # manual transaction control
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """CREATE TABLE cases_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        stage TEXT NOT NULL,
                        order_index INTEGER NOT NULL,
                        orthanc_study_uid TEXT NOT NULL,
                        title TEXT NOT NULL,
                        ground_truth_category TEXT NOT NULL,
                        ground_truth_modifier_s INTEGER NOT NULL DEFAULT 0,
                        reference_report TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        UNIQUE(stage, order_index, version)
                    )"""
                )
                conn.execute(
                    """INSERT INTO cases_new
                        (id, stage, order_index, orthanc_study_uid, title,
                         ground_truth_category, ground_truth_modifier_s, reference_report, version)
                       SELECT id, stage, order_index, orthanc_study_uid, title,
                              ground_truth_category, ground_truth_modifier_s, reference_report, 1
                       FROM cases"""
                )
                conn.execute("DROP TABLE cases")
                conn.execute("ALTER TABLE cases_new RENAME TO cases")
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise sqlite3.IntegrityError(f"foreign_key_check after cases rebuild: {violations[:5]}")
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                # Loud SystemExit, not a half-migrated service: every DROP
                # and RENAME above sits inside the one BEGIN IMMEDIATE, so
                # ROLLBACK restores the exact pre-swap state (cases intact
                # under its own name) and the _backup_before_migration
                # copy is on disk.
                logger.exception("cases_version_migration_failed")
                _fatal_config_error(
                    "CONFIGURATION ERROR: could not migrate the cases table to the "
                    "versioned schema (issue #96); the database is unchanged. "
                    "See the traceback above for the underlying sqlite3 error."
                )
            finally:
                conn.isolation_level = ""
                conn.execute("PRAGMA legacy_alter_table=OFF")
            conn.execute("PRAGMA foreign_keys=ON")
            # idx_cases_stage died with the dropped table; init_db's
            # executescript (every start, after this function, IF NOT
            # EXISTS) recreates indexes for the swapped-in table.

    if "progress" in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(progress)").fetchall()}
        if "case_assigned_at" not in cols:
            # Existing in-progress students' real assignment moment is
            # unknowable in hindsight -- backfilling "now" understates
            # their actual time-on-task for whatever case they're
            # currently on, but that's a one-time, one-case measurement
            # gap for students who were mid-session during this upgrade,
            # not an ongoing correctness issue.
            conn.execute("ALTER TABLE progress ADD COLUMN case_assigned_at REAL")
            conn.execute(
                "UPDATE progress SET case_assigned_at = ? WHERE case_assigned_at IS NULL",
                (time.time(),),
            )
            conn.commit()
        if "finalized_at" not in cols:
            # issue #202: purely additive and nullable, existing rows stay untouched
            # (a doctor who had already finished simply has not approved yet, so the
            # new review screen is what they see next). The pre-migration backup of
            # _migration_needed() below runs first.
            conn.execute("ALTER TABLE progress ADD COLUMN finalized_at REAL")
            conn.commit()

    if "submissions" in tables:
        has_unique = any(row[2] for row in conn.execute("PRAGMA index_list(submissions)").fetchall())
        if not has_unique:
            # SQLite can't ALTER TABLE to add a constraint -- rebuild the
            # table under the new schema instead (SQLite's own documented
            # pattern for this). Keeps only the latest row per
            # (student_id, case_id, stage) if any pre-constraint
            # duplicates exist, rather than failing the migration outright
            # on a UNIQUE violation partway through.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript(
                """
                ALTER TABLE submissions RENAME TO submissions_old;
                CREATE TABLE submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    student_id TEXT NOT NULL,
                    case_id INTEGER NOT NULL REFERENCES cases(id),
                    stage TEXT NOT NULL,
                    submitted_category TEXT,
                    submitted_modifier_s INTEGER,
                    submitted_text TEXT,
                    is_correct INTEGER,
                    time_spent_seconds REAL,
                    submitted_at REAL NOT NULL,
                    UNIQUE(student_id, case_id, stage)
                );
                INSERT INTO submissions
                    (id, student_id, case_id, stage, submitted_category, submitted_modifier_s,
                     submitted_text, is_correct, time_spent_seconds, submitted_at)
                    SELECT id, student_id, case_id, stage, submitted_category, submitted_modifier_s,
                           submitted_text, is_correct, time_spent_seconds, submitted_at
                    FROM submissions_old
                    WHERE id IN (
                        SELECT MAX(id) FROM submissions_old
                        GROUP BY student_id, case_id, stage
                    );
                DROP TABLE submissions_old;
                """
            )
            conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")

        sub_cols = {row[1] for row in conn.execute("PRAGMA table_info(submissions)").fetchall()}
        if "ground_truth_category" not in sub_cols:
            # issue #96: freeze the ground truth into each submission as
            # of /submit time. ALTER ADD COLUMN with constant defaults
            # needs no table rebuild (unlike the cases UNIQUE change
            # above). Runs AFTER the unique-rebuild branch so a pre-#28
            # database lands on the same schema in a single start.
            conn.execute("ALTER TABLE submissions ADD COLUMN ground_truth_category TEXT")
            conn.execute("ALTER TABLE submissions ADD COLUMN ground_truth_modifier_s INTEGER")
            conn.execute("ALTER TABLE submissions ADD COLUMN case_version INTEGER")
            conn.execute("ALTER TABLE submissions ADD COLUMN gt_backfilled INTEGER NOT NULL DEFAULT 0")
            # Backfill from CURRENT cases: best available guess, NOT a
            # proven snapshot -- if the course GT was edited before this
            # upgrade, these rows carry today's GT, not the GT in effect
            # when the student answered. gt_backfilled=1 marks exactly
            # that uncertainty for whoever analyzes the data later.
            # Skipped (new columns stay NULL) when the database has no
            # cases table at all -- the minimal legacy fixtures in
            # test_state_machine.py exercise exactly that shape.
            if "cases" in tables:
                conn.execute(
                    "UPDATE submissions SET "
                    "ground_truth_category = "
                    "(SELECT c.ground_truth_category FROM cases c WHERE c.id = submissions.case_id), "
                    "ground_truth_modifier_s = "
                    "(SELECT c.ground_truth_modifier_s FROM cases c WHERE c.id = submissions.case_id), "
                    "case_version = (SELECT c.version FROM cases c WHERE c.id = submissions.case_id), "
                    "gt_backfilled = 1"
                )
                conn.commit()
                logger.warning(
                    "ground_truth_backfilled",
                    extra={
                        "note": "existing submissions' ground truth backfilled "
                        "from CURRENT cases; marked gt_backfilled=1"
                    },
                )


def _migration_needed(conn) -> bool:
    """True only when _migrate_existing_schema would actually change
    something -- drives the #85 pre-migration backup so a routine
    restart of an already-current database neither rewrites anything nor
    litters the volume with byte-identical copies."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if not tables:
        return False
    if "progress" in tables:
        if "case_assigned_at" not in {row[1] for row in conn.execute("PRAGMA table_info(progress)").fetchall()}:
            return True
    if "cases" in tables:
        if "version" not in {row[1] for row in conn.execute("PRAGMA table_info(cases)").fetchall()}:
            return True
    if "progress" in tables and "finalized_at" not in {
        row[1] for row in conn.execute("PRAGMA table_info(progress)").fetchall()
    }:
        return True  # issue #202: back up real data before adding the column too
    if "submissions" in tables:
        if not any(row[2] for row in conn.execute("PRAGMA index_list(submissions)").fetchall()):
            return True
        if "ground_truth_category" not in {row[1] for row in conn.execute("PRAGMA table_info(submissions)").fetchall()}:
            return True
    return False


def _check_data_writable():
    """Refuse to start when the process cannot WRITE its database (issue #51 follow-up).

    Since the image runs as uid 10001, a `grading-db` volume created by an older, root-run
    image is root-owned: the container used to start, /healthz (a read) stayed green, and the
    first real write (a session, a submission) failed with `attempt to write a readonly
    database` -> HTTP 500 for the doctor. Checking here turns that into one clear start-up
    error (exit 2, same contract as _fatal_config_error) that `docker compose up` and the
    health checks surface immediately. Compose also fixes the ownership by itself through the
    one-shot `grading-db-init` service; this check covers every other way of running the image.
    """
    directory = os.path.dirname(os.path.abspath(DB_PATH)) or "."
    problems = []
    if not os.access(directory, os.W_OK | os.X_OK):
        problems.append(f"the directory {directory} is not writable")
    if os.path.exists(DB_PATH) and not os.access(DB_PATH, os.W_OK):
        problems.append(f"the database file {DB_PATH} is not writable")
    if problems:
        _fatal_config_error(
            "CONFIGURATION ERROR: " + " and ".join(problems) + f" for uid {os.getuid()}. "
            "A data volume created by a pre-#51 (root) image is root-owned. With the repository's "
            "docker-compose files the `grading-db-init` service fixes this automatically; otherwise run once: "
            "docker run --rm -u 0 -v <your grading-db volume>:/data alpine chown -R 10001:10001 /data"
        )


def init_db():
    _check_data_writable()
    conn = get_connection()
    # issue #96/#85 hook: copy the file BEFORE anything rewrites it.
    # Gated on _migration_needed so routine restarts don't pile up
    # identical copies (nor pay the VACUUM cost) on an already-current db.
    if _migration_needed(conn):
        _backup_before_migration(conn)
    _migrate_existing_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            orthanc_study_uid TEXT NOT NULL,
            title TEXT NOT NULL,
            ground_truth_category TEXT NOT NULL,
            ground_truth_modifier_s INTEGER NOT NULL DEFAULT 0,
            reference_report TEXT NOT NULL,
            -- issue #96: a case's ground truth may legally change
            -- between cohort runs (user decision 2026-10-06) -- as a NEW
            -- version row, never as an UPDATE once submissions exist
            -- (enforced by cases_ground_truth_frozen below). "The
            -- current case" = highest version at a position, see
            -- main.py's _get_case().
            version INTEGER NOT NULL DEFAULT 1,
            UNIQUE(stage, order_index, version),
            -- issue #99: closed vocabularies enforced one level below the
            -- API too, so manual SQL seeding can't silently invent a
            -- stage/category the state machine and scoring never learned.
            -- Fresh-install scope ONLY on purpose: adding CHECKs to an
            -- existing database requires a table rebuild, and we do not
            -- rewrite a live graded DB for a constraint the API already
            -- enforces (main.py Literals cover every deployment shape).
            CHECK (stage IN ('learning', 'assessment', 'test')),
            CHECK (ground_truth_category IN ('0', '1', '2', '3', '4A', '4B', '4X'))
        );

        -- case_assigned_at (issue #29): stamped server-side the moment a
        -- case becomes the student's active one (fresh progress, or
        -- _advance_progress landing on the next case/stage) -- the only
        -- honest way to measure time-on-task. The client's own
        -- Date.now()-based elapsed time is trivially editable (devtools,
        -- or a scripted request) and this data feeds a scientific
        -- publication, so it's no longer trusted for that value at all;
        -- see main.py's submit(). Only takes effect here for a genuinely
        -- fresh database -- _migrate_existing_schema() above handles an
        -- existing one.
        CREATE TABLE IF NOT EXISTS progress (
            student_id TEXT PRIMARY KEY,
            stage TEXT NOT NULL,
            case_order_index INTEGER NOT NULL,
            case_assigned_at REAL NOT NULL,
            -- issue #202: set once, by POST /finalize, when the doctor approves the end-of-exam
            -- review. NULL = not approved yet: /results then reveals no ground truth and no
            -- score. Deleted together with the row by /reset (a new run starts unapproved).
            finalized_at REAL,
            -- issue #99, see cases CHECK: fresh-install scope, API-side
            -- Literal covers existing databases.
            CHECK (stage IN ('learning', 'assessment', 'test', 'complete'))
        );

        -- UNIQUE(student_id, case_id, stage) (issue #28): /submit's own
        -- stage check (progress["stage"] != body.stage) and its INSERT are
        -- two separate steps with no locking between them -- a double-
        -- click or a scripted rapid-fire request could pass the check
        -- twice before either write lands, creating duplicate submissions
        -- for the same case/stage and skewing /results accuracy numbers.
        -- This constraint makes that a clean, guaranteed-consistent
        -- IntegrityError (mapped to 409 in main.py's submit()) instead of
        -- silently succeeding twice, without needing a manual transaction/
        -- row-lock around the whole read-then-write span.
        --
        -- Only takes effect here for a genuinely fresh database --
        -- _migrate_existing_schema() above rebuilds an existing
        -- submissions table under this same schema instead (SQLite can't
        -- ALTER TABLE to add a constraint to an existing table).
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL,
            case_id INTEGER NOT NULL REFERENCES cases(id),
            stage TEXT NOT NULL,
            submitted_category TEXT,
            submitted_modifier_s INTEGER,
            submitted_text TEXT,
            is_correct INTEGER,
            time_spent_seconds REAL,
            submitted_at REAL NOT NULL,
            -- issue #96: the ground truth frozen at /submit time (same
            -- transaction, same case row is_correct was computed
            -- against), plus which case version it came from. /results
            -- reads ONLY these columns -- before this, a GT edit after
            -- answering published a self-contradictory record (audit
            -- §3.2: ground_truth 4B vs submitted 4A vs correct:true).
            ground_truth_category TEXT,
            ground_truth_modifier_s INTEGER,
            case_version INTEGER,
            -- 1 only for rows backfilled by the migration from CURRENT
            -- cases (snapshot of convenience, not of proof).
            gt_backfilled INTEGER NOT NULL DEFAULT 0,
            UNIQUE(student_id, case_id, stage),
            -- issue #99, see cases CHECK: fresh-install scope only.
            -- submitted_category/ground_truth_category stay NULL-able
            -- (learning stage has neither; pre-#96 backfill rows may
            -- legitimately lack a snapshot) -- CHECK permits NULL.
            CHECK (stage IN ('learning', 'assessment', 'test')),
            CHECK (submitted_category IS NULL OR submitted_category IN ('0', '1', '2', '3', '4A', '4B', '4X')),
            CHECK (ground_truth_category IS NULL OR ground_truth_category IN ('0', '1', '2', '3', '4A', '4B', '4X'))
        );

        -- issue #96, the enforcement half: editing a SUBMITTED case in any
        -- grading-relevant column is ALWAYS a data-integrity mistake --
        -- not just the ground truth: re-pointing orthanc_study_uid, moving
        -- the case's (stage, order_index) position, or bumping version
        -- after the fact silently rewrites what was graded (CR on #120).
        -- The legal change path is INSERTing a new version row -- allowed
        -- precisely because UNIQUE moved to (stage, order_index, version).
        -- title/reference_report stay editable: presentation text nothing
        -- is scored against. ABORT rolls back just the offending
        -- statement. This trigger is created for fresh AND migrated
        -- databases alike (executescript below runs on every start;
        -- IF NOT EXISTS keeps it idempotent).
        CREATE TRIGGER IF NOT EXISTS cases_ground_truth_frozen
        BEFORE UPDATE OF
            stage, order_index, version, orthanc_study_uid,
            ground_truth_category, ground_truth_modifier_s
        ON cases
        FOR EACH ROW WHEN EXISTS (SELECT 1 FROM submissions WHERE case_id = OLD.id)
        BEGIN
            SELECT RAISE(ABORT, 'immutable once submitted (issue #96): insert a new version row instead');
        END;

        -- Explicit delete guard (CR on #120): with foreign_keys=ON the FK
        -- alone already refuses this DELETE, but tools/scripts may connect
        -- with the pragma OFF (the sqlite3 CLI defaults to OFF!) -- then
        -- the delete would succeed and orphan graded history. Make the
        -- protection schema-level, not session-level.
        CREATE TRIGGER IF NOT EXISTS cases_delete_frozen
        BEFORE DELETE ON cases
        FOR EACH ROW WHEN EXISTS (SELECT 1 FROM submissions WHERE case_id = OLD.id)
        BEGIN
            SELECT RAISE(ABORT, 'case has submissions (issue #96): delete would orphan graded history');
        END;

        -- Binds an unguessable, server-issued token to a student_id --
        -- the actual authorization mechanism (see main.py's /session and
        -- _resolve_token()). student_id/session_id themselves are never
        -- trusted as credentials from here on; they're only ever used for
        -- display (the watermark text), because ANY caller could set them
        -- to anything. A student_id can have at most one live token at a
        -- time (see POST /session's DELETE-then-INSERT) -- minting a new
        -- one revokes whatever came before it for that same student.
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            student_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        );

        -- Indexes for performance (issue #1: missing indexes)
        CREATE INDEX IF NOT EXISTS idx_submissions_student ON submissions(student_id);
        CREATE INDEX IF NOT EXISTS idx_submissions_stage ON submissions(stage);
        CREATE INDEX IF NOT EXISTS idx_submissions_submitted ON submissions(submitted_at);
        CREATE INDEX IF NOT EXISTS idx_cases_stage ON cases(stage);
        CREATE INDEX IF NOT EXISTS idx_sessions_student ON sessions(student_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
        """
    )
    conn.commit()

    count = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    cases_file = os.environ.get("GRADING_CASES_FILE", "").strip()
    if count == 0 and cases_file:
        # Real cases from a local file (never from the repository): see
        # app/cases_file.py. An invalid file stops startup with a message that
        # names the entry and field, never a value.
        try:
            seed_rows = load_cases(cases_file, STAGES, {key for key, _ in CATEGORY_LABELS})
        except CasesFileError as exc:
            conn.close()
            _fatal_config_error(f"CONFIGURATION ERROR: GRADING_CASES_FILE: {exc}")
        conn.executemany(
            """INSERT INTO cases
               (stage, order_index, orthanc_study_uid, title,
                ground_truth_category, ground_truth_modifier_s, reference_report)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            seed_rows,
        )
        conn.commit()
        logger.info("cases_seeded_from_file", extra={"count": len(seed_rows)})
    elif count > 0 and cases_file:
        # Silent ignoring would leave an operator wondering why the new cases do
        # not show up; existing rows may already have submissions (frozen, #96).
        logger.warning(
            "cases_file_ignored",
            extra={
                "reason": "cases table is not empty; recreate the grading-db volume (dev) "
                "or use scripts/new-case-version.py"
            },
        )
    elif count == 0:
        conn.executemany(
            """INSERT INTO cases
               (stage, order_index, orthanc_study_uid, title,
                ground_truth_category, ground_truth_modifier_s, reference_report)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            SEED_CASES,
        )
        conn.commit()
    conn.close()


def now() -> float:
    return time.time()
