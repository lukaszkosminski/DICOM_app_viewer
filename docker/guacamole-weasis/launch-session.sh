#!/usr/bin/env bash
# Guacamole-flow equivalent of docker/kasm-workspace-weasis/custom_startup.sh.
# Hosting-focused per the owner's decision of 2026-10-09: no Weasis
# export/import lockdown. The forensic watermark is started here (issue #179,
# mandatory) and the windows are tiled and kept alive (issue #180). Fetches the
# assigned case, launches Weasis on it, plus the Kasm flow's grading panel window.
# Do not point this at real patient data; see docker-compose.guacamole.yml's own
# header comment.
#
# The case-fetch + weasis:// URI construction below is copied verbatim
# from docker/kasm-workspace-weasis/custom_startup.sh -- that logic (which
# stage/study to load, the exact dicom:rs invocation) has nothing to do
# with which remote-display technology streams the pixels, so it's
# identical here. See that file's own header comment for the reasoning
# behind the exact invocation (weasis:// URI vs raw tokens, requestType
# ordering, per-token percent-encoding, the Host-header fix for QIDO-RS
# RetrieveURLs) -- not repeated here to avoid the two drifting apart.
set -euo pipefail

STUDENT_ID="${STUDENT_ID:-UNKNOWN_STUDENT}"
SESSION_ID="${SESSION_ID:-$(date +%s)}"
VIEWER_URL="${VIEWER_URL:?set VIEWER_URL to the internal viewer address}"
ORTHANC_URL="${ORTHANC_URL:?set ORTHANC_URL to the internal auth-proxy address}"
GRADING_TOKEN="${GRADING_TOKEN:?set GRADING_TOKEN to the token minted by grading-api POST /session}"
# CR #121 nit: the token is interpolated into a curl --config line wrapped
# in double quotes; a value containing " or \\ could close the quote and
# inject further curl options. grading-api only ever mints token_urlsafe
# (base64url) values, so anything else means corruption or tampering --
# fail fast and loud instead of feeding curl a crafted config.
if [[ ! "$GRADING_TOKEN" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "ERROR: GRADING_TOKEN contains characters outside the URL-safe alphabet ([A-Za-z0-9_-]) -- refusing to launch" >&2
  exit 1
fi

# issue #94: the token rides an X-Grading-Token HEADER, fed to curl via
# --config on stdin (bash process substitution + builtin printf) so it is
# never in curl's argv (`ps`-invisible) and never in a query string
# (nginx logs). Mirrors docker/kasm-workspace-weasis/custom_startup.sh.
# issue #102: same fail-open removal as docker/kasm-workspace-weasis/
# custom_startup.sh -- unreachable grading-api must never masquerade as
# {"complete": true"}. 3 attempts, linear backoff, token-free error line;
# empty CASE_JSON then falls through to the documented plain-launch path.
# API_RETRY_BACKOFF is a test seam (integer seconds; BATS uses 0).
API_ATTEMPTS=3
API_RETRY_BACKOFF="${API_RETRY_BACKOFF:-2}"
CASE_JSON=""
api_ok=0
for attempt in 1 2 3; do
  if CASE_JSON=$(curl -fsS --config - "${VIEWER_URL}api/case" < <(printf 'header = "X-Grading-Token: %s"\n' "$GRADING_TOKEN") 2>/dev/null); then
    api_ok=1
    break
  fi
  if [ "$attempt" -lt "$API_ATTEMPTS" ]; then
    sleep "$((attempt * API_RETRY_BACKOFF))"
  fi
done
if [ "$api_ok" != "1" ]; then
  echo "launch-session: ERROR grading-api unreachable at ${VIEWER_URL} after ${API_ATTEMPTS} attempts -- opening a study-less session; the case was NOT completed, use the panel's retry" >&2
fi

# Issue #180: the URI comes from weasis_case_uri.py, the same helper the Kasm flow
# and the window loop use (so the first study and later ones cannot drift apart).
# Empty output (complete case, no study, unparseable response) = plain Weasis.
# WEASIS_URI_HELPER is a test seam.
WEASIS_URI_HELPER="${WEASIS_URI_HELPER:-/opt/grading-panel/weasis_case_uri.py}"
WEASIS_URI=$(python3 "$WEASIS_URI_HELPER" uri "$CASE_JSON" "$ORTHANC_URL" 2>/dev/null || true)

# Issue #179: forensic watermark (mandatory, CLAUDE.md), same as the Kasm
# flow's custom_startup.sh: picom makes the overlay window genuinely
# transparent (without it overlay.py falls back to a glyph-shaped window), and
# the watchdog relaunches the overlay within about a second if it dies.
# Both are started in the background BEFORE the exec at the bottom of this
# file; the container is destroyed with the session, so nothing outlives it.
# Skipped, loudly, when the files are missing: a session without a watermark
# must never be silent. PICOM_CONF / WATERMARK_WATCHDOG are test-only seams.
PICOM_CONF="${PICOM_CONF:-/opt/watermark/picom.conf}"
WATERMARK_WATCHDOG="${WATERMARK_WATCHDOG:-/opt/watermark/watchdog.sh}"
if command -v picom >/dev/null 2>&1 && [ -f "$PICOM_CONF" ]; then
    (
        while true; do
            picom --config "$PICOM_CONF" >>/tmp/picom.log 2>&1
            sleep 5
        done
    ) >/dev/null 2>&1 3>&- &   # 3>&-: do not hold the BATS harness's fd 3 open (as custom_startup.sh does)
else
    echo "launch-session: WARNING picom not available -- the watermark falls back to its shaped (flickering) mode" >&2
fi
if [ -x "$WATERMARK_WATCHDOG" ]; then
    STUDENT_ID="$STUDENT_ID" SESSION_ID="$SESSION_ID" "$WATERMARK_WATCHDOG" &
else
    echo "launch-session: ERROR watermark watchdog missing at ${WATERMARK_WATCHDOG} -- this session has NO forensic watermark" >&2
fi

# Grading panel (issue #180): the Kasm flow's own panel window
# (grading_panel_window.py, a small WebKit2GTK 4.1 window), reused instead of a
# stock browser. Why: the previous version started epiphany with
# WEBKIT_FORCE_SANDBOX=0 (issue #52), but current WebKitGTK ignores that variable
# ("no longer allows disabling the sandbox"), so epiphany's web process crashed
# and the panel stayed a blank white window with the title "Blank page" (seen in
# a live run, in both this image and the one before it). The Kasm panel renders
# under this image's seccomp profile (checked live, with the screenshot in the PR),
# is locked down the same way as in Kasm, and takes the token through the
# environment instead of a URL fragment, so it is never on a command line.
# GRADING_PANEL_CMD is a test seam; arrange_windows.sh relaunches the panel with
# its own default command (the same script) if the student closes it.
GRADING_PANEL_CMD="${GRADING_PANEL_CMD:-python3 /opt/grading-panel/grading_panel_window.py}"
GRADING_PANEL_WIDTH="${GRADING_PANEL_WIDTH:-420}"
# shellcheck disable=SC2086  # GRADING_PANEL_CMD is a command line, split on purpose
VIEWER_URL="$VIEWER_URL" GRADING_TOKEN="$GRADING_TOKEN" STUDENT_ID="$STUDENT_ID" SESSION_ID="$SESSION_ID" \
    GRADING_PANEL_WIDTH="$GRADING_PANEL_WIDTH" $GRADING_PANEL_CMD &

# Issue #180: the Kasm flow's window loop (arrange_windows.sh): tiles Weasis and the
# panel side by side, restores minimised windows, relaunches a closed panel or Weasis
# (with the CURRENT study), and re-sends the study when the case changes (#185).
# openbox has no taskbar, so without this a minimised window would be lost, exactly
# as in the Kasm session (issue #151).
# A missing script is reported loudly but is not fatal: the session still works,
# only without tiling. ARRANGE_SCRIPT is a test seam.
ARRANGE_SCRIPT="${ARRANGE_SCRIPT:-/opt/grading-panel/arrange_windows.sh}"
WEASIS_BIN="${WEASIS_BIN:-/opt/weasis/bin/Weasis}"
if [ -x "$ARRANGE_SCRIPT" ]; then
    VIEWER_URL="$VIEWER_URL" ORTHANC_URL="$ORTHANC_URL" GRADING_TOKEN="$GRADING_TOKEN" \
        STUDENT_ID="$STUDENT_ID" SESSION_ID="$SESSION_ID" \
        ARRANGE_WEASIS_BIN="$WEASIS_BIN" ARRANGE_WEASIS_URI="$WEASIS_URI" WEASIS_URI_HELPER="$WEASIS_URI_HELPER" \
        GRADING_PANEL_WIDTH="$GRADING_PANEL_WIDTH" "$ARRANGE_SCRIPT" 3>&- &
else
    echo "launch-session: WARNING ${ARRANGE_SCRIPT} missing -- windows will not be tiled or relaunched" >&2
fi

WEASIS_BIN="${WEASIS_BIN:-/opt/weasis/bin/Weasis}"
if [ -n "$WEASIS_URI" ]; then
    exec "$WEASIS_BIN" "$WEASIS_URI"
else
    exec "$WEASIS_BIN"
fi
