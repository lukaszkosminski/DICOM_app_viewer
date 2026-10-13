#!/usr/bin/env bash
# Degraded-mode E2E tests (issue #115): for every critical dependency,
# verify the behaviour documented in docs/DEGRADED_MODES.md actually holds.
# Compose-based, same bootstrap/teardown conventions as smoke-test.sh --
# runnable identically locally and in CI (.github/workflows ci.yml, job
# "degraded-test").
#
# Rows covered here: grading-api down, SQLite locked past busy_timeout,
# Orthanc down (through the :8043 proxy), nginx rate limit hit. The Kasm
# startup row's fail-open assertion (#102: API failure is never reported
# as complete) lives in docker/kasm-workspace-weasis/custom_startup.bats
# (needs no compose); Kasm/Guacamole availability and the CE cap need
# real infrastructure, same exclusion as smoke-test.sh's own header.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

status=0
CREATED_ENV=0
CREATED_NETWORK=0

cleanup() {
  echo "--- tearing down ---"
  docker compose down -v >/dev/null 2>&1
  [ "$CREATED_ENV" -eq 1 ] && rm -f .env
  [ "$CREATED_NETWORK" -eq 1 ] && docker network rm kasm_default_network >/dev/null 2>&1
}
trap cleanup EXIT

if [ ! -f .env ]; then
  echo "ORTHANC_PASSWORD=$(openssl rand -hex 16)" > .env
  CREATED_ENV=1
fi
grep -q '^GRADING_COORDINATOR_KEY=' .env || echo "GRADING_COORDINATOR_KEY=$(openssl rand -hex 32)" >> .env
# shellcheck disable=SC1091
source .env

if ! docker network inspect kasm_default_network >/dev/null 2>&1; then
  docker network create kasm_default_network >/dev/null
  CREATED_NETWORK=1
fi

echo "--- docker compose up ---"
docker compose up -d --build || { echo "compose up failed"; exit 1; }

# --- helpers -------------------------------------------------------------

# wait_for <want-code(s)> <max-tries> <curl args...>: poll until the
# code matches (compose up -d returns before anything is serving, same
# lesson smoke-test.sh learned) or the budget runs out; echoes the last
# code seen. <want> is either a single code or a "|502|504|" style set.
wait_for() {
  local want="$1" tries="$2"; shift 2
  local code=""
  for _ in $(seq 1 "$tries"); do
    code=$(curl -s --max-time 80 -o /dev/null -w '%{http_code}' "$@" 2>/dev/null)
    [[ "$want" == *"|${code}|"* || "$code" == "$want" ]] && break
    sleep 2
  done
  echo "$code"
}

pass() { echo "--- $1: OK ($2) ---"; }
fail() { echo "--- $1: FAIL ($2) ---"; status=1; }

