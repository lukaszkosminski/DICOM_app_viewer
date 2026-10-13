"""Repository state for reproducible builds (issue #105). Mirrors the lint.sh gate so a
regression fails in the unit-test stage too, and tests the rules on synthetic inputs."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = sorted(ROOT.glob("docker/*/Dockerfile"))
FROM_RE = re.compile(r"^FROM\s+\S+@sha256:[0-9a-f]{64}(\s+AS\s+\S+)?\s*$", re.M)
LOCK = ROOT / "docker/grading-api/requirements.lock"


def _from_lines(text):
    return [line for line in text.splitlines() if line.startswith("FROM ")]


def test_there_are_dockerfiles_to_check():
    assert len(DOCKERFILES) >= 5


def test_every_base_image_is_pinned_by_tag_and_digest():
    for df in DOCKERFILES:
        lines = _from_lines(df.read_text())
        assert lines, df
        for line in lines:
            assert re.match(r"^FROM\s+\S+:\S+@sha256:[0-9a-f]{64}", line), f"{df}: {line}"


def test_the_pin_rule_rejects_a_bare_tag_and_a_short_digest():
    assert not FROM_RE.search("FROM python:3.12-slim\n")
    assert not FROM_RE.search("FROM python:3.12-slim@sha256:abc\n")
    assert FROM_RE.search("FROM python:3.12-slim@sha256:" + "a" * 64 + "\n")


def test_lockfile_pins_every_direct_dependency_with_hashes():
    lock = LOCK.read_text()
    reqs = [r.strip() for r in (ROOT / "docker/grading-api/requirements.txt").read_text().splitlines() if r.strip() and not r.startswith("#")]
    assert reqs
    for req in reqs:
        assert re.search(rf"^{re.escape(req)}\s+\\$", lock, re.M), req
    # every locked package carries at least one hash
    blocks = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", lock)
    for block in blocks:
        if re.match(r"[A-Za-z0-9_.-]+==", block):
            assert "--hash=sha256:" in block, block.splitlines()[0]


def test_grading_api_installs_only_hashed_requirements_and_never_upgrades_the_os():
    text = (ROOT / "docker/grading-api/Dockerfile").read_text()
    assert "--require-hashes" in text and "requirements.lock" in text
    assert not re.search(r"^\s*RUN.*apt-get\s+(dist-)?upgrade", text, re.M)


def test_every_curl_download_in_a_dockerfile_fails_loudly_and_retries():
    """A flaky GitHub release download failed a PR build while the same commit passed in the push
    run (2026-10-12); a download inside a Dockerfile must retry transient errors."""
    seen = 0
    for df in DOCKERFILES:
        text = df.read_text()
        for m in re.finditer(r"curl\s+(-[A-Za-z]+\s+)+[^\n]*", text):
            line = m.group(0)
            if "-o " not in line and "|" not in line:
                continue  # the apt package name `curl` in an install list, not a download
            seen += 1
            assert "--retry 5" in line and "--retry-all-errors" in line, f"{df}: {line}"
            assert re.search(r"-\w*f", line), f"{df}: {line}"
    assert seen >= 2
