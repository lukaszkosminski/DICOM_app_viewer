"""
Full browser-driven E2E test for the Guacamole PoC flow: provisions a
real session (running scripts/provision-guacamole-session.py as a
subprocess, exercising the exact same code path a real coordinator would
use, not a mocked/simplified version of it), drives a real headless
Chromium browser to the returned auto-login link, and confirms:
  - Guacamole's own auto-login (#/?username=...&password=...) actually
    logs the student straight in with no login form shown -- this was a
    real open question before this test existed: research confirmed the
    mechanism is documented, but never confirmed it against this exact
    Guacamole version/setup.
  - The remote-display canvas actually renders real, non-blank pixel
    data -- confirming the whole guacd -> TigerVNC -> Weasis/epiphany
    pipeline works end to end through a real browser, not just the
    lower-level Guacamole-protocol test used earlier to confirm
    KasmVNC's incompatibility and TigerVNC's compatibility.

Needs the full stack running (docker compose -f docker-compose.yml -f
docker-compose.guacamole.yml up -d) and docker/guacamole-weasis:poc built
-- this is NOT a unit test, it's the real thing, same spirit as
scripts/smoke-test.sh for the Kasm flow. Teardown
(scripts/teardown-guacamole-session.py) always runs, even on failure, so
a failed run doesn't leave an orphaned container/Guacamole user behind.
"""
import json
import os
import subprocess
import sys
import time

import pytest
from playwright.sync_api import sync_playwright

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


@pytest.fixture
def provisioned_session():
    student_id = f"E2E_TEST_{int(time.time())}"
    # session id must be digits-only (provisioner enforces the guard's
    # \d{1,20} shape; the old literal "e2e" is now refused up front).
    session_id = str(int(time.time()))
    result = subprocess.run(
        [
            sys.executable,
            os.path.join(REPO_ROOT, "scripts", "provision-guacamole-session.py"),
            "--student-id", student_id, "--session-id", session_id,
        ],
        capture_output=True, text=True, env=os.environ.copy(),
    )
    assert result.returncode == 0, f"provisioning failed: {result.stderr}"
    data = json.loads(result.stdout)
    try:
        yield data
    finally:
        subprocess.run(
            [
                sys.executable,
                os.path.join(REPO_ROOT, "scripts", "teardown-guacamole-session.py"),
                "--student-id", student_id, "--session-id", session_id,
            ],
            capture_output=True, text=True, env=os.environ.copy(), check=False,
        )
        # the 0600 credential file must not linger after the e2e run
        # (teardown already removes it when the configured link dir
        # matches; this covers link dirs the fixture ran with)
        link_file = data.get("link_file")
        if link_file and os.path.exists(link_file):
            os.remove(link_file)


def test_auto_login_link_reaches_the_client_with_no_login_form(provisioned_session):
    # #191: the link lives in the 0600 file provision stdout named, never
    # in stdout itself.
    with open(provisioned_session["link_file"], encoding="utf-8") as fh:
        link = fh.read().strip()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(link)
            # Guacamole's client is a real Angular SPA -- give its own
            # auth + connection-list load a real moment, not an instant
            # assertion.
            page.wait_for_timeout(3000)
            login_form_visible = page.locator("input[name='username']").count() > 0
            assert not login_form_visible, (
                "auto-login link showed a login form instead of logging straight in"
            )
        finally:
            browser.close()


def test_connection_renders_real_display_data(provisioned_session):
    """The actual end-to-end proof: confirm the remote-display canvas
    shows real, non-blank pixel data. No explicit click into a connection
    list is needed -- confirmed empirically (not assumed): since this
    student has exactly one connection granted, Guacamole's client
    auto-connects straight into it after login, landing directly on a
    real, correctly-rendered Weasis session (visually confirmed via a
    saved screenshot during development showing the assigned CT_small
    study rendered correctly, not just a non-blank canvas)."""
    with open(provisioned_session["link_file"], encoding="utf-8") as fh:
        link = fh.read().strip()
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(link)
            # Real display rendering needs real time: TigerVNC/X11
            # startup, Weasis's own JVM+OSGi startup, the case fetch --
            # generous but bounded.
            page.wait_for_timeout(20000)
            canvas = page.locator("canvas").first
            assert canvas.count() > 0, "no remote-display canvas ever appeared"
            screenshot = canvas.screenshot()
            # Crude but effective non-blank check: a canvas rendering a
            # real desktop image encodes to a meaningfully larger PNG than
            # an empty/transparent placeholder would.
            assert len(screenshot) > 5000, (
                "canvas screenshot looks blank/trivial, not a real rendered desktop"
            )
        finally:
            browser.close()