# Every scenario ends by bringing the stack fully back up; these two
# recovery waits gate the next scenario on real recovery, not sleep luck.
wait_api_up() { wait_for 200 30 http://localhost:8080/api/healthz; }
wait_orthanc_up() { wait_for 200 30 -u "orthanc:${ORTHANC_PASSWORD}" http://localhost:8042/system; }

echo "--- initial health ---"
INIT=$(wait_for 200 30 http://localhost:8080/api/healthz)
[ "$INIT" = 200 ] && pass "stack came up" "HTTP $INIT" || { fail "stack came up" "HTTP $INIT"; exit 1; }

# --- 1. rate limit hit (docs/DEGRADED_MODES.md "nginx rate limit") -------
# 60 concurrent requests at the burst: must produce 429s, and -- the load
# assertion of issue #115 -- ZERO 5xx: overload is answered at the edge,
# never cascaded into the app. Also pins today's documented header gap:
# no Retry-After on 429 (add one later and this test fails until the doc
# is updated, which is the point).
echo "--- 1. rate limit: 60-request burst ---"
sleep 3  # let the token bucket settle after startup traffic
BURST_DIR=$(mktemp -d)
for _ in $(seq 1 60); do
  curl -s --max-time 15 -D "$BURST_DIR/h_$RANDOM$_.txt" -o /dev/null -w '%{http_code}\n' \
    http://localhost:8080/api/healthz >> "$BURST_DIR/codes.txt" &
done
wait
COUNT_200=$(grep -c '^200$' "$BURST_DIR/codes.txt" || true)
COUNT_429=$(grep -c '^429$' "$BURST_DIR/codes.txt" || true)
COUNT_5XX=$(grep -cE '^5[0-9][0-9]$' "$BURST_DIR/codes.txt" || true)
if [ "$COUNT_5XX" -eq 0 ] && [ "$COUNT_429" -ge 1 ] && [ "$COUNT_200" -ge 1 ]; then
  pass "burst: 200 + 429, zero 5xx" "200=$COUNT_200 429=$COUNT_429 5xx=$COUNT_5XX"
else
  fail "burst: 200 + 429, zero 5xx" "200=$COUNT_200 429=$COUNT_429 5xx=$COUNT_5XX"
fi
# The 429 must come from nginx's own edge handler (its HTML body), never
# from a proxy loop; and no Retry-After today (documented gap).
FIRST_429_HDR=""
for f in "$BURST_DIR"/h_*.txt; do
  grep -q '^HTTP/[0-9.]* 429' "$f" && { FIRST_429_HDR="$f"; break; }
done
if [ -n "$FIRST_429_HDR" ] && ! grep -qi 'retry-after' "$FIRST_429_HDR"; then
  pass "429 sent with no Retry-After (documented gap pinned)" "headers of one captured 429"
else
  fail "429 sent with no Retry-After (documented gap pinned)" "Retry-After seen or no 429 captured"
fi
rm -rf "$BURST_DIR"
RECOVER=$(wait_api_up)
[ "$RECOVER" = 200 ] && pass "rate limit releases after burst" "HTTP $RECOVER" || fail "rate limit releases after burst" "HTTP $RECOVER"

# --- 2. SQLite locked past busy_timeout ----------------------------------
# A second connection holds BEGIN EXCLUSIVE for 8 s while a write request
# (POST /api/session, the cheapest authenticated write) goes through the
# app. Expectation from the doc: wait out busy_timeout=5000, then the
# standard 500 error envelope -- and afterwards the same write succeeds,
# proving nothing was half-applied.
echo "--- 2. SQLite locked past busy_timeout ---"
docker exec -d ipcmc-grading-api python3 -c \
  "import sqlite3, time; c = sqlite3.connect('${GRADING_DB_PATH:-/data/grading.db}', timeout=0); c.execute('BEGIN EXCLUSIVE'); time.sleep(8)"
sleep 1
LOCK_JSON=$(mktemp)
LOCK_CODE=$(curl -s --max-time 30 -o "$LOCK_JSON" -w '%{http_code} %{time_total}' \
  -X POST http://localhost:8080/api/session -H 'Content-Type: application/json' \
  -H "X-Coordinator-Key: ${GRADING_COORDINATOR_KEY}" \
  -d "{\"student_id\": \"DEGRADED_LOCK_$$\", \"session_id\": \"dl1\"}")
LOCK_STATUS=${LOCK_CODE%% *}
LOCK_TIME=${LOCK_CODE##* }
LOCK_WAIT_OK=$(python3 -c "print(1 if float('$LOCK_TIME') >= 4.0 else 0)")
if [ "$LOCK_STATUS" = 500 ] && [ "$LOCK_WAIT_OK" = 1 ] && grep -q '"error_code"' "$LOCK_JSON"; then
  pass "write waited for busy_timeout, then clean 500 envelope" "500 after ${LOCK_TIME}s"
else
  fail "write waited for busy_timeout, then clean 500 envelope" "code=$LOCK_STATUS time=${LOCK_TIME}s body=$(head -c 80 "$LOCK_JSON")"
fi
rm -f "$LOCK_JSON"
sleep 8  # the holder sleeps 8 s in total; this ends it
POST_LOCK=$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST http://localhost:8080/api/session -H 'Content-Type: application/json' \
  -H "X-Coordinator-Key: ${GRADING_COORDINATOR_KEY}" \
  -d "{\"student_id\": \"DEGRADED_LOCK_$$\", \"session_id\": \"dl1\"}")
[ "$POST_LOCK" = 200 ] && pass "same write succeeds once the lock is gone (no state lost)" "HTTP $POST_LOCK" || fail "same write succeeds once the lock is gone (no state lost)" "HTTP $POST_LOCK"

# --- 3. grading-api down --------------------------------------------------
# The edge must say "gateway error" -- never success, never a JSON shape
# that could be mistaken for a real answer -- while the rest of the
# viewer keeps serving (the isolation claim in the doc).
echo "--- 3. grading-api down ---"
docker compose stop grading-api >/dev/null 2>&1
sleep 2
DOWN_CODE=$(wait_for "|502|504|" 15 http://localhost:8080/api/case)
if [[ "|502|504|" == *"|$DOWN_CODE|"* ]]; then
  pass "/api/case answers a gateway error, not success" "HTTP $DOWN_CODE"
else
  fail "/api/case answers a gateway error, not success" "HTTP $DOWN_CODE (expected 502/504)"
fi
STATIC_CODE=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/)
[ "$STATIC_CODE" = 200 ] && pass "viewer static pages unaffected" "HTTP $STATIC_CODE" || fail "viewer static pages unaffected" "HTTP $STATIC_CODE"
docker compose start grading-api >/dev/null 2>&1
RECOVER=$(wait_api_up)
[ "$RECOVER" = 200 ] && pass "grading-api recovered" "HTTP $RECOVER" || fail "grading-api recovered" "HTTP $RECOVER"

# --- 4. Orthanc down (through the auth proxy) -----------------------------
# The student-visible symptom is an empty iframe; the server-side truth
# the doc commits to is "the proxy says 502/504 quickly-ish, and grading
# keeps working independently of Orthanc".
echo "--- 4. Orthanc down ---"
docker compose stop orthanc >/dev/null 2>&1
sleep 2
# nginx takes ~14 s (DNS gone) and up to ~60 s (stale address, connect
# timeout) -- both documented, both under the 75 s cap here. A hang
# beyond nginx's own machinery is a different bug and must fail here.
ORTHO_CODE=$(curl -s --max-time 75 -o /dev/null -w '%{http_code}' http://localhost:8043/system || echo 000)
if [[ "|502|504|" == *"|$ORTHO_CODE|"* ]]; then
  pass "auth proxy answers a gateway error for a dead Orthanc" "HTTP $ORTHO_CODE"
else
  fail "auth proxy answers a gateway error for a dead Orthanc" "HTTP $ORTHO_CODE (expected 502/504)"
fi
API_ALIVE=$(wait_for 200 15 http://localhost:8080/api/healthz)
[ "$API_ALIVE" = 200 ] && pass "grading unaffected by Orthanc outage" "HTTP $API_ALIVE" || fail "grading unaffected by Orthanc outage" "HTTP $API_ALIVE"
docker compose start orthanc >/dev/null 2>&1
RECOVER=$(wait_orthanc_up)
[ "$RECOVER" = 200 ] && pass "Orthanc recovered" "HTTP $RECOVER" || fail "Orthanc recovered" "HTTP $RECOVER"

if [ "$status" -eq 0 ]; then
  echo "All degraded-mode checks passed (see docs/DEGRADED_MODES.md for the contract under test)."
else
  echo "One or more degraded-mode checks FAILED (see above)."
fi
exit "$status"
