# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
the project adheres to [Semantic Versioning](https://semver.org/spec/v2-0-0/).

## Versioning policy (issue #100)

- The API's advertised version is `API_VERSION` in
  `docker/grading-api/app/main.py` — the single source of truth, reported
  through FastAPI. It is bumped in the **same commit** that adds the
  matching entry below; a matching git tag is cut at release time.
- The REST surface intentionally has **no `/v1` path prefix**: it is an
  internal contract consumed only by our own Kasm/Guacamole launchers and
  viewer pages (never a public API), every endpoint is covered by the
  integration suite, and breaking changes ship paired with those clients
  in the same deployment. Revisit path-versioning before ever exposing
  this API to third-party consumers.
- `/docs`, `/redoc` and `/openapi.json` are disabled at the application
  (issue #100): nginx proxies `/api/` straight to the app, so anything
  enabled app-side is exposed at the public edge. Need the schema offline?
  `app.openapi()` still builds it in-process.

## [Unreleased]

- **Degraded modes documented and pinned by tests (issue #115).**
  `docs/DEGRADED_MODES.md` records, per critical dependency (grading-api,
  SQLite lock, Orthanc, Kasm/Guacamole incl. the CE 5-session cap, the
  non-existent Redis, nginx rate limit), the symptom, the student-visible
  behaviour and the data behaviour - with the load numbers measured
  against the live stack (60-request burst: 21x200/39x429/zero 5xx;
  write under a held exclusive lock: 500 after busy_timeout 5.0s), plus
  the "what saturates first" order at 50/300 users. `scripts/test-degraded-modes.sh`
  (CI job `degraded-test`) pins the four testable rows; the documented
  gaps (blank Orthanc iframe, no Retry-After on 429) are pinned as
  documented so a future fix must update the doc deliberately.

- **Follow-ups to #160 (findings N1/N2/N3/S1 from its review).**
  N1: startup configuration failures (pre-migration backup, cases-table
  migration, malformed GRADING_CASES_FILE) now abort like config.py -- one
  clean stderr line, exit code 2; previously a bare SystemExit(str) raised
  inside lifespan() resurfaced through uvicorn's task group as a
  BaseExceptionGroup traceback with exit code 1. N2:
  dicom-patient-consistency.py now REFUSES images that carry no
  StudyInstanceUID instead of collapsing them into one pseudo-study whose
  majority label would relabel unrelated studies. N3: cases-file study UIDs
  must end in a digit -- dots-only (".") and trailing-dot UIDs are rejected
  (the leading-dot quirk of the received exports, issue #162, stays
  tolerated on purpose). S1: new scripts/verify-cases-in-orthanc.py
  re-checks after loading that every cases-file study is actually served by
  Orthanc -- counts + stage/order codes only, duplicate study records
  (the #162 split signature) reported as a warning. API_VERSION unchanged
  (no API-surface change; startup exit code is operator-facing, reviewer to
  rule if it counts).

- **Reload no longer strands the student on HTTP 401 (issue #145).** `watermark.html` and `grading-panel.html` keep the
  token in `sessionStorage` (this tab only, gone when it closes, never sent anywhere) after reading the URL
  fragment, so F5 / Ctrl+R / a restored tab keep working; the fragment is still erased from the address bar (#94).
  Order: fragment -> sessionStorage -> none. No token, or a 401 from grading-api, shows the owner-approved message
  ("Sesja wygasla lub link jest nieprawidlowy...") with no retry button and no automatic refresh. 22 new Playwright
  tests (`test_session_token.py`) + 1 new visual snapshot per page; the token still never appears in a request URL.
- **Real study data, local only (issue #87).** `GRADING_CASES_FILE` seeds the cases from a local
  git-ignored JSON (answer key, reports, study UIDs never reach the public repo); new
  `scripts/build-cases-file.py` (spreadsheet + DICOM headers -> cases file), `scripts/load-local-studies.sh`
  (parallel, retrying, counts-only Orthanc loader with `--skip-list`) and
  `scripts/dicom-patient-consistency.py` (detects/repairs, as copies, studies whose images carry
  more than one PatientID, which Orthanc would split into duplicated series). `./local/` is mounted
  read-only into grading-api; `local/*` and the data folders are git-ignored.
- **Configuration as code, part 1 (issue #91).** New `docs/CONFIG.md` is the
  hand-maintained source of truth for every environment variable (default,
  required, sensitive, `klasa`, validation rule, reading site). `.env.example`
  gained the previously missing user-side variables (`BIND_ADDR`,
  `GRADING_TOKEN_TTL_SECONDS`, `GRADING_DB_PATH`, `TLS_CA_BUNDLE`, the
  create-session/Guacamole operator blocks). The CI gate that keeps this
  table in sync (`scripts/check-config-surface.py`) follows in the next PR.
- **CI gate: `needs`-list drift guard + robust entry types (issue #135).** `scripts/ci-gate.py`
  now parses the job ids out of `.github/workflows/ci.yml` (stdlib regex over the `jobs:`
  block, no PyYAML) and exits 2 when `NEEDS_JSON` and the workflow disagree in either
  direction -- the drift vector that could re-open the #124 class of bug (a job added without
  `needs:` staying invisible to `ci-success`). Malformed entry values (e.g. `{"lint":"success"}`)
  now print a clean `<job>: invalid-entry` line and block instead of raising `AttributeError`;
  `"result": null` is reported as `missing` like an absent result. Gate stays fail-closed:
  missing/unreadable workflow file or a `jobs:` block that cannot be found also refuse to pass.

- **Kiosk window-keeping for the Weasis workspace (issue #151).** `arrange_windows.sh` now
  (a) sizes both windows by their OUTER frame (they used to overflow the screen by the
  title-bar height and overlap by the side borders), (b) removes the title-bar
  minimise/maximise/shade buttons, (c) restores a minimised window on the next pass (there
  is no taskbar; Alt+F9 used to hide both windows for good), (d) relaunches the grading
  panel if it is closed (never during the startup race), and (e) relaunches a closed
  Weasis on the same study, at most 5 times and never within 20 s of the last relaunch.
  The panel window has a fixed width. Found by manual testing in a live Kasm session.
- **Watermark overlay: picom compositor + adaptive density (flicker fix, issue #150).**
  The Weasis workspace image now installs `picom` and `custom_startup.sh` runs it
  (supervised); `overlay.py` is a genuinely transparent ARGB window when a compositor
  is active and falls back to the glyph shape (adaptive tile gap, default 3 tiles per
  screen via `WATERMARK_TILES_PER_SCREEN`) when it is not, so a dead picom never turns
  the viewer black. Measured flashes per 3 min in live sessions: 7 (shape, 1 s refresh),
  3 (shape, 15 s), 0 (picom). Visible effect: fewer, wider-spaced marks (was 8+).
  New `scripts/test-overlay-shape.sh` and `scripts/test-overlay-compositor.sh` + CI jobs
  `overlay-shape-test` / `overlay-compositor-test`.

- **Watermark watchdog: backoff ceiling lowered 60s -> 5s** (issue #156).
  The #148 exponential backoff let a student who repeatedly kills the
  overlay earn up to a minute without the mandatory forensic watermark.
  The shipped default of `WATCHDOG_BACKOFF_MAX` is now 5s (still tunable
  via env); crash-loop suppression and log rotation are unchanged. A BATS
  test reads the default out of the script itself, so raising it again
  fails CI instead of silently regressing the control.

### Breaking (client contract)
- **Session tokens no longer travel in URLs** (issue #94). `GET /case`
  and `GET /results` authenticate via the `X-Grading-Token` header; the
  viewer pages read the token from the URL `#token=` fragment and strip
  it from the address bar immediately; the Kasm/Guacamole launchers pass
  tokens to curl over stdin so they never appear in process listings.
  Any query string on `/api/*` is now rejected with `422` at the nginx
  edge. Minted links from before this change **do not work**.
  - **Deployment runbook**: roll out with active sessions torn down, not
    mid-sitting. Before deploying this build, end every in-flight Kasm
    session (admin console) and re-mint links via
    `scripts/create-session.py`; students reopening an old link will get
    a hard `422` at the edge, and any session whose page still holds the
    old `?token=` link will fail its next `/case` call. For the
    Guacamole PoC, `scripts/provision-guacamole-session.py` re-mints
    against a freshly provisioned connection the same way.

### Breaking (integrator-visible validation)
- `student_id`/`session_id` must match `^[A-Za-z0-9][A-Za-z0-9._@-]*$`
  with a 128-character bound, and `stage`/`category` are closed
  vocabularies (Lung-RADS `0,1,2,3,4A,4B,4X`) at the schema boundary
  (issue #99). Payloads that previously trickled through as `409`s (or
  worse, were stored) are now `422 VALIDATION_ERROR` before any state
  is consulted. Error responses and log lines never echo the rejected
  value.

### Added
- `POST /session/revoke` (issue #104) — coordinator-gated like `POST /session`.
  Takes exactly one of `token`/`student_id` and answers
  `200 {"revoked": bool, "count": n}`; an unknown or already-revoked selector is
  `revoked: false`, deliberately **not** 404, because the caller uses this as an
  idempotent compensation step and must not have to distinguish "someone got
  there first" from "never existed". Rejections never echo the submitted token.
  Every revocation logs `session_revoked` server-side with the selector kind and
  the row count — never the credential, never the `student_id` either — because a
  coordinator-keyed security action that leaves no trace cannot be audited
  (DoD 40).
  Additive endpoint, nothing existing changed → advertised API version
  `1.0.0` → `1.1.0`.
- `scripts/create-session.py --ca-bundle` / `TLS_CA_BUNDLE` (issue #104) —
  appends the named CA to the system trust store; verification stays on. The
  flag is added to the store rather than replacing it, because Kasm's gateway
  and `viewer`'s `/api/` proxy routinely have two different issuers.
  `--insecure` still works for a self-signed lab instance, now warns on every
  run, and is refused together with `--ca-bundle` instead of silently winning.
  **No change for invocations that never passed `--insecure`**: verifying was
  already their effective behavior.

### Changed
- Submitting against a stage that does not match the student's assigned
  case answers `409 VALIDATION_CASE_MISMATCH` — previously `400`. The
  grading panel reacts by auto-resyncing to the current case (issue #83;
  this is the entry the Tier-4 audit asked for).
- A case's ground truth is frozen once any submission exists; content
  changes ship as a new `cases.version` row instead of an `UPDATE`
  (issue #96). Fresh databases also carry SQLite `CHECK` constraints on
  the stage/category vocabularies (issue #99); existing databases are
  not rebuilt for them.
- When the grading-api is unreachable at session start, the viewer
  launches on the plain study and shows a retryable error — it no longer
  silently substitutes a fake "complete" payload (issue #102).
- `/docs`, `/redoc`, `/openapi.json` disabled app-side; API version made
  explicit (`1.0.0`) instead of FastAPI's stock default (issue #100).

### Fixed
- The watermark-overlay watchdog no longer grows `/tmp/watermark-overlay.log`
  without bound or relaunches a crash-looping overlay once per second
  forever (issue #66, DoD P91). First relaunch after a death is still 1 s
  (the deterrence promise unchanged); consecutive fast crashes back off
  1s → 2s → 4s … up to 60 s, an overlay that stays up resets the cadence,
  the log rotates past 1 MB keeping its tail, and every line carries the
  cumulative restart count. All knobs are the same test-seam convention as
  `OVERLAY_SCRIPT`/`WATCHDOG_LOG`; production sets none of them.
- Errors from `scripts/create-session.py`'s Kasm calls name Kasm again. When
  `api_call` grew a `label` for the two services it talks to, its default became
  the generic `"API"` and neither `request_kasm` nor `get_kasm_status` passed
  one, so a Kasm failure degraded from "Kasm API error calling
  /api/public/request_kasm" to "API error calling ..." (nit 1 of PR #134's
  review). `label` is now required and keyword-only, so a call site that omits
  it fails as a `TypeError` in the suite instead of printing a vaguer line in
  front of an operator. No `API_VERSION` change: this is operator-facing text in
  a launcher script, not the REST contract.
- A failed `scripts/create-session.py` run no longer leaves a live grading token
  behind (issue #104). The token has to be minted before `request_kasm` — its
  value is injected into that container's environment by that very call — so any
  failure in the Kasm step used to orphan it, usable until its TTL expired. It
  now revokes through `POST /api/session/revoke` on every failure before Kasm
  answers (HTTP error, refused connection, a 200 without `kasm_id`, Ctrl-C,
  SIGTERM, SIGHUP), and prints the undo instructions by `student_id`/`session_id`
  rather than the credential. Once Kasm has answered it does **not** revoke: the
  token is then a live session's credential, and killing it would break a
  student's session to tidy up a bookkeeping row. A signal inherited as ignored
  (`nohup`, `trap '' HUP`) is left ignored, so backgrounded runs keep surviving
  their terminal.
  Review record (DoD 19/45): `docs/history/issue-104-review.md` — the CR on
  PR #134 verbatim plus the two pre-PR review rounds, including their mutation
  tables (which of these guarantees are actually pinned by a test, and which
  tests were shown to fail when the guard was removed).

### Security hardening (no contract change for compliant clients)
- API access logs are structured JSON with tokens/credentials scrubbed,
  and validation/500 responses are generic bodies that never echo
  request payloads (issues #93, #95).
- Startup refuses weak/missing coordinator keys and out-of-range token
  TTLs instead of falling back to defaults (issue #97).
