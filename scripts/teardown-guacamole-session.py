#!/usr/bin/env python3
"""
Tears down a Guacamole PoC session minted by
scripts/provision-guacamole-session.py: stops+removes the per-student
container, deletes the Guacamole user + connection it created, and (when
a coordinator key is configured) revokes the grading-api token.

Thin CLI over scripts/guacamole_session.py -- the SAME library the
automatic reaper (scripts/reap-guacamole-sessions.py, issue #181) uses, so
manual and automatic teardown can never drift apart. Guacamole/guacd have
no automatic idle-session detection or container lifecycle management
(see docker-compose.guacamole.yml's own header comment); until the reaper
is deployed on a schedule, run this explicitly after every session.

Usage:
  python3 scripts/teardown-guacamole-session.py --student-id STU_12345 --session-id 1234567890

Environment (unchanged from the original script):
  GUACAMOLE_URL / GUACAMOLE_ADMIN_USERNAME / GUACAMOLE_ADMIN_PASSWORD
  GRADING_API_URL + GRADING_COORDINATOR_KEY -- optional now: with them the
  token is revoked too, without them the run reports that step and the
  token expires on its own TTL (revoke is also #182's concern).

Exit status: 0 only when every step succeeded; any failed step prints its
reason and yields 1 (partial state is loud -- the reaper would retry it).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import guacamole_session as gs_mod
from guacamole_session import NamespaceViolation, teardown_session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--student-id", required=True)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args()

    container_name = f"guac-weasis-{args.student_id}-{args.session_id}"
    guac_url = os.environ.get("GUACAMOLE_URL", "http://localhost:8090/guacamole/")
    admin_user = os.environ.get("GUACAMOLE_ADMIN_USERNAME", "guacadmin")
    admin_pass = os.environ.get("GUACAMOLE_ADMIN_PASSWORD", "guacadmin")
    grading_api_url = os.environ.get("GRADING_API_URL", "")
    coordinator_key = os.environ.get("GRADING_COORDINATOR_KEY", "")
    link_dir = os.environ.get("GUACAMOLE_LINK_DIR") or gs_mod.DEFAULT_LINK_DIR

    try:
        print(f"Tearing down {container_name} ...")
        report = teardown_session(
            container_name,
            guac_url=guac_url,
            admin_user=admin_user,
            admin_pass=admin_pass,
            grading_api_url=grading_api_url or None,
            coordinator_key=coordinator_key or None,
            link_dir=link_dir,
            verbose=True,
        )
    except NamespaceViolation as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    failed = {k: v for k, v in report.items() if not gs_mod.step_ok(v)}
    skipped = {k: v for k, v in report.items() if str(v).startswith("skipped")}
    for step, why in skipped.items():
        print(f"NOTE step '{step}': {why}")
    if failed:
        for step, err in failed.items():
            print(f"FAILED step '{step}': {err}", file=sys.stderr)
        return 1
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
