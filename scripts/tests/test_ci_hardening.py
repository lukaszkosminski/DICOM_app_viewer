"""CI hardening rules (issue #67), checked on the workflow files as text so they need no extra
dependency: least-privilege token, actions pinned to full commit SHAs, retrying and verified
downloads. A rule that is removed or weakened fails here, not silently in a later incident."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github/workflows").glob("*.yml"))
CI = (ROOT / ".github/workflows/ci.yml").read_text()


def test_there_are_workflows_to_check():
    assert WORKFLOWS


def test_every_workflow_declares_a_least_privilege_token():
    for wf in WORKFLOWS:
        text = wf.read_text()
        assert re.search(r"^permissions:\s*$", text, re.M) or re.search(r"^permissions:\s*\{\}", text, re.M), wf.name
    assert re.search(r"^permissions:\n  contents: read\n", CI, re.M)


def test_no_job_widens_the_token_beyond_read_only_without_saying_why():
    # a write permission in ci.yml would have to be a deliberate, reviewed change
    assert not re.search(r"^\s+(contents|packages|pull-requests|issues|actions|checks|id-token): write", CI, re.M)


def test_every_action_is_pinned_to_a_full_commit_sha():
    for wf in WORKFLOWS:
        for line in wf.read_text().splitlines():
            m = re.search(r"\buses:\s*(\S+)", line)
            if not m or m.group(1).startswith("./"):
                continue
            assert re.search(r"@[0-9a-f]{40}$", m.group(1)), f"{wf.name}: {line.strip()}"
            assert re.search(r"#\s*v\d", line), f"{wf.name}: the version comment is missing: {line.strip()}"


def test_the_pin_rule_rejects_mutable_tags():
    assert not re.search(r"@[0-9a-f]{40}$", "actions/checkout@v4")
    assert not re.search(r"@[0-9a-f]{40}$", "actions/checkout@main")


def test_dependabot_keeps_the_pinned_actions_current():
    cfg = (ROOT / ".github/dependabot.yml").read_text()
    assert 'package-ecosystem: "github-actions"' in cfg


def test_downloads_fail_loudly_retry_and_are_checksum_verified():
    for line in CI.splitlines():
        if re.search(r"^\s*curl\b", line) or " curl " in line:
            assert re.search(r"-\w*f\w*", line) and "--retry 5" in line and "--retry-all-errors" in line, line.strip()
    # every downloaded artefact is verified before use
    assert CI.count("sha256sum -c -") >= 2
    for name in ("hadolint", "trivy"):
        assert re.search(rf'echo "[0-9a-f]{{64}}  {name}', CI), name


def test_apt_calls_retry_transient_failures():
    for line in CI.splitlines():
        if "apt-get" in line and "update" in line:
            assert "Acquire::Retries=5" in line, line.strip()
        if "apt-get" in line and "install" in line:
            assert "Acquire::Retries=5" in line, line.strip()
