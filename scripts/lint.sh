#!/usr/bin/env bash
# "Lint & Format" stage of the CI pipeline -- fast checks needing no running
# infrastructure: bash syntax, compose file validity, and grading-api's own
# Python lint/format (ruff). Run this locally before pushing; also runs in
# CI (.github/workflows/ci.yml). Complements scripts/smoke-test.sh, which
# actually brings the stack up.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

status=0

echo "--- bash syntax check ---"
while IFS= read -r -d '' f; do
  echo "  $f"
  bash -n "$f" || status=1
done < <(find . \( -name "*.sh" -o -name "*.envsh" -o -name "xstartup" \) -not -path "./.git/*" -not -path "*/.venv-test/*" -print0)

echo "--- docker compose config validation ---"
# A placeholder value is fine here -- config only renders/validates the
# compose model, it doesn't start anything or need a real credential.
# Confirmed separately that `config` does NOT require kasm_default_network
# (docker-compose.yml's external network) to actually exist.
export ORTHANC_PASSWORD="lint-only-placeholder-not-a-real-secret"
export GRADING_COORDINATOR_KEY="lint-only-placeholder-not-a-real-secret"
export GUACAMOLE_DB_PASSWORD="lint-only-placeholder-not-a-real-secret"
docker compose -f docker-compose.yml config >/dev/null || status=1
docker compose -f docker-compose.remote-host.yml config >/dev/null || status=1
docker compose -f docker-compose.yml -f docker-compose.guacamole.yml config >/dev/null || status=1

echo "--- pinned external images: tag + immutable digest (issue #50) ---"
# Every `image:` line in the two DEPLOYMENT compose files must pin
# `name:tag@sha256:<64 hex>`. Today only orthanc is image-based
# (grading-api/viewer are build:) -- if a future external image appears it
# is covered by the same rule with no exemption list, by design.
# `:latest` as the tag is rejected even with a digest: a mutable tag on a
# graded-cohort service invites "but WHICH build did cohort X run" disputes.
# The orthanc pin must be byte-identical across both files (drift between
# local-dev and remote-host deployments is its own silent-upgrade bug).
pin_ok=1
while IFS= read -r line; do
  stripped=${line#*: }
  case "$stripped" in
    *"@sha256:"*)
      case "$stripped" in
        *":latest@"*)
          echo "image tagged :latest even though digest-pinned: $stripped" >&2
          pin_ok=0 ;;
      esac ;;
    *)
      echo "UNPINNED image (needs name:tag@sha256:<digest>): $stripped" >&2
      pin_ok=0 ;;
  esac
done < <(grep -hE '^[[:space:]]*image:[[:space:]]' docker-compose.yml docker-compose.remote-host.yml)

orthanc_main=$(grep -E '^[[:space:]]*image:[[:space:]]*orthancteam/orthanc:' docker-compose.yml | tr -d '[:space:]')
orthanc_remote=$(grep -E '^[[:space:]]*image:[[:space:]]*orthancteam/orthanc:' docker-compose.remote-host.yml | tr -d '[:space:]')
if [ "$orthanc_main" != "$orthanc_remote" ]; then
  echo "orthanc pin differs between docker-compose.yml and docker-compose.remote-host.yml:" >&2
  echo "  $orthanc_main" >&2
  echo "  $orthanc_remote" >&2
  pin_ok=0
fi
[ "$pin_ok" -eq 1 ] || status=1

echo "--- reproducible builds (issue #105): digest-pinned base images + hashed lockfile ---"
# Every FROM in docker/*/Dockerfile must carry @sha256:<64 hex>. Update procedure:
# docs/REPRODUCIBLE_BUILDS.md.
repro_ok=1
for df in docker/*/Dockerfile; do
  while IFS= read -r line; do
    case "$line" in
      *"@sha256:"*) ;;
      *) echo "UNPINNED base image in $df (needs name:tag@sha256:<digest>): $line" >&2; repro_ok=0 ;;
    esac
  done < <(grep -E '^FROM[[:space:]]' "$df")
done
# requirements.lock must contain every direct dependency of requirements.txt at the same
# version, with hashes, and the Dockerfile must install it with --require-hashes.
while IFS= read -r req; do
  case "$req" in ""|"#"*) continue ;; esac
  if ! grep -qE "^${req//./\\.}[[:space:]]+\\\\$" docker/grading-api/requirements.lock; then
    echo "requirements.lock does not pin '$req' with hashes -- regenerate it (docs/REPRODUCIBLE_BUILDS.md)" >&2
    repro_ok=0
  fi
done < docker/grading-api/requirements.txt
grep -q -- '--require-hashes' docker/grading-api/Dockerfile || { echo "grading-api Dockerfile must install with --require-hashes" >&2; repro_ok=0; }
grep -qE '^[[:space:]]*RUN.*apt-get[[:space:]]+(dist-)?upgrade' docker/grading-api/Dockerfile && { echo "grading-api Dockerfile must not run apt-get upgrade (issue #105)" >&2; repro_ok=0; }
[ "$repro_ok" -eq 1 ] || status=1

echo "--- Weasis pin sync (issue #50): both Dockerfiles must carry the same version + sha256 ---"
weasis_pin() { grep -hE '^ARG WEASIS_(VERSION|SHA256)=' "$1" | sort; }
kasm_pin="$(weasis_pin docker/kasm-workspace-weasis/Dockerfile)"
guac_pin="$(weasis_pin docker/guacamole-weasis/Dockerfile)"
if [ -z "$kasm_pin" ] || [ "$kasm_pin" != "$guac_pin" ]; then
  echo "FAIL: WEASIS_VERSION/WEASIS_SHA256 missing or differ between docker/kasm-workspace-weasis and docker/guacamole-weasis" >&2
  status=1
fi

echo "--- text style (docs/STYLE.md): no pictographs, no CJK, ASCII dashes in code, English prose ---"
python3 scripts/check-text-style.py || status=1

echo "--- ruff check + format --check (docker/grading-api) ---"
if command -v ruff >/dev/null 2>&1; then
  ruff check docker/grading-api || status=1
  ruff format --check docker/grading-api || status=1
else
  echo "ruff not found -- install it first: pip install -r docker/grading-api/requirements-dev.txt (in a venv)" >&2
  status=1
fi

if [ "$status" -eq 0 ]; then
  echo "All lint checks passed."
else
  echo "One or more lint checks FAILED (see above)."
fi
exit "$status"
