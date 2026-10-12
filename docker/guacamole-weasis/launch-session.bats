#!/usr/bin/env bats
# Tests for launch-session.sh (the Guacamole flow's launcher). Same
# stubbing technique as docker/kasm-workspace-weasis/custom_startup.bats
# -- no real grading-api, Weasis, or browser needed: WEASIS_BIN/
# GRADING_PANEL_CMD (test-only seams this script added specifically for this,
# unset in production) point at stubs that capture their own argv
# instead of actually launching anything, and a stub `curl` stands in for
# grading-api's /api/case response. The real python3 still runs for the
# actual dicom:rs URI-building and URL-encoding logic -- see this script's
# own header comment for why that behavior matters and isn't mocked away.
SCRIPT="$BATS_TEST_DIRNAME/launch-session.sh"

setup() {
  STUB_DIR="$BATS_TEST_TMPDIR/stub"
  mkdir -p "$STUB_DIR"

  cat > "$STUB_DIR/weasis" <<'EOF'
#!/usr/bin/env bash
: > "$WEASIS_ARGS_FILE"
for arg in "$@"; do printf '%s\n' "$arg" >> "$WEASIS_ARGS_FILE"; done
EOF
  chmod +x "$STUB_DIR/weasis"

  # The panel is a command that reads its settings from the environment (the token
  # never goes on a command line); the stub records them.
  cat > "$STUB_DIR/panel" <<'EOF'
#!/usr/bin/env bash
{
  echo "args=$*"
  echo "VIEWER_URL=$VIEWER_URL"
  echo "STUDENT_ID=$STUDENT_ID"
  echo "SESSION_ID=$SESSION_ID"
  echo "WIDTH=$GRADING_PANEL_WIDTH"
  echo "GRADING_TOKEN=$GRADING_TOKEN"
} > "$PANEL_ENV_FILE"
EOF
  chmod +x "$STUB_DIR/panel"

  cat > "$STUB_DIR/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$CURL_ARGS_FILE"
cat > "${CURL_STDIN_FILE:-/dev/null}"
if [ -n "${CURL_EXIT_CODE:-}" ] && [ "$CURL_EXIT_CODE" != "0" ]; then
  exit "$CURL_EXIT_CODE"
fi
printf '%s' "${CURL_RESPONSE_JSON:-{\}}"
EOF
  chmod +x "$STUB_DIR/curl"

  export PATH="$STUB_DIR:$PATH"
  export WEASIS_URI_HELPER="$BATS_TEST_DIRNAME/../kasm-workspace-weasis/weasis_case_uri.py"
  export WEASIS_BIN="$STUB_DIR/weasis"
  export GRADING_PANEL_CMD="$STUB_DIR/panel"
  export WEASIS_ARGS_FILE="$BATS_TEST_TMPDIR/weasis-args.txt"
  export PANEL_ENV_FILE="$BATS_TEST_TMPDIR/panel-env.txt"
  export CURL_ARGS_FILE="$BATS_TEST_TMPDIR/curl-args.txt"
  export CURL_STDIN_FILE="$BATS_TEST_TMPDIR/curl-stdin.txt"
  export VIEWER_URL="http://ipcmc-viewer:8080/"
  export ORTHANC_URL="http://ipcmc-viewer:8043/"
  export GRADING_TOKEN="test-token-abc"
  unset STUDENT_ID SESSION_ID CURL_EXIT_CODE CURL_RESPONSE_JSON
  # issue #102 retry loop: pin the backoff seam to 0 for fast tests
  # (production keeps 2s/4s).
  export API_RETRY_BACKOFF=0
}

@test "fails fast with a clear message when VIEWER_URL is unset" {
  unset VIEWER_URL
  run bash "$SCRIPT"
  [ "$status" -ne 0 ]
  [[ "$output" == *"VIEWER_URL"* ]]
}

@test "fails fast with a clear message when ORTHANC_URL is unset" {
  unset ORTHANC_URL
  run bash "$SCRIPT"
  [ "$status" -ne 0 ]
  [[ "$output" == *"ORTHANC_URL"* ]]
}

@test "fails fast with a clear message when GRADING_TOKEN is unset" {
  unset GRADING_TOKEN
  run bash "$SCRIPT"
  [ "$status" -ne 0 ]
  [[ "$output" == *"GRADING_TOKEN"* ]]
}

@test "authenticates the case lookup with the grading token, not a bare student_id" {
  export CURL_RESPONSE_JSON='{"complete": true}'
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  # issue #94: the token travels as an X-Grading-Token header fed via
  # --config on stdin -- present in the stdin header, absent from argv
  # (ps-invisible), absent from the URL (no query-string leak into logs).
  grep -qF -- 'header = "X-Grading-Token: test-token-abc"' "$CURL_STDIN_FILE"
  ! grep -qF -- "test-token-abc" "$CURL_ARGS_FILE"
  grep -qF -- "api/case" "$CURL_ARGS_FILE"
  ! grep -q -- "student_id=" "$CURL_ARGS_FILE"
}

