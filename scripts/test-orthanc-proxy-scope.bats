#!/usr/bin/env bats
# Guard test for scripts/test-orthanc-proxy-scope.sh (p2 safety review of
# PR #214): the script must REFUSE to start while any ipcmc-* container
# exists (the live real-data stack uses fixed names and host ports), and
# must pass cleanly when none exist. No Docker daemon needed -- docker is
# stubbed via DOCKER_BIN + PATH.

stub_docker() {
  STUB_DIR="$(mktemp -d)"
  cat > "$STUB_DIR/docker" <<STUB
#!/usr/bin/env bash
case "\$1 \$2" in
  "ps -a") echo "$1" ;;
  *) exit 0 ;;
esac
STUB
  chmod +x "$STUB_DIR/docker"
}

teardown() {
  rm -rf "${STUB_DIR:-}" 2>/dev/null || true
}

@test "preflight refuses while an ipcmc-* container exists (exit 2, names untouched)" {
  stub_docker "ipcmc-orthanc
bluet-scope-grading-api-1"
  run env DOCKER_BIN="$STUB_DIR/docker" bash "$BATS_TEST_DIRNAME/test-orthanc-proxy-scope.sh" --preflight
  [ "$status" -eq 2 ]
  [ "${lines[0]}" = "REFUSING to start: containers named ipcmc-* already exist (live or leftover stack)." ]
  [[ "$output" != *"guard: clear"* ]]
}

@test "preflight passes when no ipcmc-* container exists" {
  stub_docker "some-unrelated-container
kasm_foo"
  run env DOCKER_BIN="$STUB_DIR/docker" bash "$BATS_TEST_DIRNAME/test-orthanc-proxy-scope.sh" --preflight
  [ "$status" -eq 0 ]
  [[ "$output" == *"guard: clear"* ]]
}

@test "preflight passes on an empty docker ps (stubbed empty listing)" {
  stub_docker ""
  run env DOCKER_BIN="$STUB_DIR/docker" bash "$BATS_TEST_DIRNAME/test-orthanc-proxy-scope.sh" --preflight
  [ "$status" -eq 0 ]
  [[ "$output" == *"guard: clear"* ]]
}

# -- p2 review of #214: evidence rule + exit contract ---------------------

@test "masking: printed scoped lines carry no 64-hex key and no /s/<hex>" {
  run bash "$BATS_TEST_DIRNAME/test-orthanc-proxy-scope.sh" --selftest-mask
  [ "$status" -eq 0 ]
  [[ "$output" == *"/s/<key>/"* ]]
  [[ "$output" == *"/<id>"* ]]
  ! grep -qE "[0-9a-f]{16}" <<<"$output"
  ! grep -qE "/s/[0-9a-f]" <<<"$output"
}

@test "exit contract: gaps recorded in default mode exit 0" {
  run bash "$BATS_TEST_DIRNAME/test-orthanc-proxy-scope.sh" --selftest-exit 2 0
  [ "$status" -eq 0 ]
}

@test "exit contract: gaps with --assert-scoped exit 1" {
  run bash "$BATS_TEST_DIRNAME/test-orthanc-proxy-scope.sh" --selftest-exit 2 1
  [ "$status" -eq 1 ]
}

@test "exit contract: no gaps exits 0 even with --assert-scoped" {
  run bash "$BATS_TEST_DIRNAME/test-orthanc-proxy-scope.sh" --selftest-exit 0 1
  [ "$status" -eq 0 ]
}

@test "usage errors exit 2 (bad flag, non-integer selftest args)" {
  run bash "$BATS_TEST_DIRNAME/test-orthanc-proxy-scope.sh" --bogus
  [ "$status" -eq 2 ]
  run bash "$BATS_TEST_DIRNAME/test-orthanc-proxy-scope.sh" --selftest-exit x 0
  [ "$status" -eq 2 ]
}
