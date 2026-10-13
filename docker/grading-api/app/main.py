"""
Lung-RADS grading mechanics: 3-stage state machine (nauka/ocena/test).

See /docs/PROXMOX_DEPLOYMENT.md's sibling doc (README.md, "Lung-RADS grading")
for the overall design. Key rule this file enforces: ground_truth_category
and reference_report are NEVER returned by GET /case (only after POST
/submit, for the stage that's supposed to reveal them) -- the whole point of
the 3 stages is what gets revealed and when.

Authorization (README.md flagged this as a real, unfixed gap; this closes
it): every endpoint below used to take student_id as a bare, client-
supplied parameter with nothing checking it actually belonged to the
caller -- any request could read or submit as any student_id. Now every
endpoint takes an unguessable `token` instead, minted once by POST /session
(coordinator-only, see GRADING_COORDINATOR_KEY below) and resolved
server-side to the real student_id via the `sessions` table. student_id/
session_id are never trusted as credentials again from here on -- they're
only ever used for display (the watermark text), which is fine since
displaying the wrong ID isn't a security problem, only trusting it for
grading actions was.

Revocation (issue #104): POST /session/revoke (coordinator-only) deletes a
token before its TTL expires. It exists so the coordinator can undo a token
it already minted when the rest of the session setup fails -- see
scripts/create-session.py, which mints the token before asking Kasm for a
session (Kasm needs the token as container environment, so the order can't
simply be inverted) and now revokes that token whenever that Kasm call fails.
"""

import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from . import db
from .config import COORDINATOR_KEY, RESET_ENABLED
from .errors import AppError, app_error_handler, unhandled_exception_handler, validation_error_handler
from .logging_config import configure_logging, get_logger, request_id_var

# Configured once, at import time -- not inside lifespan(), which would
# re-run (and reset logging's root handlers) on every TestClient context
# in tests, fighting pytest's own caplog handler. See logging_config.py's
# own comment on configure_logging().
configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


# issue #100, hard-coded and deliberately WITHOUT a dev/docs opt-out
# (same philosophy as config.py's "no dev exception" for the coordinator
# key): an env flag you can forget to unset in production is one forgotten
# variable away from re-exposing an internal grading contract to the
# public internet edge (nginx proxies /api/ straight here -- /api/docs
# was live until this line). The contract surface that IS published is
# the code + tests + CHANGELOG.md. Need the schema? app.openapi() builds
# the same dict in-process regardless of openapi_url:
#   python -c "from app.main import app; import json; print(json.dumps(app.openapi()))"
# Version: single source of truth is API_VERSION below; it is bumped in
# the SAME commit that adds a CHANGELOG.md entry (the review contract for
# this repo: no behavior-visible change lands without one), and the
# matching git tag is cut at release time by the release owner.
API_VERSION = "1.1.0"

app = FastAPI(
    title="IP_CMC Grading API",
    version=API_VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Generates (or honors an incoming) X-Request-Id, makes it available
    to every log line emitted while handling this request via
    request_id_var, and echoes it back in the response -- the actual
    mechanism issue #72 asked for ("trace/log correlation propagated
    across boundaries"). Also logs one INFO line per request (method,
    path, status, duration) as a basic structured access log."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        # issue #95: mirror onto request.state too. Request.state is
        # backed by scope["state"], shared with the Request the OUTER
        # ServerErrorMiddleware later rebuilds for the unhandled-exception
        # handler -- by then this contextvar's finally: below has already
        # reset it, so scope state is the only channel the 500 handler
        # still has the id through.
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        start = time.monotonic()
        try:
            response = await call_next(request)
            # Logged here, still inside the contextvar's scope -- logging
            # this after the finally below resets it would mean the access
            # log line itself never carries the request_id it's meant to
            # be correlated by. Caught by test_logging.py's own assertion
            # that this line's request_id matches the response header.
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
            response.headers["x-request-id"] = request_id
            return response
        finally:
            request_id_var.reset(token)


app.add_middleware(RequestIdMiddleware)

# Shared secret only the coordinator (scripts/create-session.py, run by
# whoever mints links) knows -- required so POST /session can't just be
# called directly by a student's own browser to mint a token for anyone
# else's student_id, which would recreate the exact hole this closes.
# Imported from app/config.py, which validates presence AND minimum
# length at import time (issue #97) -- the compose ${VAR:?} guard alone
# never covered a bare `docker run`/CI, and never checked the value at
# all: an empty-string key used to start fine, letting anyone mint
# tokens for any student_id.


