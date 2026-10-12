# DICOM Viewer — MVP

Minimal, runnable proof-of-concept for the IP_CMC Lung-RADS training platform.
See `CLAUDE.md` for the hard rules this project must never violate.

(This README occasionally refers to "the original design discussion" — PM
correspondence and radiology requirements gathered during scoping. That
material is kept local-only and gitignored, never shipped in this repo, so a
fresh clone won't have it; nothing below depends on it being present.)

**Scope of this MVP** (deliberately narrow — see "What's NOT in this MVP"
below): a DICOM store with a couple of sample studies, a forensic watermark
baked into every session, served through a container that's meant to sit
behind Kasm Workspaces so each session comes from its own unique,
individually-issued link. No Moodle, no payments, no stratified sampling yet.

**Setting up your own copy?** See `docs/LOCAL_SETUP_GUIDE.md` for a
complete, step-by-step walkthrough (no assumed context) — the sections
below are more of a technical decisions log than an onboarding doc.
**Just want the shape of it?** See `docs/ARCHITECTURE.md` for current-state
Mermaid diagrams (session flow + deployment topology — those cover the
Chrome+Orthanc flow described below; they predate the Weasis flow and
haven't been redrawn for it yet).

## Two viewer flows, side by side

This project didn't replace Orthanc with Weasis — **Orthanc is still the
DICOM store and DICOMweb server behind both flows.** What changed is which
*viewer* a student's Kasm session actually shows them, after PM feedback
wanted something closer to the native desktop viewers
(Horos/Weasis) radiologists already know, instead of Orthanc's own web UI.
Both flows are real, both are registerable in Kasm at the same time, and
both talk to the same `grading-api` for the Lung-RADS mechanics:

| | **Chrome flow** (original MVP) | **Weasis flow** (in progress) |
|---|---|---|
| Workspace image | `docker/kasm-workspace/` | `docker/kasm-workspace-weasis/` |
| What the student sees | Chrome, kiosk mode, one page: Orthanc's web viewer in an iframe + the grading panel as a sidebar | Weasis, a real native DICOM viewer window + the grading panel as its own small browser window beside it |
| Frontend page(s) | `docker/viewer/watermark.html` (iframe + sidebar, one page) | `docker/viewer/grading-panel.html` (panel only — Weasis shows the images itself) |
| Watermark | DOM-based, `mix-blend-mode:difference` for guaranteed contrast (browser-only, one page) | Native GTK overlay (`overlay.py` + `watchdog.sh`) covering the whole screen, since Weasis is a separate window a page-based watermark could never reach |
| Status | Done, this is what's actually deployed today | Milestone "Weasis viewer migration", GitHub issues #3–#8 — see below for exactly what's shipped vs. still open |

Nothing about `orthanc`, `grading-api`, or the local dev quick-start below
changes between the two — only which workspace image you register in Kasm,
and which frontend page that image's `custom_startup.sh` points at.

## Real study data (local only)

Real (radiologist-supplied, pseudonymised) studies are **never committed**: the GitHub
repo is public, and the cases carry the exam's answer key, clinical reference reports and
real study UIDs. Everything below stays on this host (issue #87); `Dokumentacja/DICOM_Images`,
`Dokumentacja/Opisy_badan` and `./local/*` are git-ignored.

1. **Check consistency first** (counts only, nothing printed from the files):
   `python3 scripts/dicom-patient-consistency.py Dokumentacja/DICOM_Images`.
   Orthanc groups by PatientID, so a study whose images disagree on the patient is split
   into duplicated, truncated series (the first batch had this in 2 of 5 studies, 18 of
   9,155 images). `--fix-out local/fixed` writes corrected COPIES of only those images (the
   originals are never touched) and refuses if the odd images could be somebody else's.
2. **Load into Orthanc**: `./scripts/load-local-studies.sh Dokumentacja/DICOM_Images --skip-list local/fixed/skip.txt`
   then `./scripts/load-local-studies.sh local/fixed`. Parallel, retrying, idempotent,
   prints counts only (a shared Docker host answered ~1% of parallel uploads with resets; re-run until `0 failed`).
3. **Build the cases file**: `python3 scripts/build-cases-file.py --xlsx Dokumentacja/Opisy_badan/<file>.xlsx --dicom-dir Dokumentacja/DICOM_Images --out local/cases.json`
   (columns `Anonim | Opis | Lung-Rads`; `--stages` maps studies to learning/assessment/test,
   default: all studies in learning, the first in assessment and test). Studies are matched to
   spreadsheet rows by their majority PatientName/PatientID; UIDs are read from the headers.
4. **Point grading-api at it**: `GRADING_CASES_FILE=/local/cases.json` in `.env`, then start
   the stack with an EMPTY `grading-db` volume (`docker compose down -v`): the file is only read while the
   cases table is empty (otherwise a `cases_file_ignored` warning is logged; change an
   answered case with `scripts/new-case-version.py`). A malformed file stops startup with a message
   naming the entry and field, never a value.

Known quirks of the first batch (all handled above): every Study/Series/SOP UID has a stripped
root and starts with a dot (e.g. `.123456.654321`; Orthanc, DICOMweb and Weasis cope), there is
no DeidentificationMethod declaration in the files, and dates/age are present. The
anonymisation sign-off (issue #87) is the radiologist's written statement, not these checks.

## Quick start (local dev, no Kasm needed)

This is the fastest way to poke at `grading-api`/the grading UI without
installing Kasm at all — it exercises the same `orthanc` + `grading-api` +
`viewer` stack either flow uses, just through a plain browser tab instead of
a Kasm session.

```bash
cp .env.example .env
sed -i "s/^ORTHANC_PASSWORD=.*/ORTHANC_PASSWORD=$(openssl rand -hex 16)/" .env
sed -i "s/^GRADING_COORDINATOR_KEY=.*/GRADING_COORDINATOR_KEY=$(openssl rand -hex 32)/" .env
docker compose up -d --build
./scripts/fetch-public-samples.sh   # pulls in BRAINIX (~64MB, not committed to git)
./scripts/load-sample-studies.sh
```
Both `sed` lines are required — a real tester following an earlier version
of this section that only set `ORTHANC_PASSWORD` hit
`docker-compose.yml`'s own `GRADING_COORDINATOR_KEY:?...` guard failing
`docker compose up`, which cascaded into `load-sample-studies.sh` failing
too (nothing was actually running yet).

> **BREAKING (issue #97):** `GRADING_COORDINATOR_KEY` must now be at least
> 32 characters, contain no leading/trailing whitespace, and be set in
> every launch path (not just compose) — grading-api exits at startup
> otherwise with a `CONFIGURATION ERROR` naming the variable. Deployments
> that used a shorter key stop starting until it's regenerated
> (`openssl rand -hex 32`). `GRADING_TOKEN_TTL_SECONDS`, if set, must be
> an integer between 1 and 604800 (7 days).

```
┌──────────────┐      ┌───────────────────────┐      ┌─────────────┐
│  Your browser│ ───► │ viewer (nginx:8080)   │ ───► │  orthanc    │
│  (stand-in   │      │ watermark.html +      │      │  (DICOM     │
│  for Kasm's  │      │ grading panel +       │      │  store,     │
│  streamed    │      │ reverse proxy         │      │  internal   │
│  session)    │      └───────────┬───────────┘      │  only)      │
└──────────────┘                  │                  └─────────────┘
                                   ▼
                       ┌───────────────────────┐
                       │  grading-api          │
                       │  (FastAPI + SQLite,   │
                       │  internal only)       │
                       └───────────────────────┘
```

**Reloading the page (issue #145):** the token is read from the `#token=` fragment, erased from the
address bar, and kept in `sessionStorage` (this tab only; cleared when the tab closes; never sent to any
server). F5 / Ctrl+R / a restored tab therefore keep the session; opening the page with no token, or after
grading-api rejected it (unknown or expired), shows "Sesja wygasła lub link jest nieprawidłowy…" and waits
for a new link (no retry button, no automatic refresh). `localStorage` and cookies are deliberately not used.

**Opening bare `http://localhost:8080/` no longer works** — since the
session-token system shipped (see "Lung-RADS grading" below),
`watermark.html`'s own `/api/case` fetch has nothing to authenticate with
and unconditionally 401s, showing "Błąd ładowania: HTTP 401: Unauthorized"
in the panel instead of a case. This bit a real tester cold; the fix is to
mint a token first:

```bash
./scripts/mint-local-link.sh
```

This mints a `grading-api` session token (gated by `GRADING_COORDINATOR_KEY`
from `.env`, the same mechanism `scripts/create-session.py` uses for real
Kasm sessions) and prints a ready-to-open link, e.g.
`http://localhost:8080/?student_id=STU_LOCAL_TEST&session_id=sess_...#token=...` (issue #94: the token is a URL *fragment* -- the browser never transmits it, so it can't reach nginx logs).
Open that link — you should see the watermarked viewer wrapper
(`watermark.html`, the Chrome-flow page) with a rotating
`STUDENT_ID | SESSION_ID | timestamp` overlay, loading Orthanc Explorer 2
(browse to a study, then open it in the Stone Web Viewer) inside it.

Optional student/session IDs: `./scripts/mint-local-link.sh STU_12345
SESS_9921A3B`. The link's `token` is what actually authenticates every
`grading-api` call now — `student_id`/`session_id` in the URL are display-only
(the watermark text), same as in a real Kasm session. The link also carries
`orthanc_url` (default `http://localhost:8043/` for this local setup — the
auth-injecting proxy, not Orthanc's own port, see below) so the wrapper
knows which origin to iframe.

Want to see the Weasis-flow page (`grading-panel.html`) locally instead?
Take the same link `mint-local-link.sh` printed and swap the path:
`http://localhost:8080/grading-panel.html?student_id=...&session_id=...#token=...`
renders it the same way, minus Weasis itself (that only runs inside a
Kasm/XFCE session, or the Xvfb-based dev technique described further down)
— useful for iterating on the grading UI in isolation.

**Credentials**: there is no hardcoded Orthanc password anywhere in this
repo — `ORTHANC_PASSWORD` comes from `.env` (copied from the committed
`.env.example` placeholder, generated fresh above), and both the `orthanc`
and `viewer` containers read it from there at start. `docker-compose.yml`
refuses to start either service if it isn't set, rather than silently
falling back to a known default.

### Sample data

`sample-data/*.dcm` are [pydicom](https://github.com/pydicom/pydicom)'s own
bundled test fixtures (`CT_small.dcm`, `MR_small.dcm`) — synthetic/test files
used for exactly this purpose upstream, not real patient data, and safe to
commit to a public repo.

`sample-data/brainix/` is the classic **BRAINIX** MRI teaching study (232
instances / 7 series / ~64MB), pulled from Orthanc's own official public demo
server (`orthanc.uclouvain.be/demo`) via `scripts/fetch-public-samples.sh`.
It's a long-standing public/anonymized teaching dataset, not real patient
data — but at ~64MB it's **not committed to git** (see `.gitignore`); re-run
the fetch script to get it back after a fresh clone.

`sample-data/dicomlibrary_ct/` is a real, multi-hundred-slice **CT study**
(970 instances / 5 series / ~494MB) from
[dicomlibrary.com](https://www.dicomlibrary.com)'s own public "DICOM
Samples" gallery — the kind of large, realistic study the smaller fixtures
above can't stand in for (e.g. issue #8's real-data scrubbing/DLP test).
Confirmed anonymized directly via `pydicom` tag inspection before use
(`PatientName` = `"Anonymized^^"`, `PatientID` = `"0"`, no institution/
physician/accession), not just trusted from the site's own claim. **Not
committed to git** and **not fully automatable** like `brainix/` above —
`fetch-public-samples.sh`'s own comment explains why (dicomlibrary.com's
download link is a per-visit, 5-minute-expiring token, not a stable public
URL) and how to re-fetch it manually if this copy is ever lost.

Real studies still need the anonymization pipeline described in `CLAUDE.md`
(hard rule: *"All DICOM files must be anonymized and stripped of PHI before
ingestion"*) — that pipeline doesn't exist yet and is out of scope for this
MVP.

## Lung-RADS grading

A `grading-api` service (FastAPI + SQLite, `docker/grading-api/`)
implements the 3-stage state machine from the PM's spec, in Polish. It's the
same backend for both viewer flows — only the frontend page calling it
differs (`watermark.html` for Chrome, `grading-panel.html` for Weasis):

1. **Nauka (learning)** — free-text impression, submit, then the real
   reference report appears to cross-check against. Not graded — no NLP,
   no correctness scoring (matches CLAUDE.md's "no NLP or free-text
   grading" hard rule exactly).
2. **Ocena (assessment)** — structured Lung-RADS category (`0`–`4X`,
   dropdown) + "S" modifier (radio), submit, immediate correct/incorrect
   feedback against ground truth (revealed either way, right or wrong).
3. **Test (exam)** — same structured inputs, submit, **no** feedback at
   all until all test-stage cases are done, then a final score summary.

**In the Chrome flow**, the grading panel lives in `watermark.html` as a
sidebar next to the DICOM viewer iframe (same page, no separate route) — on
loading a case it deep-links the iframe to that one assigned study
(`ui/app/index.html#/filtered-studies?StudyInstanceUID=...`), not the full
patient list, matching the original design discussion's "Single DICOM Study
Isolation" requirement. **Worth knowing**: Orthanc's Explorer 2 has no true per-study
*viewer* deep-link (confirmed against its actual router source — no route
takes a study ID at all); this filters the list to one row, which still
needs one click from the student to open the viewer on it. The
`stone-webviewer` plugin this project assumed earlier isn't even installed
on this Orthanc image (confirmed via `GET /plugins`) — corrected once
actually tested against the real instance, not left as an assumption.

**In the Weasis flow**, there's no iframe at all — Weasis itself opens
directly on the assigned study (see "The two workspace images" below, issue
#4), and `grading-panel.html` only ever renders the form, in its own small
window beside it.

Ground truth + reference reports are **placeholder content** on the
existing sample studies (CT_small/MR_small/BRAINIX — none of which are
actually lung CTs) — one case per stage, purely to prove the mechanics work
end to end. Real curated content (500 studies, real ground truth, a real
radiologist's reference reports) is separate work, not yet started.

`grading-api` is internal-only (no host port, not on `kasm_default_network`
— nothing outside `viewer`'s nginx needs to reach it directly), reached via
a same-origin `/api/` proxy so the panel's `fetch()` calls need no CORS
handling. SQLite chosen deliberately over the Postgres the original design
discussion specified — lighter for proving out the mechanics now; migrating
later means changing a connection string, not the app logic. (Flagging
explicitly: this means CLAUDE.md's literal "Stratified random sampling via
PostgreSQL" hard rule doesn't hold today — no Postgres, and no real
stratified sampling yet either, since there's no real 500-study pool to
stratify. Worth revisiting CLAUDE.md's wording once this direction is
confirmed as lasting.)

**A real gap, now fixed**: every `grading-api` endpoint used to take
`student_id` as a plain, unauthenticated parameter — nothing bound it to
the caller's actual Kasm session, so any client on the network could read
or submit as *any* student ID. This mattered more than it might look: at
real cohort sizes (300-500 students), one compromised or malicious session
could manipulate many other students' Lung-RADS exam results, undermining
the whole assessment's validity.

Fixed with a server-issued session token: `POST /session` mints an
unguessable token bound to a `student_id`, but only for a caller that
supplies `GRADING_COORDINATOR_KEY` (a shared secret only
`scripts/create-session.py` knows — otherwise a student could just mint
their own token for anyone's ID and recreate the exact hole this closes).
Every other endpoint (`/case`, `/submit`, `/reset`, `/results`) now takes
that `token` instead, resolving the real `student_id` server-side; a
missing, unknown, or expired token (default TTL 8h, `GRADING_TOKEN_TTL_SECONDS`)
is rejected outright. `student_id`/`session_id` are still shown in the
watermark text — display was never the problem, trusting them for grading
actions was. `custom_startup.sh` (both images) bakes the token in the same
way it already baked in `STUDENT_ID`/`SESSION_ID`; `watermark.html`/
`grading-panel.html` read it from the query string. Minting a new token
for a `student_id` also revokes whatever token existed before it — a
coordinator re-minting a link they suspect leaked gets real revocation,
not just a second valid link.

Revocation also has an explicit endpoint (issue #104): `POST /session/revoke`,
same coordinator key, taking exactly one of `token`/`student_id`. It exists for
the case where the rest of session setup fails — the token must be minted
*before* Kasm is asked for a session, because `request_kasm` injects its value
into that container's environment, so a Kasm failure used to leave a live
token with no session behind it. `create-session.py` calls it on every such
failure; an unknown or already-revoked selector answers `200 revoked:false`
rather than 404, because the caller uses it as an idempotent compensation step.

### Logging and error handling (issues #72, #74)

Two foundational pieces added deliberately together, before working
through the rest of the open issue backlog: `grading-api` previously had
no logging at all (not even a stray `print()`) and every error was an
ad hoc `HTTPException(code, "free-text string")`.

- **Structured logging** (`docker/grading-api/app/logging_config.py`) —
  one JSON object per line, to stdout (no log-shipping agent needed).
  `RequestIdMiddleware` (`main.py`) generates or honors an incoming
  `X-Request-Id`, echoes it in the response, and stamps it onto every log
  line emitted while handling that request (via a `contextvars`-based
  filter, not just baked into the formatted string — inspectable directly
  on the `LogRecord`, which is what makes it testable with pytest's
  `caplog`) — the actual "log correlation propagated across boundaries"
  mechanism. Logs: one access-log line per request, every auth failure
  and error response (via the taxonomy below), and stage
  transitions/submissions recorded. Deliberately never logs: the raw
  session token, `GRADING_COORDINATOR_KEY`, or student free-text answers
  — regression-tested directly (`tests/test_logging.py`), not just
  documented as an intention. Since issue #94 the token is also absent
  from *every URL by design*: `GET /api/case`/`GET /api/results` take it
  as the `X-Grading-Token` header, browser pages receive it as a URL
  fragment (never transmitted), launcher scripts pass it to curl via a
  stdin-fed header (never in `ps`), and nginx rejects any query string
  under `/api/` outright — end-to-end verified by
  `scripts/test-log-leak.sh` (sentinel greps over real container logs). `httpx`'s own logger is explicitly quieted
  (found empirically while writing that test: it logs full request URLs,
  including query-string tokens, at INFO level — test-harness-only today,
  but silenced at the source rather than relied on to stay irrelevant),
  and uvicorn's own default access log is disabled in the Dockerfile
  (`--no-access-log`) — redundant with the new structured one, and would
  otherwise log the token-bearing query string on every request until
  issue #63 moves it out of the URL.
- **Stable error taxonomy** (`docker/grading-api/app/errors.py`) — every
  error response, from every endpoint, is now
  `{"error_code": "...", "message": "..."}` instead of a bare string.
  Two catch-all handlers guarantee this holds even for errors this file
  never anticipated: `RequestValidationError` (pydantic validation
  failures — the client gets a generic message, never the raw rejected
  input, which for a `max_length` violation could otherwise mean echoing
  a large payload straight back) and a bare `Exception` handler (any
  truly unexpected bug still gets a generic `SERVER_ERROR` response, with
  the real exception logged server-side via `logger.exception`, full
  traceback included). This is also where issue #62 (`/healthz` leaking
  raw sqlite exception text) actually gets fixed, generalized to every
  endpoint rather than patched once at `/healthz` specifically — found
  while fixing it that `/healthz`'s own DB connection also leaked on
  every failure path (only ever closed on success), fixed alongside.

Both are covered by dedicated tests (`tests/test_logging.py`,
`tests/test_error_taxonomy.py`) that run in CI on every PR, same as
everything else in `docker/grading-api/tests/`. Several were verified
the way this project verifies tests generally — not just "written and
green," but confirmed to actually fail against the bug they guard
against: the `/healthz` leak test fails if `str(e)` goes back into the
message, and the no-secrets-in-logs test fails if a token/coordinator
key ever gets logged.

## What's NOT in this MVP (on purpose)

Per the original design discussion's own recommendation ("About the
two-week MVP" section), all of this is deferred:

- Moodle / LTI 1.3 launch and grade passback.
- Payments (300 PLN), certificates, access expiry.
- Real stratified case sampling (50/50/30 across Lung-RADS classes, drawn
  from a real curated 500-study pool) — see "Lung-RADS grading" above for
  what's actually built: the 3-stage mechanics, with 1 placeholder case per
  stage rather than a real stratified pool.
- Admin dashboard, leaderboard, expert-review queue.

## Kasm Workspaces

### Installing Kasm itself

Kasm Workspaces Community Edition is installed on this machine (via
<https://kasm.com/docs/latest/install/single-server-install/>, using
`sudo bash kasm_release/install.sh` — a host-level change with its own
systemd service on port 443, deliberately not scripted here since it needs
an interactive sudo password; see git history for the exact steps if setting
this up somewhere else, e.g. the real Proxmox VM).

Everything below assumes Orthanc/viewer run on this same machine as Kasm.
Running them on a separate Proxmox VM/LXC instead? See
`docs/PROXMOX_DEPLOYMENT.md` and `docker-compose.remote-host.yml` — only the
networking/firewall piece differs, everything else here still applies.

**Kasm Workspaces Community Edition licensing, read before planning a real
cohort**: non-commercial/non-profit/personal use only (EULA §2.2) and capped
at 5 concurrent sessions — see `CLAUDE.md`'s hard rules. A real paid,
10-20+ concurrent deployment needs a paid tier and a legal/procurement
review first.

### The two workspace images

**Chrome** (`docker/kasm-workspace/`) — a Kasm Chrome workspace in kiosk
mode, pinned to `watermark.html`. Build/rebuild with:
```bash
docker build -t ipcmc/dicom-viewer:mvp docker/kasm-workspace
```
Then register it in the Kasm admin UI as a Workspace (type **Container**,
Docker Image `ipcmc/dicom-viewer:mvp`, registry blank since it's built
locally on the same Docker host Kasm's agent uses). This is what's actually
deployed and confirmed working end-to-end today.

**Weasis** (`docker/kasm-workspace-weasis/`, milestone "Weasis viewer
migration", GitHub issues #3–#8) — built on Kasm's lean `core-ubuntu-noble`
base (XFCE + KasmVNC only, not the bloated "desktop" bundle) with Weasis
4.7.3 installed from its own `.deb`. Build the same way:
```bash
docker build -t ipcmc/dicom-viewer-weasis:mvp docker/kasm-workspace-weasis
```
Registering it in the Kasm admin UI works the same as Chrome above (same
**Container** type, this image's tag); both images can be registered side
by side.

**How to run/test Weasis without a full Kasm session** — this is a real
native GUI app, so there's no plain-browser equivalent to the Chrome flow's
"just open the page" quick start. What actually verified every piece of
this image during development, and still works for re-verifying it:
```bash
# Inside a running container built from the image above (or any container
# with GTK3 + Weasis's own JRE), with an X display available:
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99
xfwm4 &                      # a real window manager -- bare Xvfb has none,
                              # so "always on top" has nothing to test against
/opt/weasis/bin/Weasis        # or with a weasis:// URI, see custom_startup.sh
import -window root screenshot.png   # ImageMagick, to actually look at it
```
A real Kasm session already provides all of this (KasmVNC's own Xvnc +
xfwm4) — this is purely the technique for developing/debugging the image
without one.

What's confirmed working in this image, in the order it was built:

- **Issue #3 (New Kasm Image)**: launched under a virtual X display, the
  JVM starts, OSGi bundles load, the DICOM codec registers, and the main
  window renders correctly (DICOM Explorer panel, menus, the standard "not
  a certified medical device" disclaimer responds to a real click) — Weasis
  bundles its own JRE at `/opt/weasis/lib/runtime`, so no separate Java
  package is needed.
- **Issue #4 (Automatic Launch Without Extra Clicks)**:
  `docker/kasm-workspace-weasis/custom_startup.sh` fetches the current case
  from `grading-api` (same `/api/` proxy the Chrome flow uses) and launches
  Weasis straight into it via its `dicom:rs` command, with zero manual
  clicks — the disclaimer dialog is also suppressed at build time
  (`weasis.show.disclaimer=false` patched into Weasis's own config, since
  ephemeral Kasm containers get a fresh `$HOME` every session and would
  otherwise hit it every time). Getting `dicom:rs` to actually work took
  real debugging, worth recording:
  - It only works sent as a `weasis://` URI, not raw CLI tokens — Weasis's
    own main-argv command parser races the OSGi bundle that provides the
    command and silently no-ops otherwise (confirmed against
    `nroduit/Weasis`'s own launcher source).
  - The query needs an explicit `requestType=STUDY` ahead of `studyUID=`
    — without it, Weasis's request-type classification (it implements the
    IHE "Invoke Image Display" profile) falls through with no error.
  - **A real bug this surfaced, fixed as part of this issue, not a
    Weasis-side problem**: the auth-injecting proxy (`:8043`) was
    forwarding nginx's `$host` to Orthanc, which strips the port even
    when the original request had one — Orthanc's DICOMweb plugin embeds
    whatever Host it receives into every QIDO-RS response's
    `RetrieveURL`, so Weasis's *queries* worked but every subsequent
    *image download* connection-refused against the wrong port. Fixed by
    forwarding `$http_host` instead (see
    `docker/viewer/default.conf.template`).
- **Issue #5 (Grading Panel)**: `docker/viewer/grading-panel.html` — a
  trimmed-down `watermark.html` with the DICOM iframe removed, everything
  else (the 3-stage JS/API logic, the watermark tiling, the anti-tamper
  deterrents) reused unchanged. A new, separate file rather than an
  in-place edit — `watermark.html` is still the live page for the Chrome
  flow, gutting it would have broken that working flow mid-migration.
- **Issue #7 (Lock Down Weasis Native Save/Copy/Export)** — a direct answer
  to the context-menu/export concern raised on a reference screenshot: File
  > Export offered two items, confirmed via a real running session before
  disabling anything (not guessed from menu labels alone) —
  "Exporting view" (screenshot/clipboard copy of the current view, no
  matching preference exists anywhere in Weasis's own config, see below)
  and "DICOM" (raw export to local disk/CD/ISO). `docker/kasm-workspace-weasis/patch-weasis-config.py`
  patches `weasis.export.dicom`, `weasis.export.dicom.send`,
  `weasis.import.dicom`, `weasis.import.images`, and
  `weasis.import.dicom.qr` to `false`, and blanks the `felix.auto.start.110`
  bundle group entirely (not just hides it) — Felix never loads the DICOM
  Send/Q-R/ISO-writer bundles at all, so the underlying code doesn't exist
  at runtime. Consolidated with issue #4's own disclaimer patch in the same
  script, since both edit the same `base.json` file. Verified in the built
  image: all six preference values confirmed `false`/blanked via
  `docker cp`'d config inspection (not by running the full Kasm entrypoint,
  which starts a real desktop session rather than exiting).
  **Residual gap, not silently accepted**: "Exporting view" has no
  matching preference in Weasis's own config at all — confirmed against
  its actual source (`ActionW.java`'s `EXPORT_VIEW` is an unconditional,
  always-registered core action, not gated by any property). Saving a
  file this way stays trapped inside the ephemeral container regardless,
  since Kasm's own `allow_kasm_downloads` setting blocks that path — but
  the **"Clipboard" option was a real, confirmed exfiltration path, not
  hypothetical**: a live test showed an image copied this way landed on
  the real local clipboard, pasteable into an application outside the
  session entirely, disproving the assumption that Kasm's DLP already
  covered this. Root cause and fix are in the DLP section below.
- **Issue #6 (Watermark Overlay)**: `docker/kasm-workspace-weasis/overlay.py`
  is a transparent, always-on-top, click-through native window (GTK3, not a
  browser page — Weasis is a separate native window a page-based watermark
  could never cover) tiled with `STUDENT_ID | SESSION_ID | timestamp` text,
  paired with `watchdog.sh` so killing it just gets it relaunched within
  about a second. Verified for real, not just assumed:
  - **Compositor + adaptive density (flicker fix, issue #150)**: the first
    overlay was made "transparent" by an X bounding shape built from the glyph
    pixels, re-cut every second. It made the DICOM view flash whenever the
    mouse moved (found by manual testing in a live Kasm session; overlay off =
    no flicker). Measured in live sessions: 7 flashes / 3 min with the shape,
    3 with a 15 s refresh, **0 with picom** (a ~0.5 MB package, started by
    `custom_startup.sh`, config `picom.conf`: no effects, it only makes the
    ARGB overlay genuinely transparent). If picom is not running the overlay
    falls back to the shape (an ARGB window with no compositor is an opaque
    black rectangle) and keeps it small with a density defined per screen:
    `WATERMARK_TILES_PER_SCREEN` (default **3**, owner decision), so the shape
    stays ~1,000-1,200 rectangles from 1366x768 to 4K;
    `./scripts/test-overlay-shape.sh` (budget) and
    `./scripts/test-overlay-compositor.sh` (real X server: alpha mode, then
    picom killed -> fallback, pixel-checked) enforce both in CI.
  - **True transparency without a compositor**: no compositor (e.g.
    `picom`) runs in this XFCE/KasmVNC session by default, so this uses
    the X Shape extension instead (`Gdk.Window.shape_combine_region()`)
    — only the actual glyph pixels are part of the window at all.
    Confirmed with a screenshot against a solid-color test background:
    the color showed through everywhere except the rendered text.
  - **Genuine click-through**: `input_shape_combine_region()` set to an
    *empty* region, independent of the bounding shape above. Confirmed
    by placing a real clickable test button underneath and clicking
    directly on top of rendered watermark glyphs — the click reached the
    button every time.
  - **Stays on top of a real window manager**, not just bare Xvfb (which
    has no window manager at all, so nothing enforces stacking order):
    `Gtk.WindowType.POPUP` (override-redirect, outside window manager
    control entirely) plus `set_keep_above()`, tested under a real
    `xfwm4` session.
  - **The watchdog actually relaunches it**: killed the overlay process
    directly and confirmed a new one appeared within ~1 second, watermark
    coverage intact in a follow-up screenshot.
  - **A limitation flagged, not hidden**: without a compositor, CSS-style
    `mix-blend-mode:difference` (what `watermark.html`/`grading-panel.html`
    use for guaranteed contrast) has no real cross-window equivalent here
    — that trick only works within one browser's own rendering pipeline.
    This uses a dark-stroke + light-fill "halo" around each glyph
    instead (the same technique subtitle overlays use), which is legible
    against both light and dark content but isn't the same mathematical
    guarantee.

**Confirmed via the first real Kasm-issued session** (registered in the
admin UI, launched through `create-session.py`'s minted link — not just
Xvfb/manual container testing): Weasis actually launches on the assigned
study through a real session end to end. That same real test also caught
one bug Xvfb testing couldn't have (fixed): the watermark overlay measured
screen size once before KasmVNC's client-viewport resize happened, so it
covered only part of a real browser-sized screen — see git log for
`docker/kasm-workspace-weasis/overlay.py`.

**The grading panel gap that same test surfaced is now fixed**:
`docker/kasm-workspace-weasis/grading_panel_window.py` displays
`grading-panel.html` (issue #5) in its own window, launched (backgrounded)
by `custom_startup.sh` before the final `exec` into Weasis, pinned to the
right edge of the screen. Deliberately **not** a general-purpose browser
install (no Chromium/Firefox/Epiphany) — that would reopen exactly the
save/print/devtools attack surface issue #7 just closed, through a
different door. Instead: a single WebKit2GTK `WebView` with no browser
chrome at all (no address bar, no menu, nothing to build one from since
none exists), locked down via WebKit2's own documented APIs — context
menu suppressed, developer tools disabled, new-window/popup creation
vetoed, navigation restricted to this session's own origin (same-origin
`fetch`/XHR calls to `grading-api` are unaffected, only top-level page
navigation is restricted), plus a keyboard-shortcut blocklist (F12,
Ctrl+U/S/P, Ctrl+Shift+I/J/C) as defense in depth. See that file's own
header comment for the full rationale and what's explicitly *not* claimed
(same "deterrence, not prevention" framing as the watermark overlay).
Verified against a real `grading-api` session token, not just that it
renders: the actual Polish learning-stage form loaded correctly, complete
with the page's own DOM watermark tiling inside the window.

**Auto-tiled against Weasis, not left to manual resizing**:
`docker/kasm-workspace-weasis/arrange_windows.sh` (launched the same way,
backgrounded before the `exec`) resizes both Weasis's and the grading
panel's windows to split the screen cleanly — `wmctrl` turned out to
already be present in this base image (confirmed via `apt-cache policy`
before assuming otherwise). Two real gotchas found via live tests, not
assumed:
  - Weasis's main window opens already maximized
    (`_NET_WM_STATE_MAXIMIZED_HORZ`/`_VERT`, confirmed via `xprop`), and
    xfwm4 silently ignores a plain geometry resize request against a
    maximized window — a "successful" (exit-0) `wmctrl -r ... -e ...`
    call had zero visible effect until the maximized state was explicitly
    removed first.
  - **Frames count (issue #151).** `wmctrl -e` takes the window FRAME's
    top-left but the CLIENT's size, and xfwm4 puts a 29 px title bar and
    5 px borders around each window. Giving each window "the full screen
    height" pushed Weasis' lower edge 34 px off screen and made the two
    windows overlap by their side borders; the script now subtracts each
    window's own `_NET_FRAME_EXTENTS`. The same loop keeps the kiosk
    intact: minimised windows are restored (no taskbar exists, Alt+F9
    hid both for good), a closed grading panel or Weasis is relaunched
    (rate-limited), and the title-bar minimise/maximise buttons are gone.
  - An *earlier, one-shot* version of this script measured the screen
    size once at startup — but KasmVNC starts at a fixed default geometry
    and only resizes to the client's real browser-viewport size *after* a
    real client connects (confirmed via a live session's own VNC log:
    "Got request for framebuffer resize to 1920x950", logged well after
    this script's first pass had already run and tiled both windows
    against the smaller, stale geometry — the exact same root cause as
    the watermark overlay bug fixed earlier). Fixed the same way as that
    bug: this now re-reads the screen size and re-applies both windows'
    geometry every 2 seconds for the life of the session, not once —
    `wmctrl` calls are cheap and idempotent, so this is a harmless no-op
    once the geometry is already correct, and self-corrects within one
    cycle of any later resize. Verified against a scripted, stubbed
    resize (not just reasoned about): a fake `xrandr` reporting a
    different resolution on successive calls confirmed the script
    re-tiles both windows to the new size on its very next cycle.

**Still outstanding**:
- **Issue #8 (Regression + Real-Data Scrubbing Test)** — re-verify DLP still
  functions on this new image type, test real-world scrubbing smoothness on
  an actual multi-hundred-slice CT study (current fixtures are too small),
  update documentation. The unit-test slice of this (see Testing below) is
  already done; the DLP/real-data piece is not.

### Networking

`docker-compose.yml` attaches `orthanc`/`viewer` to `kasm_default_network`
(external, created by the Kasm installer) so Kasm's session containers
reach them by container name — `http://ipcmc-viewer:8080/` and the
auth-injecting proxy at `http://ipcmc-viewer:8043/` (see
`docker/viewer/default.conf.template` for why Orthanc isn't reached
directly: an iframe/Weasis can't answer its Basic Auth challenge).
`grading-api` stays off this network entirely — nothing outside `viewer`'s
nginx needs to reach it.

### Per-student links

`scripts/create-session.py` first mints a `grading-api` session token
(`POST /session`, see "Lung-RADS grading" above — this is the only place
that ever happens, gated by `GRADING_COORDINATOR_KEY`), then calls Kasm's
public API (`/api/public/request_kasm`) to mint a one-off session with
`STUDENT_ID`/`SESSION_ID`/`GRADING_TOKEN` all baked into its environment —
that's what each image's `custom_startup.sh` reads to launch its viewer
(Chrome or Weasis) with the right identity and credential already in
place. Needs an API key from the admin UI (**Settings → Developers → Add
API Key**, with the **"Users Auth Session"** and **"User"** permissions
enabled — `request_kasm` 403s without both), the target workspace's
image_id (from its edit URL in the admin UI), and the same
`GRADING_COORDINATOR_KEY` value the running stack's `.env` has:
```bash
KASM_SERVER=https://your-kasm-host \
KASM_API_KEY=... KASM_API_KEY_SECRET=... KASM_IMAGE_ID=... \
GRADING_COORDINATOR_KEY=... \
python3 scripts/create-session.py --student-id STU_12345 \
  --ca-bundle /etc/kasm/ca.pem   # or --insecure, but only for a self-signed lab box
```
`GRADING_API_URL` (default `http://localhost:8080/`) points at wherever
`viewer`'s `/api/` proxy is reachable from — override it if this script
runs somewhere other than the Docker host itself (e.g. the separate-Proxmox
setup, `docs/PROXMOX_DEPLOYMENT.md`).

**TLS is verified by default** (issue #104): no flag means certificate *and*
hostname verification against your system trust store. A private CA gets
`--ca-bundle /path/to/ca.pem` (or `TLS_CA_BUNDLE` in the environment), which is
**added to** your system trust store rather than replacing it — Kasm's gateway
and `viewer`'s `/api/` proxy routinely have two different issuers. That is
the flag to reach for when a call fails with a certificate error, not
`--insecure`, which turns verification off for every request the script makes
including the ones carrying `KASM_API_KEY_SECRET`. `--insecure` remains only
for a self-signed local/dev Kasm instance; it prints a warning on every run,
and typing it together with `--ca-bundle` is rejected outright rather than
silently overriding the CA you thought was in use. See
`docs/PROXMOX_DEPLOYMENT.md`'s "Transport security" section for how to get a
real certificate (and why the separate-host setup needs more than just that).

**A failed run undoes itself** (issue #104): the grading token has to be
minted *before* the Kasm call, because its value is injected into that
container's environment, so a Kasm failure used to leave a live token with no
session behind it. `create-session.py` now calls `POST /api/session/revoke`
whenever it fails before Kasm answered — and deliberately does *not* revoke
once Kasm answered, because by then a real session holds that token. Ctrl-C,
SIGTERM (a cancelled CI job) and a closed terminal all trigger the same undo.
What no handler can catch — `SIGKILL`, an interpreter crash — falls back
to the token's own expiry, `GRADING_TOKEN_TTL_SECONDS` (capped at 7 days by
`docker/grading-api/app/config.py`), which is why that cap exists.

Prints a ready-to-share `link` — no login required, it's pre-authenticated
via a session token Kasm generates. The script also tries a readiness
check (`get_kasm_status`) but treats it as best-effort: a scoped API key
commonly lacks the separate "impersonate another user" permission that
call needs for an anonymous/other user, so it's fine if that part logs a
non-fatal warning and skips straight to printing the link.

### DLP settings

Clipboard, file upload/download, printing — the on-prem equivalent of
AppStream's "Stack Policy" — configured on the **Group** anonymous sessions
land in (`Access → Groups → All Users → Settings`), not per-workspace:
`allow_kasm_clipboard_down/up/seamless`, `allow_kasm_downloads`,
`allow_kasm_uploads`, `allow_kasm_printing`, `allow_kasm_sharing`,
`allow_kasm_webcam`, `allow_kasm_microphone`, `allow_kasm_gamepad`,
`allow_kasm_audio` all set to `False`. Verified two ways, not just trusted:
queried `group_settings` directly in Kasm's own Postgres DB to confirm the
stored values, then launched a real test session and confirmed inside the
container that
`KASM_SVC_DOWNLOADS`/`KASM_SVC_UPLOADS`/`KASM_SVC_PRINTER` are `0` — those
services aren't just hidden in the UI, they never start.

**A real gap this same testing found, now fixed**: the admin-UI clipboard
toggles above only govern *plain-text* clipboard sync — confirmed correctly
blocking a real attempt to copy text out of a session. But KasmVNC has a
second, independent clipboard-DLP layer for *rich/binary* MIME types
(`data_loss_prevention.clipboard.allow_mimetypes` in `kasmvnc.yaml`), whose
documented default already includes `image/png` — and in practice, an image
copied to the clipboard from inside a session (confirmed via Weasis's own
"Export → Clipboard" feature, which has no config of its own to disable —
see issue #7 above) landed on the real local clipboard, pasteable into an
application outside the session entirely. Kasm's own docs don't explain how
this setting relates to the simpler admin-UI toggles. Fixed by baking a
`kasmvnc.yaml` with `allow_mimetypes: []` directly into **both** workspace
images (`docker/kasm-workspace/kasmvnc.yaml`,
`docker/kasm-workspace-weasis/kasmvnc.yaml`) — confirmed both at the
mechanism level (the real `Xvnc` process's own `-DLP_ClipTypes` flag now
shows empty instead of the previous default) and end to end, via a real
session: the exact same "Export → Clipboard" → paste-into-a-local-app
attempt that previously escaped the session is now blocked. Also flagged
upstream, since the underlying gap is in Kasm/KasmVNC itself, not this
project's code:
[kasmtech/workspaces-issues#912](https://github.com/kasmtech/workspaces-issues/issues/912).

**Verified against the Weasis image via a real click-through test**
(issue #8): the checks above (downloads/uploads/printing services not
starting, text clipboard blocked) all held. The image-clipboard gap above
is the one thing that real testing caught that assuming coverage from the
Chrome-image checks wouldn't have.

**Regression-tested on every PR, not just verified once**
(`scripts/test-copy-lockdown.sh`, `scripts/tests/test_copy_lockdown.py`):
the two copy-mechanism lockdowns that live as actual files in this repo —
Weasis's own native export/import/send/Q-R (`patch-weasis-config.py`) and
the `kasmvnc.yaml` rich-clipboard-mimetype block above, for **both**
workspace images — are asserted against, including a real `docker build`
of the Weasis image so a later Dockerfile edit that silently stops
applying the patch would fail CI, not just an isolated unit test. What
this can't cover, structurally: the Group-level admin-UI toggles at the
top of this section live in a real, running Kasm instance's own Postgres
DB, not in any file here — a GitHub Actions runner has no live Kasm to
re-check those against, so that verification stays the one-time, by-hand
act described above unless someone re-runs it against the real instance.

## Apache Guacamole flow (PoC, alternative to Kasm)

**Why this exists**: Kasm Workspaces Community Edition is capped at 5
concurrent sessions and restricted to non-commercial/non-profit/personal
use (EULA §2.2, see `CLAUDE.md`) — a real blocker for any deployment
beyond a small pilot, needing a paid tier + legal/procurement review
before scaling further. Apache Guacamole is Apache-licensed with no such
cap. This is a proof of concept evaluated *alongside* the Kasm flow, not
a replacement yet — same backend (`orthanc`/`grading-api`/`viewer`,
untouched), same Lung-RADS mechanics, just a different way of streaming
Weasis to a browser.

**Deliberately basic-functionality only, not a hardened flow**: no
watermark overlay, no Weasis export/import lockdown, no window
auto-tiling, and `--security-opt seccomp=unconfined` on every session
container (see below). **Do not point this at real patient data or run
it as a real deployment** — it
exists to prove the streaming mechanism works, matching exactly what was
asked of this PoC; hardening it to Kasm-flow parity is separate, future
work.

### Architecture

```
scripts/provision-guacamole-session.py
        │
        ├─► grading-api POST /session (mints a token, same as create-session.py)
        ├─► docker run (a fresh, per-student Weasis+TigerVNC container)
        └─► Guacamole REST API (creates a real user + VNC connection)
                        │
   Guacamole webapp ◄───┘ (Postgres-backed JDBC auth, its own DB --
        │                  separate from grading-api's own SQLite)
        ▼
      guacd  ──── VNC (raw RFB) ────►  docker/guacamole-weasis/ container
                                       (TigerVNC + openbox + Weasis +
                                        a plain browser tab for the
                                        grading panel)
```

- **`docker-compose.guacamole.yml`** — an overlay file (combine with the
  base compose file, not standalone): `guacd`, the Guacamole webapp, and
  its own Postgres (JDBC auth backend — a real, separate requirement from
  grading-api's own database, not a redundant duplicate). Reuses the base
  file's existing `ipcmc-internal` network — no dependency on
  `kasm_default_network` or Kasm being installed at all.
- **`docker/guacamole-weasis/`** — a new workspace image, plain
  `ubuntu:24.04` (not any Kasm-maintained base). **TigerVNC, not
  KasmVNC** — confirmed via direct Guacamole-protocol-level testing (not
  assumed from docs) that `guacd`'s VNC client cannot complete an RFB
  handshake against KasmVNC at all: it serves VNC exclusively over its
  own websocket-wrapped protocol, never a raw RFB TCP port, which is what
  `guacd` needs.
- **`scripts/provision-guacamole-session.py`** / **`teardown-guacamole-session.py`**
  — Guacamole's own equivalent of `create-session.py`. Guacamole has no
  built-in per-session container provisioning (unlike Kasm's agent) and
  no supported anonymous quick-link mechanism (checked before building
  this, not assumed — Guacamole's own "quickconnect" extension explicitly
  requires prior authentication, and the only anonymous-login extension
  that exists is an unmaintained third-party fork with no version-
  compatibility guarantee). A real per-student Guacamole user + Postgres-
  backed JDBC auth, combined with Guacamole's documented URL-parameter
  auto-login (`#/?username=...&password=...`), is the officially-supported
  way to get a single, no-further-login-prompt link per student.
- **`scripts/guacamole-iac.sh`** — brings the whole stack up from scratch
  on a fresh host. Plain bash on purpose (not Ansible yet) — the agreed
  starting point before considering a migration once this PoC's shape has
  settled.

### Setup

```bash
./scripts/guacamole-iac.sh
```

Then mint a per-student link:
```bash
GRADING_COORDINATOR_KEY=$(grep GRADING_COORDINATOR_KEY .env | cut -d= -f2) \
  python3 scripts/provision-guacamole-session.py --student-id STU_12345
```
The stdout JSON is non-secret; the auto-login link itself is written to
a 0600 file whose path appears as `link_file` (default dir
`local/session-links`). `cat` it, hand it to exactly one student, delete
it after (`cat`, one student, `rm`). Tear the session down once finished
(the session id is the `session_id` field of that JSON; digits only):
```bash
python3 scripts/teardown-guacamole-session.py --student-id STU_12345 --session-id <session_id>
```

### Confirmed working, via real tests, not assumed

- Full pipeline verified with a direct Guacamole-protocol test (bypassing
  the webapp entirely): `guacd` receives real display data
  (`img`/`rect`/`blob` instructions) from the TigerVNC container — this is
  what confirmed KasmVNC's incompatibility and TigerVNC's compatibility
  in the first place.
- Full browser-driven E2E tests (`docker/guacamole/tests/test_guacamole_e2e.py`,
  `./scripts/test-guacamole-e2e.sh`): a real headless Chromium, driven by
  Playwright, confirms the auto-login link logs straight in with no login
  form shown, and that the remote-display canvas renders real (non-blank)
  pixel data. Visually confirmed during development (a saved screenshot,
  not kept in the repo) that this is a correctly-rendered Weasis session
  showing the assigned study, not just "some canvas content."
- `docker/guacamole-weasis/launch-session.bats` — same BATS stubbing
  technique as the Kasm flow's own launcher tests: the case-fetch/
  `dicom:rs` URI construction (copied verbatim from
  `docker/kasm-workspace-weasis/custom_startup.sh` — identical regardless
  of which remote-display tech streams the pixels) and the grading-panel
  URL construction both verified.

### Real bugs found and fixed along the way

- Weasis's own `.deb` postinst script calls `xdg-desktop-menu`, which
  fails outright ("No writable system menu directory found") on a
  minimal image without a full desktop environment's usual menu
  directories already present — the Kasm base image never hit this
  because its full XFCE stack provisions them implicitly.
- `java.awt.Desktop.getDesktop()` throws
  `UnsupportedOperationException` on a bare `openbox` window manager
  without `XDG_CURRENT_DESKTOP` set — Weasis calls this at startup and
  crashed outright without it.
- `epiphany` (the grading-panel browser) needs unprivileged user
  namespaces for its own internal `bubblewrap` sandboxing, which this
  container's default seccomp profile blocks — see the security
  trade-offs below.

### Known security trade-offs (tracked, not accidental)

**Fixed since first written**: per-session VNC authentication (issue
#53) — every container used to run with `-SecurityTypes None` (zero VNC
auth), safe only because it was never published to the host. Now
`scripts/provision-guacamole-session.py` mints a real per-session
password (`secrets.token_hex(4)` — classic VNC/RFB auth only ever uses
the first 8 bytes, a protocol limitation), hands it to the container via
`VNC_PASSWORD` (`docker/guacamole-weasis/docker-entrypoint.sh` writes it
into TigerVNC's own password file via `tightvncpasswd`'s `vncpasswd`,
since Ubuntu's own `tigervnc-standalone-server` package doesn't ship
one), and gives Guacamole's connection config the same password so
`guacd` actually authenticates. Regression-tested
(`scripts/tests/test_provision_guacamole_session.py`) against both this
and the container never gaining a published port.

- **`--security-opt seccomp=unconfined`** on every session container —
  needed for `epiphany`'s own internal sandboxing (`bubblewrap`/user
  namespaces). A real hardening pass should replace this with a narrower
  custom seccomp profile permitting just the specific syscalls needed,
  not a blanket disable.
- **Guacamole's default admin credentials** (`guacadmin`/`guacadmin`) —
  change these before anything beyond a local PoC; `scripts/guacamole-iac.sh`
  doesn't do this for you.
- **No automatic idle-session teardown** — unlike Kasm's own agent,
  nothing here detects an abandoned session and cleans it up
  automatically; `teardown-guacamole-session.py` must be run explicitly
  (or scheduled) or containers/Guacamole users accumulate over time.

### Guacamole itself: bugs/conflicts checked, nothing new to report

Researched before building (not assumed): one known CVE
([CVE-2024-35164](https://github.com/apache/guacamole-server/security/advisories/GHSA-8wh3-jcvc-qrmq),
an RCE in the terminal emulator's handling of SSH/telnet console codes,
fixed in 1.6.0) — irrelevant to VNC-only usage here, but confirms pinning
`guacd`/`guacamole` at `1.6.0` (already done) rather than an older
version. The KasmVNC incompatibility above is a genuine architectural
difference between two independent projects' own protocol choices, not a
Guacamole bug — nothing new was found worth filing against Guacamole's
own tracker for this work.

## Security note (read this before assuming more than it does)

Per the original design discussion: **nothing here, or in the real
Kasm deployment, actually prevents someone from photographing their screen.**
The watermark is deliberately an attribution/deterrence control, not a
prevention control — that distinction needs to be in whatever acceptable-use
agreement participants sign, not just in this code. What this MVP *does*
enforce:

- Raw DICOM pixels never reach a browser outside the internal Docker network
  (Orthanc has no published port beyond `127.0.0.1`).
- Every session's viewer carries a sparse, tiled, timestamped identity
  watermark (student/session ID baked in server-side via the Kasm API, not
  user-controllable JS state) — DOM-based with
  `mix-blend-mode:difference` for the Chrome flow, a native GTK overlay
  using the X Shape extension for the Weasis flow — re-rendered every
  second either way.
- A DOM-tamper check (Chrome flow) reloads the page if the watermark is
  hidden or removed, and a watchdog process (Weasis flow) relaunches the
  overlay within ~1 second if it's killed — deterrents, not guarantees
  (disabling JS entirely, or having root in the container, defeats them
  respectively — same as noted in the docs).
- The auth-injecting Orthanc proxy (`:8043`) is read-only (issue #64) —
  it used to forward every method to every path, so anything reachable on
  `kasm_default_network` (not just `viewer` itself) got Orthanc's full
  authenticated REST API, including delete. Confirmed the real
  vulnerability first (a `DELETE` through the proxy genuinely destroyed a
  study), then fixed it with a method restriction
  (`limit_except GET HEAD`), not a path allowlist — confirmed via a real
  Playwright-driven browse-and-open flow that Explorer2's own read-only
  browsing needs GET across many paths plus exactly one POST endpoint
  (`/tools/find`, Orthanc's own search/query call), which is why
  `/dicom-web/`-only was rejected as the fix even though that's what was
  originally proposed.

## Testing

A staged pipeline (`.github/workflows/ci.yml`), each stage gating the
next so a cheap failure (bad syntax, a build break) fails in seconds
instead of waiting on the slowest stage — jobs grouped under the same
stage still run in parallel with each other:

1. **Lint & Format** — `scripts/lint.sh`
2. **Build Test** — `scripts/build-test.sh`
3. **Security Scan** — `scripts/security-scan.sh`
4. **Unit & Shell Tests** — `scripts/test-grading-api.sh` + `scripts/test-shell-scripts.sh` + `scripts/test-provision-guacamole-session.sh`
5. **Integration Tests** — `scripts/smoke-test.sh` + `scripts/test-guacamole-integration.sh` + `scripts/test-visual-regression.sh`

Every script above is runnable identically on a local machine, not just
in CI.

- `scripts/lint.sh` — bash syntax check on every script, `docker compose
  config` validation on all three compose files, and `ruff check`/
  `ruff format --check` against `docker/grading-api` (needs `ruff` on
  `PATH`, e.g. via its own `requirements-dev.txt`). No infrastructure
  needed, safe to run anytime.
- `scripts/build-test.sh` — builds every image in the repo (both compose
  files plus the 3 standalone Kasm/Guacamole workspace Dockerfiles), never
  runs any of them. Fails fast on a build-breaking change before the
  slower stages below even start.
- `scripts/security-scan.sh` — bandit (Python security linter, scoped to
  `grading-api`'s own `app/` code, not `tests/` — its legitimate asserts
  trip bandit's B101 with zero security value), shellcheck (real static
  analysis for the bash scripts, beyond `lint.sh`'s syntax-only check),
  hadolint (Dockerfile best practices — see `.hadolint.yaml` for the
  threshold and why 2 low-severity findings are intentionally
  non-blocking), and trivy (known CVEs in pinned dependencies and built
  images). Every tool here was dry-run against this repo before being
  wired in, and every real finding it surfaced was fixed first (3 HIGH
  starlette CVEs via a `fastapi`/`uvicorn` bump, several `nginx:alpine`
  base-image CVEs via a version bump + a build-time `apk upgrade`, an
  unused import, 4 overlong lines, 2 real shellcheck findings) — this
  stage stays green by default, not noisy from day one.
- `scripts/test-grading-api.sh` (issues #8, and the session-token fix) —
  unit tests for `grading-api`'s 3-stage state machine and its session-
  token authorization (`docker/grading-api/tests/test_state_machine.py`,
  21 tests), driven through FastAPI's own `TestClient` against a fresh,
  isolated SQLite file per test — no Docker, no real stack, runs in well
  under a second. Since issue #104 the same runner also covers
  `docker/grading-api/tests/test_session_revoke.py` (16 tests) for
  `POST /session/revoke`: the coordinator gate, both selectors, an
  already-gone token answering `200 revoked:false` rather than 404, that no
  rejection ever echoes the token it was handed, that every successful
  revocation leaves a `session_revoked` audit line naming the selector kind and
  row count (never the credential, never even the `student_id`), and that a
  trailing-newline `student_id` is rejected rather than waved through an
  anchored-but-search-style pattern check. These exist specifically
  to protect the invariants this
  project keeps stating in prose but never had automated coverage for:
  a token is required everywhere and only `POST /session` (coordinator-key
  gated) can mint one; an invalid, unknown, or expired token is rejected;
  minting a new token for a `student_id` revokes whatever came before it;
  ground truth/reference reports never leak before the stage that reveals
  them; the test stage reveals nothing at all; progress advances correctly
  case→case→stage→"complete"; two students' state never crosses; and the
  existing input validation (stage mismatches, `time_spent_seconds` range)
  actually behaves as documented. Verified the suite itself, not just that
  it's green: deliberately broke the token-expiry check in `main.py`,
  confirmed exactly one test failed (the one guarding that invariant,
  nothing else), then reverted.
- `scripts/test-shell-scripts.sh` (issue #16) — BATS tests (20 tests total)
  for the Kasm workspace launcher scripts. No Docker, no real
  Kasm/Weasis/grading-api needed: the scripts under test gained small,
  production-inert seams (`CHROME_BIN`/`WEASIS_BIN`/`OVERLAY_SCRIPT`/
  `WATCHDOG_LOG`, all unset in production) so a test can stub the actual
  binary — a fake executable that just captures its own argv to a file —
  instead of launching a real browser, Weasis, or GTK overlay.
  - `docker/kasm-workspace/custom_startup.bats` (7 tests) and
    `docker/kasm-workspace-weasis/custom_startup.bats` (9 tests): the
    Weasis suite also stubs `curl` (via `PATH`) to stand in for
    `grading-api`'s response, while the real `python3` still runs the
    actual `dicom:rs` URI-building logic being tested — that's the whole
    point, verifying the real percent-encoding behavior that took real
    debugging to get right (issue #4), not mocking it away. Verified the
    suite itself the same way as `test-grading-api.sh`: deliberately
    removed `requestType=STUDY` from the built URI, reran, confirmed
    exactly one test failed, then reverted. Also fixed a small real
    inconsistency found while writing these: `docker/kasm-workspace/
    custom_startup.sh`'s `VIEWER_URL` had no `:?` guard (unlike
    `ORTHANC_URL` right next to it), so a genuinely missing value failed
    with bash's own generic `VIEWER_URL: unbound variable` instead of a
    message actually pointing at the problem — now guarded the same way.
    Both suites also gained a `GRADING_TOKEN` seam and an assertion the
    Weasis suite's `curl` call authenticates with the session token
    (since issue #94 via a stdin-fed `X-Grading-Token` header — asserted
    present in the stdin header AND absent from curl's argv), never the
    old bare `?student_id=` (the session-token fix's own regression
    coverage here).
  - `docker/kasm-workspace-weasis/watchdog.bats` (4 tests): since the
    script under test is a deliberate infinite loop, every test bounds it
    with `timeout` rather than waiting for it to exit on its own. **Found
    and fixed a real bug writing this test**: the logged exit code was
    always `0`, regardless of what the overlay actually exited with —
    `$(date ...)` runs its own command inside the same `echo`'s string
    and overwrites `$?` before the later `$?` in that string gets
    expanded, clobbering the real exit status before it was ever read.
    Fixed by capturing `$?` into a variable immediately after the command
    it belongs to. Verified the same way: confirmed the test fails
    against the original code, passes against the fix.
- `scripts/test-provision-guacamole-session.sh` (issue #53) — unit tests
  for `scripts/provision-guacamole-session.py` (3 tests), `subprocess.run`
  and `urllib.request.urlopen` both mocked, no Docker/real Guacamole
  needed. pytest's discovery from that directory also runs
  `scripts/tests/test_create_session.py` (issue #104, 46 tests): every
  failure path between minting a grading token and Kasm answering has to
  revoke it, the one path where revoking would be wrong (Kasm already
  answered, so the token is a live session's credential) has to leave it
  alone, no failure message may contain the token itself, and every request
  has to leave on a verifying context (`--insecure` only when typed, private
  CA appended via `--ca-bundle`, SIGTERM/SIGHUP compensating like Ctrl-C), and
  every failure line names the service that failed (`label` is required on
  `api_call`, so a Kasm failure reads "Kasm API error calling
  /api/public/request_kasm", never a generic "API error …").
  Guards the two invariants that make per-session VNC auth
  actually work: the container's `docker run` never gains a `-p`/
  `--publish` (the port must stay reachable only via `docker_network`),
  and the same `VNC_PASSWORD` the container gets is exactly what
  Guacamole's connection config sends back during authentication.
  Verified each independently by injecting the corresponding regression
  and confirming exactly that test failed, then reverting.
- `scripts/smoke-test.sh` — brings the main stack up for real (building
  images, stubbing `kasm_default_network` if it doesn't exist) and checks
  the HTTP status codes that were, until this was added, verified by hand
  after every change: Orthanc healthy, the watermarked viewer wrapper
  loads, the auth-injecting proxy actually injects auth (307, not 401),
  that Orthanc's DICOMweb `RetrieveURL` actually includes the proxy's port
  (issue #4's regression check — a status-code-only check would never have
  caught this, since the QIDO query itself always returned 200 regardless
  of the bug), and (the session-token fix's own regression check) that
  `POST /api/session` actually mints a usable token, that an invalid token
  is rejected with 401, and that `POST /api/session` itself is rejected
  without the coordinator key. Since issue #104 it also proves the revocation
  path works **through this proxy** rather than only in-process: mint →
  `POST /api/session/revoke` → the revoked token gets 401 at `/api/case`, and
  `revoke` without the coordinator key is refused. That edge routing matters —
  a revoke that 404s at nginx would leave `create-session.py` silently not
  compensating while every unit test stayed green.
  **Tears the stack down with `docker compose down -v` when it's done** —
  don't run this against an environment with data you care about; it's
  meant for a disposable/CI environment.
- `scripts/test-guacamole-integration.sh` — self-contained wrapper around
  `scripts/test-guacamole-e2e.sh` (which itself assumes the stack is
  already up, meant for a developer who already ran
  `scripts/guacamole-iac.sh`): provisions `.env` secrets, builds
  `docker/guacamole-weasis:poc`, brings up the core stack + Guacamole
  infra, loads only the small committed `CT_small.dcm` fixture (a
  brand-new student always starts on the learning-stage seed case, so the
  larger `fetch-public-samples.sh` download isn't needed here), runs the
  real Playwright E2E suite, and tears everything down on exit — same
  self-contained/self-tearing-down pattern as `smoke-test.sh` above. Needs
  Docker-in-Docker (bind-mounts the host's `docker.sock`) and
  `--network host`, same as `scripts/test-guacamole-e2e.sh` itself.
- `scripts/test-visual-regression.sh` — visual regression testing for
  `watermark.html` and `grading-panel.html`: catches any change to what a
  student actually *sees* (`docker/viewer/tests/test_visual_regression.py`,
  14 tests — every meaningfully distinct panel state, both flows), not
  just whether the API responses are correct. Uses
  [`pytest-playwright-visual-snapshot`](https://pypi.org/project/pytest-playwright-visual-snapshot/)
  (`pixelmatch` under the hood, the same engine Playwright's own JS visual
  comparisons use) — the open-source equivalent of what a commercial
  UI/UX-testing SaaS product would otherwise be needed for. The hard part
  is determinism: a live-updating watermark (student/session ID +
  timestamp, re-rendered every second) would make every screenshot differ
  from the last if left alone. Solved with Playwright's Clock API
  (`page.clock.set_fixed_time`, frozen *before* the page's own script
  runs) rather than masking the watermark away — confirmed with two
  independent fresh-stack runs producing byte-identical results before
  committing the baselines, and confirmed the other direction too: a
  deliberately introduced visible change (a button's color) was caught
  precisely on the 6 states that button actually appears in, nothing
  else. Same self-contained/self-tearing-down pattern as the other
  integration tests. Baselines live under
  `docker/viewer/tests/__snapshots__/` (committed); on a mismatch,
  diff/actual/expected images are written to
  `docker/viewer/tests/__snapshot_failures__/` (gitignored — uploaded as
  a CI artifact instead, see `.github/workflows/ci.yml`) so a reviewer
  can see exactly what changed. Run
  `./scripts/test-visual-regression.sh --update-snapshots` and review the
  resulting images before committing a deliberate UI change.

Deliberately not covered by any of these (needs real Kasm infrastructure a
CI runner doesn't have, stays manual): actually launching a Kasm session,
`create-session.py` against a live instance, DLP settings — see this
project's own commit history for how each of those was actually verified.
(Each `custom_startup.sh`'s own logic — the command it builds, not an
actual Kasm session launching it — *is* now covered, by the BATS suite
above.)

## Repo layout

```
CLAUDE.md                             hard rules for this project (do not violate)
.hadolint.yaml                        Dockerfile lint config for scripts/security-scan.sh (threshold + why)
.env.example                          copy to .env and fill in a real ORTHANC_PASSWORD (gitignored)
docker-compose.yml                    orthanc + grading-api + viewer, for local testing
docker-compose.remote-host.yml        variant for running orthanc/viewer on a separate Proxmox VM/LXC
docker/orthanc/                       Orthanc config (no credentials in here -- see .env.example)
docker/viewer/                        nginx (config templated, auth token computed from env) +
                                       watermark.html (Chrome flow) + grading-panel.html (Weasis flow)
docker/grading-api/                   Lung-RADS 3-stage grading mechanics (FastAPI + SQLite)
docker/grading-api/app/logging_config.py  structured JSON logging + request-id correlation (issue #72)
docker/grading-api/app/errors.py      stable {error_code, message} error taxonomy (issue #74)
docker/grading-api/tests/             unit tests (scripts/test-grading-api.sh)
docker/grading-api/pyproject.toml     ruff config (line-length, isort known-first-party)
docker/kasm-workspace/                Chrome-based Kasm workspace image -- the one actually deployed
docker/kasm-workspace-weasis/         Weasis-based workspace image -- milestone in progress, issues #3-#8
docker-compose.guacamole.yml           Guacamole PoC overlay (guacd + webapp + Postgres) -- combine with
                                       docker-compose.yml, doesn't stand alone
docker/guacamole/postgres-init/       Guacamole's own official Apache-licensed JDBC schema
docker/guacamole/tests/               full browser-driven E2E tests (scripts/test-guacamole-e2e.sh)
docker/guacamole-weasis/              basic-functionality-only Weasis+TigerVNC image for the Guacamole flow
                                       -- not security-hardened, see README's own Guacamole section
docker/viewer/tests/                  visual regression tests (scripts/test-visual-regression.sh) --
                                       __snapshots__/ (committed baselines), __snapshot_failures__/ (gitignored)
sample-data/                          public-domain sample DICOM files
scripts/fetch-public-samples.sh       pulls larger public teaching studies (BRAINIX) into sample-data/
scripts/load-sample-studies.sh        uploads sample-data/ (recursively) into Orthanc
scripts/create-session.py             mints a per-student Kasm session link (tested against a live instance)
scripts/mint-local-link.sh            mints a grading-api token for the no-Kasm Quick Start (localhost:8080/... needs a token now, not just student_id)
scripts/guacamole-iac.sh              brings up the whole Guacamole PoC stack from scratch
scripts/provision-guacamole-session.py mints a per-student Guacamole session link, written to a 0600 file, never printed (Kasm-free flow)
scripts/teardown-guacamole-session.py tears down a Guacamole session (container + Guacamole user/connection + link file)
scripts/lint.sh                       stage 1, Lint & Format: bash syntax + compose config + ruff (CI)
scripts/build-test.sh                 stage 2, Build Test: builds every image in the repo (CI)
scripts/security-scan.sh              stage 3, Security Scan: bandit + shellcheck + hadolint + trivy (CI)
scripts/test-grading-api.sh           stage 4, grading-api unit tests via pytest (CI)
scripts/test-shell-scripts.sh         stage 4, Kasm + Guacamole launcher script tests via BATS (CI)
scripts/test-provision-guacamole-session.sh
                                       stage 4, provision-guacamole-session.py unit tests via pytest (CI)
scripts/tests/                        test_provision_guacamole_session.py + its own requirements-dev.txt
scripts/smoke-test.sh                 stage 5, full-stack integration check (CI)
scripts/test-guacamole-e2e.sh         full browser-driven E2E test for the Guacamole flow -- assumes the
                                       stack is already up; scripts/test-guacamole-integration.sh below
                                       wraps it for CI, this one's for a developer using guacamole-iac.sh
scripts/test-guacamole-integration.sh stage 5, self-contained wrapper around test-guacamole-e2e.sh (CI)
scripts/test-visual-regression.sh     stage 5, visual regression testing for watermark.html +
                                       grading-panel.html (CI)
.github/workflows/ci.yml              the 5-stage pipeline above, run on every push/PR
```
