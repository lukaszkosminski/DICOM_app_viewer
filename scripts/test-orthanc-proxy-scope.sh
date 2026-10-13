#!/usr/bin/env bash
# Blue-team item 0 (issue #195): measure what the Orthanc proxy on :8043 lets a
# session see, per the red-team task list (docs/RED_TEAM_TASKS.md, RT-T-01..04,
# delivered on PR #192). PUBLIC SAMPLE DATA ONLY -- throwaway stack,
# self-tearing-down. Never point this at a stack holding real data.
#
# Modes:
#   default          run every probe, PRINT the gap table, exit per result
#                    (gaps on legacy paths are the KNOWN open state until the
#                    callers switch + PR-B2 closes them; CI does not gate on
#                    this script yet)
#   --assert-scoped  same run; non-zero exit additionally means "a session
#                    could reach data it was not issued" -- the future gate.
#
# Evidence rules (from the task list): HTTP codes, counts, sizes, SHA-256 of
# UIDs -- never payloads, never tag values, never the token or the derived
# /s/<key>/ secret.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

ASSERT_SCOPED=0
[ "${1:-}" = "--assert-scoped" ] && ASSERT_SCOPED=1

PROXY="http://localhost:8043"
API="http://localhost:8080/api"
CREATED_ENV=0

cleanup() {
  echo "--- tearing down ---"
  docker compose down -v >/dev/null 2>&1 || true
  [ "$CREATED_ENV" -eq 1 ] && rm -f .env || true
}
trap cleanup EXIT

if [ ! -f .env ]; then
  echo "ORTHANC_PASSWORD=$(openssl rand -hex 16)" > .env
  CREATED_ENV=1
fi
grep -q '^GRADING_COORDINATOR_KEY=' .env || echo "GRADING_COORDINATOR_KEY=$(openssl rand -hex 32)" >> .env
# shellcheck disable=SC1091
source .env

echo "--- bring up the stack, load PUBLIC samples ---"
docker compose up -d --wait --wait-timeout 300 >/dev/null
./scripts/fetch-public-samples.sh >/dev/null
./scripts/load-sample-studies.sh >/dev/null

mint() {
  curl -sf -X POST "$API/session" -H "Content-Type: application/json" \
    -H "X-Coordinator-Key: ${GRADING_COORDINATOR_KEY}" \
    -d "{\"student_id\": \"$1\", \"session_id\": \"1\"}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])'
}
issued_uid() {
  curl -sf "$API/case" -H "X-Grading-Token: $1" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("orthanc_study_uid",""))'
}
key_of() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
sh_of()  { printf '%s' "$1" | sha256sum | cut -d' ' -f1 | cut -c1-12; }

TOK_A=$(mint BLUET_A) || { echo "mint failed"; exit 1; }
TOK_B=$(mint BLUET_B) || { echo "mint failed"; exit 1; }
UID_A=$(issued_uid "$TOK_A")
UID_B=$(issued_uid "$TOK_B")
KEY_A=$(key_of "$TOK_A")
if [ -z "$UID_A" ] || [ -z "$UID_B" ] || [ "$UID_A" = "$UID_B" ]; then
  echo "expected two distinct issued cases, got hashes '$(sh_of "$UID_A")' '$(sh_of "$UID_B")'"
  echo "(empty usually means the stage's case list is exhausted -- rerun after 'docker compose down -v')"
  exit 1
fi
echo "--- A holds <hash $(sh_of "$UID_A")>, B holds <hash $(sh_of "$UID_B")>; token/key never printed ---"

status=0
probe() { # probe <task> <class> <expect blocked|own-ok> <path> [extra curl args...]
  local task="$1" class="$2" expect="$3" path="$4"; shift 4
  local out c n verdict="ok"
  out=$(curl -s -o /dev/null -w "%{http_code} %{size_download}" "$@" "${PROXY}${path}")
  c="${out%% *}"; n="${out##* }"
  if [ "$expect" = "blocked" ] && [ "$c" = "200" ]; then verdict="REACHABLE(gap)"; status=2; fi
  if [ "$expect" = "own-ok" ] && [ "$c" != "200" ]; then verdict="SCOPED-BROKEN"; status=2; fi
  printf '%-8s %-6s %-46s code=%s bytes=%s %s\n' "$task" "$class" "${path:0:46}" "$c" "$n" "$verdict"
}

echo; echo "=== RT-T-01 enumeration ==="
probe RT-T-01 scoped blocked "/s/$KEY_A/dicom-web/studies"
probe RT-T-01 scoped own-ok  "/s/$KEY_A/dicom-web/studies?StudyInstanceUID=$UID_A"
probe RT-T-01 legacy blocked "/dicom-web/studies"
probe RT-T-01 legacy blocked "/dicom-web/patients"

echo; echo "=== RT-T-02 bulk retrieval (archive via legacy REST) ==="
IDS=$(curl -sf "$PROXY/studies" || echo "[]")
FIRST_ID=$(printf '%s' "$IDS" | python3 -c 'import json,sys
try: print(json.load(sys.stdin)[0])
except Exception: print("")')
if [ -n "$FIRST_ID" ]; then
  echo "legacy /studies listing returned $(printf '%s' "$IDS" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))') internal ids (count only)"
  probe RT-T-02 legacy blocked "/studies/$FIRST_ID/archive"
  probe RT-T-02 legacy blocked "/studies/$FIRST_ID/media"
  probe RT-T-02 scoped blocked "/s/$KEY_A/dicom-web/studies/$UID_B/archive"
else
  echo "RT-T-02 legacy (no /studies listing reachable -- enumeration already closed?)"
fi

echo; echo "=== RT-T-03 raw instance file + tags (first instance found via legacy REST) ==="
IID=""
if [ -n "$FIRST_ID" ]; then
  SID=$(curl -sf "$PROXY/studies/$FIRST_ID" | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["Series"][0])
except Exception: print("")')
  if [ -n "$SID" ]; then
    IID=$(curl -sf "$PROXY/series/$SID" | python3 -c 'import json,sys
try: print(json.load(sys.stdin)["Instances"][0])
except Exception: print("")')
  fi
fi
if [ -n "$IID" ]; then
  probe RT-T-03 legacy blocked "/instances/$IID/file"
  probe RT-T-03 legacy blocked "/instances/$IID/tags"
fi
probe RT-T-03 scoped blocked "/s/$KEY_A/dicom-web/studies/$UID_B/metadata"
probe RT-T-03 scoped own-ok  "/s/$KEY_A/dicom-web/studies/$UID_A/metadata"

echo; echo "=== RT-T-04 write/delete attempts ==="
probe RT-T-04 legacy blocked "/studies/${FIRST_ID:-none}" -X DELETE
probe RT-T-04 legacy blocked "/instances" -X POST --data-binary "@/dev/null"
probe RT-T-04 legacy blocked "/tools/find" -X POST -H "Content-Type: application/json" -d '{"Level":"Study","Query":{}}'

echo; echo "=== cross-session scoped probes (#196 core property) ==="
probe ITEM1 scoped blocked "/s/$KEY_A/dicom-web/studies/$UID_B/series"
probe ITEM1 scoped own-ok  "/s/$KEY_A/dicom-web/studies/$UID_A/series"

echo
if [ "$status" -ne 0 ]; then
  if [ "$ASSERT_SCOPED" -eq 1 ]; then
    echo "RESULT: FAILED (--assert-scoped): a session reached data it was not issued, or the scoped flow is broken."
  else
    echo "RESULT: gaps recorded above (exit 2). Known open state until callers switch + PR-B2; re-run with --assert-scoped once that lands."
  fi
fi
exit "$status"