@app.get("/healthz")
def healthz():
    """Health check with database connection verification (issue #6)."""
    conn = None
    try:
        conn = db.get_connection()
        conn.execute("SELECT 1")
        return {"status": "ok"}
    except Exception:
        # issue #62: used to put str(e) straight into the client response,
        # leaking sqlite file paths/internals to an unauthenticated caller.
        # logger.exception (not .error) captures the full traceback
        # server-side -- that's where this detail actually belongs.
        logger.exception("healthz_database_error")
        raise AppError(503, "DATABASE_UNAVAILABLE", "Database unavailable")
    finally:
        # Previously only closed on the success path -- a real connection
        # leak on every failure, found while touching this function for
        # the above.
        if conn is not None:
            conn.close()


# issue #99: one pattern for every student_id this service accepts, so
# POST /session and POST /session/revoke can never drift apart (a looser
# revoke pattern would make "kill this student's link" a way to smuggle
# markup/whitespace past the checks the mint path enforces).
_STUDENT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._@-]*$"


class SessionBody(BaseModel):
    # issue #99: these were unbounded str. An oversized or markup-bearing
    # value that passed the coordinator gate would live forever in the
    # sessions table and in every forensic watermark drawn for the session
    # (the panel/watermark render student_id into DOM text). Real IDs --
    # Moodle logins, Kasm session ids, mint-local-link's STU_LOCAL_TEST --
    # are ASCII-alphanumeric with . _ - @ ; the pattern rejects markup,
    # whitespace and control characters outright, the length bound rejects
    # payload stuffing. 128 (not 64) because the visual-regression harness
    # legitimately mints long per-test ids (VR_<testname>_<variant>).
    # Pydantic's pattern-failure message names the pattern, never the
    # submitted value (test_input_validation.py pins that -- echoing IDs
    # in errors would reintroduce the #93 leak class through the
    # 200-char-truncated log sanitizer).
    student_id: str = Field(max_length=128, pattern=_STUDENT_ID_PATTERN)
    session_id: str = Field(max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")


@app.post("/session")
def create_session(body: SessionBody, x_coordinator_key: str | None = Header(default=None)):
    """Mint a fresh token bound to student_id, the only path that creates
    that binding. Called by scripts/create-session.py right after minting
    the Kasm session itself, not by anything running inside a session."""
    if not x_coordinator_key:
        # same taxonomy rule as the X-Grading-Token endpoints (CR on #121:
        # auth failures answer 401 AUTH_*, never FastAPI's 422).
        raise AppError(401, "AUTH_INVALID_COORDINATOR_KEY", "Missing X-Coordinator-Key header")
    if not secrets.compare_digest(x_coordinator_key, COORDINATOR_KEY):
        # app_error_handler logs every AppError generically (error_code,
        # status_code, path) -- no need to also log here, that would just
        # double-log the same failure.
        raise AppError(401, "AUTH_INVALID_COORDINATOR_KEY", "Invalid coordinator key")

    conn = db.get_connection()
    try:
        now = db.now()
        # Opportunistic cleanup, not a separate cron job -- cheap, and
        # keeps this table from growing unbounded across many sessions.
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        # A student_id has at most one live token: minting a new one
        # revokes whatever token existed before it for that same
        # student_id (e.g. a coordinator re-minting a link they suspect
        # leaked).
        conn.execute("DELETE FROM sessions WHERE student_id = ?", (body.student_id,))

        token = secrets.token_urlsafe(32)
        expires_at = now + db.TOKEN_TTL_SECONDS
        conn.execute(
            "INSERT INTO sessions (token, student_id, session_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token, body.student_id, body.session_id, now, expires_at),
        )
        conn.commit()
        return {"token": token, "expires_at": expires_at}
    finally:
        conn.close()


class SessionRevokeBody(BaseModel):
    """issue #104: `str | None` with no pydantic constraints; the shape checks
    are manual and their messages name the offending field, never its content.

    This is defence in depth, not the primary control (CR on PR #134 asked for
    the wording to say so): since issue #93 `validation_error_handler` answers a
    generic body and logs only sanitized loc/type/msg triples, so a constrained
    field would not leak a token today either. The reason to keep the checks
    manual anyway is that this endpoint's payload is a live credential *by
    design* -- the guarantee should not depend on a handler three layers away
    continuing to sanitize everything, forever.
    """

    token: str | None = None
    student_id: str | None = None


@app.post("/session/revoke")
def revoke_session(body: SessionRevokeBody, x_coordinator_key: str | None = Header(default=None)):
    """Delete a live token instead of waiting out its TTL - the compensation
    POST /session has no equivalent of.

    Called by scripts/create-session.py when the Kasm half of a session
    fails after the token was already minted (issue #104: the token has to
    be minted first, because its value is injected into the Kasm container's
    environment by request_kasm, so the failure order is token-then-Kasm and
    the orphan token used to be left live and usable). Coordinator-only for
    the same reason POST /session is: an endpoint that revokes tokens is
    exactly as dangerous in the wrong hands as one that mints them.

    Selectors: `token` for the caller that holds it, `student_id` as the
    operator's escape hatch ("kill whatever link this student has") and for
    the case where the mint response was never parsed and the token value is
    lost. Revoking an already-gone/expired/unknown token answers 200 with
    revoked=false rather than 404: this endpoint is a compensation step, and
    a caller compensating for its own failed request must not have to
    distinguish "someone revoked it first" from "it never existed".
    """
    if not x_coordinator_key:
        raise AppError(401, "AUTH_INVALID_COORDINATOR_KEY", "Missing X-Coordinator-Key header")
    if not secrets.compare_digest(x_coordinator_key, COORDINATOR_KEY):
        raise AppError(401, "AUTH_INVALID_COORDINATOR_KEY", "Invalid coordinator key")

    if (body.token is None) == (body.student_id is None):
        raise AppError(400, "VALIDATION_REVOKE_SELECTOR", "Provide exactly one of token/student_id")

    # Two explicit statements rather than a built-up WHERE clause: both are
    # parameterized anyway, but this keeps bandit's B608 (and every future
    # reader's "is that f-string user input?" pause) out of a security-
    # critical DELETE.
    conn = db.get_connection()
    try:
        if body.token is not None:
            if len(body.token) > 128:
                raise AppError(400, "VALIDATION_REVOKE_SELECTOR", "token exceeds 128 characters")
            # No pattern on the token: it's opaque secrets.token_urlsafe
            # output, and a non-matching value simply revokes nothing
            # (revoked=false), so there is nothing worth validating.
            cur = conn.execute("DELETE FROM sessions WHERE token = ?", (body.token,))
            selector = "token"
        else:
            if len(body.student_id) > 128 or not re.fullmatch(_STUDENT_ID_PATTERN, body.student_id):
                raise AppError(400, "VALIDATION_REVOKE_SELECTOR", "student_id is not a valid identifier")
            cur = conn.execute("DELETE FROM sessions WHERE student_id = ?", (body.student_id,))
            selector = "student_id"
        conn.commit()
        # Audit trail (CR on PR #134 about issue #104; DoD 40, ties to #98):
        # revocation is a security-relevant action carried out with the coordinator key, and
        # until now it left no server-side trace at all. Which selector was used
        # and how many rows died is the whole point of the line -- the token
        # value is never logged, same rule as every other line in this service
        # (see logging_config.py's docstring; student_id is not logged here
        # either, for the same pseudonymity reason).
        logger.info("session_revoked", extra={"by": selector, "count": cur.rowcount})
        return {"revoked": cur.rowcount > 0, "count": cur.rowcount}
    finally:
        conn.close()


def _resolve_token(conn, token: str) -> str:
    """Every other endpoint's only path to a student_id -- never trust one
    handed in directly by the caller. Returns the student_id a valid,
    unexpired token was minted for; raises 401 otherwise."""
    row = conn.execute("SELECT student_id, expires_at FROM sessions WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise AppError(401, "AUTH_INVALID_TOKEN", "Invalid or unknown token")
    if row["expires_at"] < db.now():
        raise AppError(401, "AUTH_TOKEN_EXPIRED", "Token expired")
    return row["student_id"]


def _get_or_create_progress(conn, student_id: str):
    row = conn.execute("SELECT * FROM progress WHERE student_id = ?", (student_id,)).fetchone()
    if row is None:
        # issue #41: two concurrent requests for the same brand-new
        # student_id (e.g. two rapid GET /case calls right after minting
        # a token) can both see "no row exists" before either INSERT
        # commits -- student_id is the PRIMARY KEY, so the second INSERT
        # then raises sqlite3.IntegrityError. Catch it and fall through to
        # re-SELECTing the row the other request just created, the
        # standard concurrency-safe "insert-or-get" pattern, instead of
        # letting it propagate as an unhandled 500.
        try:
            conn.execute(
                "INSERT INTO progress (student_id, stage, case_order_index, case_assigned_at) "
                "VALUES (?, 'learning', 0, ?)",
                (student_id, db.now()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        row = conn.execute("SELECT * FROM progress WHERE student_id = ?", (student_id,)).fetchone()
    return row


def _get_case(conn, stage: str, order_index: int):
    # issue #96: "the current case" at a position is its HIGHEST version
    # row -- ground-truth changes arrive as new version rows, never as
    # an UPDATE once submissions exist (cases_ground_truth_frozen
    # trigger). With every position at version 1 this is exactly the
    # query the pre-#96 code ran.
    return conn.execute(
        "SELECT * FROM cases WHERE stage = ? AND order_index = ? ORDER BY version DESC LIMIT 1",
        (stage, order_index),
    ).fetchone()


def _advance_progress(conn, student_id: str, stage: str, order_index: int):
    """Move to the next case in this stage, or the next stage, or 'complete'.

    case_assigned_at is re-stamped on every transition (issue #29) -- the
    moment this runs is exactly the moment the next case (if any) becomes
    the student's active one, so this is the only correct place to reset
    the clock.

    Does NOT commit -- issue #60: this used to commit on its own, as a
    separate transaction from its caller's own submission INSERT. The one
    caller (submit()) now commits once, after both writes, so a failure
    here rolls back the submission too instead of leaving it stranded.
    """
    next_case = _get_case(conn, stage, order_index + 1)
    if next_case is not None:
        conn.execute(
            "UPDATE progress SET case_order_index = ?, case_assigned_at = ? WHERE student_id = ?",
            (order_index + 1, db.now(), student_id),
        )
    else:
        stage_idx = db.STAGES.index(stage)
        if stage_idx + 1 < len(db.STAGES):
            next_stage = db.STAGES[stage_idx + 1]
            conn.execute(
                "UPDATE progress SET stage = ?, case_order_index = 0, case_assigned_at = ? WHERE student_id = ?",
                (next_stage, db.now(), student_id),
            )
            logger.info("stage_advanced", extra={"from_stage": stage, "to_stage": next_stage})
        else:
            conn.execute(
                "UPDATE progress SET stage = 'complete', case_order_index = 0, case_assigned_at = ? "
                "WHERE student_id = ?",
                (db.now(), student_id),
            )
            logger.info("training_complete", extra={"from_stage": stage})


@app.get("/case")
def get_case(x_grading_token: str | None = Header(default=None, alias="X-Grading-Token")):
    """issue #94: the token moved from ?token= to a header, everywhere.

    A query-string credential was reproduced verbatim by nginx's access
    log AND its error log (upstream URL) -- two log files holding live
    session tokens per request (audit-confirmed on the running viewer
    container; issues #63/#94). Headers appear in neither. Every client
    of these two GET endpoints is first-party (watermark.html,
    grading-panel.html, custom_startup.sh, the tests) -- there are no
    external integrations to keep query-compatibility for, and tokens
    are hours-lived, so query acceptance is removed outright rather
    than deprecated (a #63 note for the changelog goes in with #100).
    POST /submit and /reset keep the token in the JSON body: a body is
    not logged by nginx, so they were never part of this leak."""
    if not x_grading_token:
        # CR on #121: a required Header() parameter makes FastAPI answer a
        # missing header with 422 VALIDATION_ERROR -- clients and the
        # error taxonomy (#74) expect auth failures as 401 AUTH_*.
        raise AppError(401, "AUTH_INVALID_TOKEN", "Missing X-Grading-Token header")
    conn = db.get_connection()
    try:
        student_id = _resolve_token(conn, x_grading_token)
        progress = _get_or_create_progress(conn, student_id)
        if progress["stage"] == "complete":
            return {"complete": True}

        case = _get_case(conn, progress["stage"], progress["case_order_index"])
        if case is None:
            # A real data-integrity bug (seed data doesn't cover a
            # position progress claims to be at), not a client mistake --
            # the stage/index detail is genuinely useful for diagnosing
            # it, but that's server-side-only information, not something
            # to hand back to whatever's calling /case.
            logger.error(
                "no_case_at_progress_position",
                extra={"stage": progress["stage"], "case_order_index": progress["case_order_index"]},
            )
            raise AppError(500, "SERVER_ERROR", "Internal server error")

        total_in_stage = conn.execute(
            # DISTINCT order_index, not COUNT(*): from issue #96 on, one
            # position can hold several version rows, and what a student
            # sees as "case 3 of 10" counts positions, not schema rows.
            "SELECT COUNT(DISTINCT order_index) FROM cases WHERE stage = ?",
            (progress["stage"],),
        ).fetchone()[0]

        response = {
            "complete": False,
            "case_id": case["id"],
            "stage": case["stage"],
            "title": case["title"],
            "orthanc_study_uid": case["orthanc_study_uid"],
            "position": progress["case_order_index"] + 1,
            "total_in_stage": total_in_stage,
        }
        if case["stage"] in ("assessment", "test"):
            response["category_options"] = db.CATEGORY_LABELS
        return response
    finally:
        conn.close()


class SubmitBody(BaseModel):
    token: str
    case_id: int
    # issue #99: Literal instead of str+max_length. db.STAGES remains the
    # conceptual source of truth -- test_input_validation.py asserts the
    # Literal members match db.STAGES/db.CATEGORY_LABELS so the two
    # can't drift silently. A bad stage used to sail past Pydantic (only
    # bounded to 20 chars) and die later as a 409 stage-mismatch -- now
    # it's a schema-level 422 before any state is consulted.
    stage: Literal["learning", "assessment", "test"]
    # Free-text learning-stage impression -- genuinely open-ended prose, so
    # bounded generously rather than tightly, just to reject unbounded
    # payloads (never trusted for grading either way -- learning is
    # self-assessment only, see this module's docstring).
    text: Optional[str] = Field(default=None, max_length=10_000)
    # Lung-RADS closed vocabulary -- issue #99 Literal (same db-sync test).
    category: Optional[Literal["0", "1", "2", "3", "4A", "4B", "4X"]] = None
    modifier_s: Optional[bool] = None
    # No time_spent_seconds field (issue #29): this data feeds a scientific
    # publication, so time-on-task is computed server-side in submit()
    # from progress.case_assigned_at instead of ever trusting a
    # client-supplied elapsed time -- Date.now() arithmetic in the browser
    # is trivially editable (devtools, or a scripted request bypassing the
    # UI entirely). The frontend currently still sends this field (left
    # over from before this fix) -- harmless, Pydantic drops unrecognized
    # fields by default.


@app.post("/submit")
def submit(body: SubmitBody):
    conn = db.get_connection()
    try:
        student_id = _resolve_token(conn, body.token)
        progress = _get_or_create_progress(conn, student_id)
        if progress["stage"] != body.stage:
            raise AppError(
                409,
                "VALIDATION_STAGE_MISMATCH",
                f"Submission stage '{body.stage}' doesn't match current progress stage '{progress['stage']}'",
            )

        # issue #61: resolve the case from progress's own position, not
        # from whatever case_id the client sent -- the old
        # `SELECT * FROM cases WHERE id = ?` accepted ANY case belonging to
        # the right stage, not only the one actually assigned. A client
        # submitting a later case in the same stage would skip the real
        # current case (never answered), _advance_progress() still moved
        # on from the current index regardless, and when progress later
        # reached the skipped case, its submission already existed from
        # the out-of-order request -- the same "stuck behind a 409"
        # symptom as issue #60. In the assessment stage it would also have
        # handed back the ground truth of a case the student hasn't
        # actually been shown yet.
        case = _get_case(conn, progress["stage"], progress["case_order_index"])
        if case is None:
            # Mirrors GET /case's own handling of this exact failure mode
            # (see that endpoint) -- a real data-integrity bug (seed data
            # doesn't cover progress's current position), not a client
            # mistake.
            logger.error(
                "no_case_at_progress_position",
                extra={"stage": progress["stage"], "case_order_index": progress["case_order_index"]},
            )
            raise AppError(500, "SERVER_ERROR", "Internal server error")

        if body.case_id != case["id"]:
            raise AppError(
                409,
                "VALIDATION_CASE_MISMATCH",
                "case_id doesn't match the currently assigned case",
            )

        # issue #29: time-on-task computed server-side from when this case
        # actually became active (case_assigned_at, stamped by
        # _get_or_create_progress()/_advance_progress()), never from a
        # client-supplied value -- see SubmitBody's own comment for why.
        # issue #44: clamped to zero in case the server clock is ever
        # adjusted backwards (e.g. an NTP correction) between assignment
        # and submission, which would otherwise land a negative value in
        # submissions and skew time-on-task data for that row.
        time_spent_seconds = max(0, db.now() - progress["case_assigned_at"])

        # Correctness is category-only: the modifier is recorded for later
        # analysis but doesn't affect scoring -- matches the original design
        # discussion's framing of categorical comparison as the primary metric.
        is_correct = None
        if body.stage in ("assessment", "test"):
            is_correct = 1 if body.category == case["ground_truth_category"] else 0

        # issue #28: the stage check above and this INSERT are two separate
        # steps with no locking between them -- a double-click or a
        # scripted rapid-fire request could pass the check twice before
        # either write lands. UNIQUE(student_id, case_id, stage) (see
        # db.py) turns that race into a clean, guaranteed-consistent
        # IntegrityError here rather than silently creating duplicate
        # submissions that would skew /results accuracy numbers.
        # issue #60: the submission INSERT and _advance_progress()'s own
        # UPDATE(s) must land as ONE transaction -- previously each
        # committed separately, so a failure between them (most likely in
        # practice: SQLite writer-lock contention under concurrent
        # submissions near a shared stage deadline, since db.get_connection()
        # sets no PRAGMA busy_timeout) left the submission durably stored
        # but progress never advanced. Every retry then hit
        # UNIQUE(student_id, case_id, stage) and returned 409 forever, with
        # no way for the student to move on. See the issue's own comment
        # thread for the full failure-mode writeup.
        #
        # Neither statement below calls conn.commit() itself -- one commit
        # at the very end of this block covers both, and any exception from
        # either rolls back everything, not just the statement that failed.
        try:
            conn.execute(
                """INSERT INTO submissions
                   (student_id, case_id, stage, submitted_category, submitted_modifier_s,
                    submitted_text, is_correct, time_spent_seconds, submitted_at,
                    ground_truth_category, ground_truth_modifier_s, case_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    student_id,
                    body.case_id,
                    body.stage,
                    body.category,
                    None if body.modifier_s is None else int(body.modifier_s),
                    body.text,
                    is_correct,
                    time_spent_seconds,
                    db.now(),
                    # issue #96: freeze the ground truth -- and which
                    # version row it came from -- into the same INSERT as
                    # is_correct itself: snapshot and score derive from the
                    # same `case` row, same transaction, so they can never
                    # disagree later. gt_backfilled keeps its default 0:
                    # this IS a proven snapshot.
                    case["ground_truth_category"],
                    case["ground_truth_modifier_s"],
                    case["version"],
                ),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
            raise AppError(409, "DUPLICATE_SUBMISSION", "This case/stage was already submitted")

        # No student_id/submitted content here on purpose (see
        # logging_config.py's own comment) -- stage/is_correct are just
        # aggregate/class-label facts, not anything identifying.
        logger.info("submission_recorded", extra={"stage": body.stage, "is_correct": is_correct})

        try:
            _advance_progress(conn, student_id, progress["stage"], progress["case_order_index"])
        except Exception:
            conn.rollback()
            raise

        conn.commit()

        if body.stage == "learning":
            return {"reference_report": case["reference_report"]}
        elif body.stage == "assessment":
            return {
                "correct": bool(is_correct),
                "ground_truth_category": case["ground_truth_category"],
                "ground_truth_modifier_s": bool(case["ground_truth_modifier_s"]),
            }
        else:  # test: no reveal
            return {}
    finally:
        conn.close()


class ResetBody(BaseModel):
    token: str


@app.post("/reset")
def reset(body: ResetBody):
    """Start over from the beginning. There's no separate 'main screen' in
    this single-page kiosk app (viewer + grading panel) -- once a student
    reaches 'complete', this is the way back to a fresh learning-stage case.
    Clears past submissions too (not just progress), so a repeat run
    doesn't leave stale rows alongside the new ones and skew /results.

    Blocked entirely during the test stage (issue #27): with no guard here,
    a student partway through the graded exam who doesn't like how it's
    going could just reset and retry with a case sequence they now
    remember -- a real cheating vector, not a hypothetical one, since
    /submit never reveals anything during "test" but the student still
    sees which case comes next. learning/assessment/complete are
    unaffected -- neither stage is graded, and 'complete' is the
    documented, legitimate way to start a fresh run."""
    if not RESET_ENABLED:
        # issue #98: /reset deletes recorded answers. Off unless GRADING_RESET_ENABLED=1 (local
        # testing). Checked first, but a valid token is still required to learn anything.
        conn = db.get_connection()
        try:
            _resolve_token(conn, body.token)
        finally:
            conn.close()
        raise AppError(403, "RESET_DISABLED", "Starting over is disabled; ask the coordinator for a new session")
    conn = db.get_connection()
    try:
        student_id = _resolve_token(conn, body.token)
        progress = _get_or_create_progress(conn, student_id)
        if progress["stage"] == "test":
            raise AppError(403, "RESET_BLOCKED_DURING_TEST", "Cannot reset during the test stage")
        conn.execute("DELETE FROM progress WHERE student_id = ?", (student_id,))
        conn.execute("DELETE FROM submissions WHERE student_id = ?", (student_id,))
        conn.commit()
        return {"reset": True}
    finally:
        conn.close()


def _answer_rows(conn, student_id: str):
    """All of the student's submissions, ordered learning -> assessment -> test and by case
    position. Reads the frozen snapshot columns (issue #96) plus the case row the submission
    points at (case_id is the exact version row, which is frozen once answered)."""
    return conn.execute(
        """SELECT s.stage, s.submitted_category, s.submitted_modifier_s, s.submitted_text,
                   s.is_correct, s.ground_truth_category, s.ground_truth_modifier_s,
                   c.order_index, c.title, c.reference_report,
                   c.ground_truth_category AS case_category, c.ground_truth_modifier_s AS case_modifier_s
            FROM submissions s JOIN cases c ON c.id = s.case_id
            WHERE s.student_id = ?
            ORDER BY CASE s.stage WHEN 'learning' THEN 0 WHEN 'assessment' THEN 1 ELSE 2 END,
                     c.order_index""",
        (student_id,),
    ).fetchall()


def _numbered(rows):
    """(position within the stage starting at 1, total in the stage, row), in order."""
    totals: dict = {}
    for r in rows:
        totals[r["stage"]] = totals.get(r["stage"], 0) + 1
    seen: dict = {}
    for r in rows:
        seen[r["stage"]] = seen.get(r["stage"], 0) + 1
        yield seen[r["stage"]], totals[r["stage"]], r


def _answer_of(r):
    if r["stage"] == "learning":
        return {"text": r["submitted_text"]}
    return {"category": r["submitted_category"], "modifier_s": bool(r["submitted_modifier_s"])}


@app.get("/review")
def review(x_grading_token: str | None = Header(default=None, alias="X-Grading-Token")):
    """issue #202: the end-of-exam summary of the doctor's OWN answers (all three stages,
    read-only). Contains no ground truth, no reference report and no verdict: those come from
    /results, and only after POST /finalize. Available once the exam is complete, and stays
    available afterwards."""
    if not x_grading_token:
        raise AppError(401, "AUTH_INVALID_TOKEN", "Missing X-Grading-Token header")
    conn = db.get_connection()
    try:
        student_id = _resolve_token(conn, x_grading_token)
        progress = _get_or_create_progress(conn, student_id)
        if progress["stage"] != "complete":
            return {"complete": False}
        items = [
            {
                "stage": r["stage"],
                "position": pos,
                "total_in_stage": total,
                "title": r["title"],
                "answer": _answer_of(r),
            }
            for pos, total, r in _numbered(_answer_rows(conn, student_id))
        ]
        return {
            "complete": True,
            "finalized": progress["finalized_at"] is not None,
            "reset_enabled": RESET_ENABLED,
            "items": items,
        }
    finally:
        conn.close()


class FinalizeBody(BaseModel):
    token: str


@app.post("/finalize")
def finalize(body: FinalizeBody):
    """issue #202: the doctor approves the end-of-exam review. One-way and idempotent: the first
    call records the moment, later calls change nothing. Refused (409) until the exam is complete.
    After this, GET /results reveals the correct answers and the score."""
    conn = db.get_connection()
    try:
        student_id = _resolve_token(conn, body.token)
        progress = _get_or_create_progress(conn, student_id)
        if progress["stage"] != "complete":
            raise AppError(409, "FINALIZE_NOT_COMPLETE", "The exam is not complete yet")
        if progress["finalized_at"] is not None:
            return {"finalized": True, "already_finalized": True}
        conn.execute(
            "UPDATE progress SET finalized_at = ? WHERE student_id = ? AND finalized_at IS NULL",
            (db.now(), student_id),
        )
        conn.commit()
        logger.info("exam_finalized")
        return {"finalized": True, "already_finalized": False}
    finally:
        conn.close()


@app.get("/results")
def results(x_grading_token: str | None = Header(default=None, alias="X-Grading-Token")):
    if not x_grading_token:
        # CR on #121: 401 AUTH_INVALID_TOKEN, not FastAPI's 422 (see /case).
        raise AppError(401, "AUTH_INVALID_TOKEN", "Missing X-Grading-Token header")
    conn = db.get_connection()
    try:
        student_id = _resolve_token(conn, x_grading_token)
        progress = _get_or_create_progress(conn, student_id)
        if progress["stage"] != "complete":
            return {"complete": False}
        if progress["finalized_at"] is None:
            # issue #202: nothing about the correct answers or the score before the doctor has
            # approved the review (POST /finalize). Before this, the test ground truth was
            # returned as soon as the exam was complete; the panel just did not show it.
            return {"complete": True, "finalized": False}

        # issue #96: reads ONLY the submission's own frozen snapshot for the graded stages --
        # a later ground-truth change is a new case version and cannot alter these rows.
        rows = _answer_rows(conn, student_id)
        test_rows = [r for r in rows if r["stage"] == "test"]
        total = len(test_rows)
        correct = sum(1 for r in test_rows if r["is_correct"])
        breakdown = [
            {
                "ground_truth": r["ground_truth_category"],
                "submitted": r["submitted_category"],
                "correct": bool(r["is_correct"]),
            }
            for r in test_rows
        ]
        items = []
        for pos, stage_total, r in _numbered(rows):
            graded = r["stage"] != "learning"
            # graded stages: the frozen snapshot; learning has none, so the case row (reference only)
            category = r["ground_truth_category"] if graded and r["ground_truth_category"] else r["case_category"]
            modifier = r["ground_truth_modifier_s"] if graded and r["ground_truth_category"] else r["case_modifier_s"]
            items.append(
                {
                    "stage": r["stage"],
                    "position": pos,
                    "total_in_stage": stage_total,
                    "title": r["title"],
                    "answer": _answer_of(r),
                    "ground_truth_category": category,
                    "ground_truth_modifier_s": bool(modifier),
                    "correct": bool(r["is_correct"]) if graded and r["is_correct"] is not None else None,
                    "reference_report": r["reference_report"],
                }
            )
        return {
            "complete": True,
            "finalized": True,
            "test_total": total,
            "test_correct": correct,
            "accuracy": (correct / total) if total else None,
            "breakdown": breakdown,
            "reset_enabled": RESET_ENABLED,
            "items": items,
        }
    finally:
        conn.close()


# ---- SPIKE (issue #195, item 1): per-session scoping of the Orthanc proxy -----------
# nginx (:8043) calls this through auth_request before forwarding any /s/<key>/... request
# to Orthanc. <key> is sha256(grading token) in hex: one-way, so a key that leaks through
# an nginx error log (which prints the request line) does NOT reveal the grading token and
# cannot be used for /submit; it only lets the holder READ the session's current study.
import hashlib  # noqa: E402

_KEY_PATH_RE = re.compile(r"^/s/([0-9a-f]{64})(/dicom-web/[^?]*)?(?:\?(.*))?$")
_UID = r"[0-9]+(?:\.[0-9]+)*"


def _study_path_re(uid: str):
    u = re.escape(uid)
    return re.compile(
        rf"^/dicom-web/studies/{u}"
        rf"(?:/(?:series|metadata|thumbnail|rendered)"
        rf"(?:/{_UID}(?:/(?:metadata|thumbnail|rendered|instances(?:/{_UID}(?:/(?:metadata|thumbnail|rendered|frames/[0-9,]+))?)?))?)?)?$"
    )


@app.get("/orthanc-authz")
def orthanc_authz(x_original_uri: str | None = Header(default=None, alias="X-Original-URI")):
    """204 = allow, 401 = unknown/expired key, 403 = not this session's current study."""
    m = _KEY_PATH_RE.match(x_original_uri or "")
    if not m:
        raise AppError(403, "AUTHZ_BAD_PATH", "Not a scoped Orthanc path")
    key, path, query = m.group(1), m.group(2) or "", m.group(3) or ""
    conn = db.get_connection()
    try:
        student_id = None
        for row in conn.execute("SELECT token, student_id FROM sessions WHERE expires_at >= ?", (db.now(),)):
            if hashlib.sha256(row["token"].encode()).hexdigest() == key:
                student_id = row["student_id"]
                break
        if student_id is None:
            raise AppError(401, "AUTHZ_INVALID_KEY", "Unknown or expired key")
        progress = _get_or_create_progress(conn, student_id)
        if progress["stage"] == "complete":
            raise AppError(403, "AUTHZ_NO_STUDY", "No study assigned")
        case = _get_case(conn, progress["stage"], progress["case_order_index"])
        if case is None:
            raise AppError(403, "AUTHZ_NO_STUDY", "No study assigned")
        uid = case["orthanc_study_uid"]
    finally:
        conn.close()
    if path == "/dicom-web/studies":
        # QIDO search: only a query that names THIS study and nothing broader.
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if params.get("StudyInstanceUID") != uid and params.get("0020000D") != uid:
            raise AppError(403, "AUTHZ_FORBIDDEN", "Search must name the assigned study")
        return Response(status_code=204)
    if _study_path_re(uid).match(path):
        return Response(status_code=204)
    raise AppError(403, "AUTHZ_FORBIDDEN", "Path is outside the assigned study")