@test "launches the assigned study with a correctly built dicom:rs URI" {
  export CURL_RESPONSE_JSON='{"complete": false, "orthanc_study_uid": "1.2.3.4"}'
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  uri=$(cat "$WEASIS_ARGS_FILE")
  [[ "$uri" == weasis://\?* ]]
  decoded=$(python3 -c "
import sys, urllib.parse
uri = sys.argv[1].removeprefix('weasis://?')
print(' '.join(urllib.parse.unquote(p) for p in uri.split('+')))
" "$uri")
  [[ "$decoded" == *'$dicom:rs'* ]]
  [[ "$decoded" == *'--url "http://ipcmc-viewer:8043/dicom-web"'* ]]
  [[ "$decoded" == *'requestType=STUDY&studyUID=1.2.3.4'* ]]
}

@test "falls back to a plain launch (no args) when the student has no case assigned" {
  export CURL_RESPONSE_JSON='{"complete": true}'
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [ ! -s "$WEASIS_ARGS_FILE" ]
}

@test "issue #102: unreachable grading-api retries 3x, logs loudly, never fakes complete" {
  export CURL_EXIT_CODE=7
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [ ! -s "$WEASIS_ARGS_FILE" ]
  [[ "$output" == *"grading-api unreachable"* ]]
  [[ "$output" == *"after 3 attempts"* ]]
  [[ "$output" != *"test-token-abc"* ]]
  local COUNTER="$BATS_TEST_TMPDIR/tries"
  printf '#!/usr/bin/env bash\nprintf x >> "%s"\nexit 7\n' "$COUNTER" > "$STUB_DIR/curl"
  chmod +x "$STUB_DIR/curl"
  rm -f "$COUNTER"
  run bash "$SCRIPT"
  [ "$(wc -c < "$COUNTER")" -eq 3 ]
}

@test "issue #180: the grading panel gets student, session, viewer URL, width and token through the ENVIRONMENT" {
  export STUDENT_ID="stu 1" SESSION_ID="sess_1" CURL_RESPONSE_JSON='{"complete": true}'
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  # backgrounded, so its stub's file write races the foreground script: poll briefly
  for _ in $(seq 1 30); do [ -s "$PANEL_ENV_FILE" ] && break; sleep 0.1; done
  grep -qx "VIEWER_URL=http://ipcmc-viewer:8080/" "$PANEL_ENV_FILE"
  grep -qx "STUDENT_ID=stu 1" "$PANEL_ENV_FILE"
  grep -qx "SESSION_ID=sess_1" "$PANEL_ENV_FILE"
  grep -qx "WIDTH=420" "$PANEL_ENV_FILE"
  grep -qx "GRADING_TOKEN=test-token-abc" "$PANEL_ENV_FILE"
}

@test "issue #180: the token is not on the panel's command line (issue #94: no token in argv or the URL)" {
  export CURL_RESPONSE_JSON='{"complete": true}'
  run bash "$SCRIPT"
  for _ in $(seq 1 30); do [ -s "$PANEL_ENV_FILE" ] && break; sleep 0.1; done
  grep -qx "args=" "$PANEL_ENV_FILE"
}

@test "issue #94 CR: refuses to launch when GRADING_TOKEN leaves the URL-safe alphabet" {
  export GRADING_TOKEN='evil"token\with-injectables'
  run bash "$SCRIPT"
  [ "$status" -ne 0 ]
  [[ "$output" == *"URL-safe"* ]]
  [ ! -s "$WEASIS_ARGS_FILE" ]
}

# ---- issue #179: forensic watermark (mandatory, CLAUDE.md) -----------------------
# The watchdog and picom are backgrounded, so their stub files race the foreground
# script: poll briefly (never sleep a fixed time) before asserting.
wait_for_file() { for _ in $(seq 1 30); do [ -s "$1" ] && return 0; sleep 0.1; done; return 1; }

watermark_stubs() {
  export WM_ENV_FILE="$BATS_TEST_TMPDIR/wm-env.txt" PICOM_ARGS_FILE="$BATS_TEST_TMPDIR/picom-args.txt"
  cat > "$STUB_DIR/watchdog.sh" <<'STUB'
#!/usr/bin/env bash
echo "STUDENT_ID=$STUDENT_ID SESSION_ID=$SESSION_ID" > "$WM_ENV_FILE"
STUB
  cat > "$STUB_DIR/picom" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$PICOM_ARGS_FILE"
sleep 30
STUB
  chmod +x "$STUB_DIR/watchdog.sh" "$STUB_DIR/picom"
  touch "$BATS_TEST_TMPDIR/picom.conf"
  export WATERMARK_WATCHDOG="$STUB_DIR/watchdog.sh" PICOM_CONF="$BATS_TEST_TMPDIR/picom.conf"
}

@test "issue #179: the watermark watchdog is started with the student and session ids" {
  watermark_stubs
  export STUDENT_ID="STU_42" SESSION_ID="sess_9" CURL_RESPONSE_JSON='{"complete": true}'
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  wait_for_file "$WM_ENV_FILE"
  [ "$(cat "$WM_ENV_FILE")" = "STUDENT_ID=STU_42 SESSION_ID=sess_9" ]
}

@test "issue #179: picom is started with the watermark compositor config" {
  watermark_stubs
  export CURL_RESPONSE_JSON='{"complete": true}'
  run bash "$SCRIPT"
  wait_for_file "$PICOM_ARGS_FILE"
  grep -qx -- "--config" "$PICOM_ARGS_FILE"
  grep -qxF "$PICOM_CONF" "$PICOM_ARGS_FILE"
}

@test "issue #179: a missing watchdog is reported loudly, never silently skipped, and the session still starts" {
  watermark_stubs
  export WATERMARK_WATCHDOG="$BATS_TEST_TMPDIR/does-not-exist.sh" CURL_RESPONSE_JSON='{"complete": true}'
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"NO forensic watermark"* ]]
}

@test "issue #179: without picom the watermark still starts, with a warning about the fallback" {
  watermark_stubs
  rm "$STUB_DIR/picom"
  export PATH="$STUB_DIR:/usr/bin:/bin"
  command -v picom && skip "a real picom is installed on this host"
  export CURL_RESPONSE_JSON='{"complete": true}'
  run bash "$SCRIPT"
  [[ "$output" == *"picom not available"* ]]
  wait_for_file "$WM_ENV_FILE"
}

# ---- issue #180: window layout, panel relaunch, case following (arrange_windows.sh) ---
arrange_stub() {
  export ARRANGE_ENV_FILE="$BATS_TEST_TMPDIR/arrange-env.txt"
  cat > "$STUB_DIR/arrange_windows.sh" <<'STUB'
#!/usr/bin/env bash
{
  echo "WEASIS_URI=$ARRANGE_WEASIS_URI"
  echo "WEASIS_BIN=$ARRANGE_WEASIS_BIN"
  echo "STUDENT=$STUDENT_ID VIEWER=$VIEWER_URL ORTHANC=$ORTHANC_URL"
  echo "HAS_TOKEN=$([ -n "$GRADING_TOKEN" ] && echo yes || echo no)"
} > "$ARRANGE_ENV_FILE"
STUB
  chmod +x "$STUB_DIR/arrange_windows.sh"
  export ARRANGE_SCRIPT="$STUB_DIR/arrange_windows.sh"
}

@test "issue #180: the window loop gets the study URI and the API settings" {
  arrange_stub
  export STUDENT_ID="STU_7" SESSION_ID="s1"
  export CURL_RESPONSE_JSON='{"orthanc_study_uid":"1.2.840.1"}'
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  wait_for_file "$ARRANGE_ENV_FILE"
  grep -q "^WEASIS_URI=weasis://" "$ARRANGE_ENV_FILE"
  grep -q "^WEASIS_BIN=$WEASIS_BIN$" "$ARRANGE_ENV_FILE"
  grep -q "STUDENT=STU_7 VIEWER=http://ipcmc-viewer:8080/ ORTHANC=http://ipcmc-viewer:8043/" "$ARRANGE_ENV_FILE"
  grep -q "^HAS_TOKEN=yes$" "$ARRANGE_ENV_FILE"
}

@test "issue #180: the window loop gets an EMPTY study URI when the case is complete (a plain Weasis is relaunched)" {
  arrange_stub
  export CURL_RESPONSE_JSON='{"complete": true}'
  run bash "$SCRIPT"
  wait_for_file "$ARRANGE_ENV_FILE"
  grep -q "^WEASIS_URI=$" "$ARRANGE_ENV_FILE"
}

@test "issue #180: a missing window-loop script is reported loudly and the session still starts" {
  export ARRANGE_SCRIPT="$BATS_TEST_TMPDIR/nope.sh" CURL_RESPONSE_JSON='{"complete": true}'
  run bash "$SCRIPT"
  [ "$status" -eq 0 ]
  [[ "$output" == *"windows will not be tiled or relaunched"* ]]
}

@test "issue #180: the window loop gets the token only through its environment" {
  arrange_stub
  export CURL_RESPONSE_JSON='{"complete": true}'
  run bash "$SCRIPT"
  wait_for_file "$ARRANGE_ENV_FILE"
  # passed through the environment only; the stub records just whether it is set
  grep -qx "HAS_TOKEN=yes" "$ARRANGE_ENV_FILE"
  ! grep -q "test-token-abc" "$ARRANGE_ENV_FILE"
}
