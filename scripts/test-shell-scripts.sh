#!/usr/bin/env bash
# BATS tests for the shell scripts with real logic worth verifying beyond
# scripts/lint.sh's bash -n syntax check (issue #16) -- see each .bats
# file's own header comment for what it covers and why. No Docker, no real
# Kasm/Weasis/grading-api needed: the scripts under test have small,
# test-only seams (CHROME_BIN/WEASIS_BIN/OVERLAY_SCRIPT/WATCHDOG_LOG env
# vars, unset in production) that let these tests stub the real binary,
# a fake `curl`, and a fake overlay script instead.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v bats >/dev/null 2>&1; then
  echo "bats not found -- install it first (e.g. 'sudo apt-get install bats' on Debian/Ubuntu; see https://bats-core.readthedocs.io/en/stable/installation.html)" >&2
  exit 1
fi

bats \
  docker/kasm-workspace/custom_startup.bats \
  docker/kasm-workspace-weasis/custom_startup.bats \
  docker/kasm-workspace-weasis/watchdog.bats \
  docker/kasm-workspace-weasis/arrange_windows.bats \
  docker/guacamole-weasis/launch-session.bats \
  scripts/load-local-studies.bats \
  scripts/test-orthanc-proxy-scope.bats \
  scripts/ci-gate.bats \
  "$@"
