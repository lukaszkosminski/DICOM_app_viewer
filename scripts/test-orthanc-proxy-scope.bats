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
  [ -n "${STUB_DIR:-}" ] && rm -rf "$STUB_DIR"
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
