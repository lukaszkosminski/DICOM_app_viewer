#!/usr/bin/env python3
"""Idle/expiry reaper for Guacamole PoC sessions (issue #181, part of #177).

Guacamole has no destroy-on-logout: per-student containers and Guacamole
users accumulate unless something removes them (docker-compose.guacamole.yml
header). CLAUDE.md makes ephemerality a hard rule, so this script is that
something -- meant to be run on a schedule (cron/systemd timer); one pass
per invocation, all state it needs between passes lives in one small JSON
file.

What a "session" is and how it dies comes from scripts/guacamole_session.py
-- the SAME library teardown-guacamole-session.py uses (no duplicated
logic, #181 acceptance criteria). The reaper never invents its own deletion
paths: container + guacamole user + connection + grading-token revocation,
namespace-guarded to `guac-weasis-*` / `stu_*` names only.

Policy:
  * a session whose CONTAINER already exited is reaped immediately (the
    student can no longer be looking at it);
  * a RUNNING session is reaped when it is older than --max-age-hours;
  * a RUNNING session is reaped when it has had no OPEN Guacamole
    connection for --idle-minutes. "No open connection" is measured per
    run and accumulated in the state file (first inactive pass starts the
    clock). If the active-connections endpoint is unreachable, idle reaping
    is SKIPPED for this run entirely -- a missing signal is never a reason
    to kill an exam (max-age still applies).
  * Guacamole USERS with no container at all (provisioning died halfway,
    #182 territory) lose their user + guac-weasis-<student>-* connections
    and have their grading token revoked; nothing else is touched.

Defaults are deliberately longer than an exam slot (see docs/GUACAMOLE.md,
issue #183): idle 180 min, max age 48 h. The active-connection signal only
protects a CONNECTED student; tuning these two numbers to the 24-48 h
exam windows (#122) stays the owner's deployment decision.

Environment: GUACAMOLE_URL, GUACAMOLE_ADMIN_USERNAME, GUACAMOLE_ADMIN_PASSWORD,
GRADING_API_URL, GRADING_COORDINATOR_KEY (revocation without the key is
impossible -- every teardown then REPORTS the token as "left to expire",
which must be visible in the run summary, never silent).

Exit status: 0 = pass clean; 1 = at least one step failed (retried next
pass); 2 = reaper could not run at all (login/parse errors).
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import guacamole_session as gs  # sibling-module import, path fixed above

DEFAULT_IDLE_MINUTES = 180
DEFAULT_MAX_AGE_HOURS = 48


def _load_state(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, sort_keys=True, indent=1)
    os.rename(tmp, path)  # atomic on POSIX: the cron job can never read half a file


class Reaper:
    """All external effects enter through the constructor, so the pytest
    suite drives the ENTIRE policy against fakes: `docker` (argv callable),
    `opener` (urlopen stand-in), `now` (unix clock)."""

    def __init__(
        self,
        docker,
        opener,
        now,
        *,
        guac_url,
        admin_user,
        admin_pass,
        grading_api_url="",
        coordinator_key="",
        idle_minutes=DEFAULT_IDLE_MINUTES,
        max_age_hours=DEFAULT_MAX_AGE_HOURS,
        state=None,
        link_dir="",
        log=print,
    ):
        self.docker = docker
        self.opener = opener
        self.now = now
        self.guac_url = guac_url if guac_url.endswith("/") else guac_url + "/"
        self.admin_user = admin_user
        self.admin_pass = admin_pass
        self.link_dir = link_dir or gs.DEFAULT_LINK_DIR
        self.grading_api_url = grading_api_url
        # credential files must not outlive reaped sessions (#191 CodeQL
        # fix); empty falls back to the shared library default
        self.coordinator_key = coordinator_key
        self.idle_seconds = float(idle_minutes) * 60.0
        self.max_age_seconds = float(max_age_hours) * 3600.0
        self.state = state if state is not None else {}
        self.log = log
        self.failures = []  # (target, {step: error}) retried next pass
        self.reaped = []  # container names torn down this pass
        self.orphaned = []  # guacamole users cleaned without a container
        self.token = None

    # -- one pass -----------------------------------------------------------

    def run(self, dry_run=False):
        self.token = gs.guac_login(
            self.opener, self.guac_url, self.admin_user, self.admin_pass
        )
        containers = gs.docker_list_sessions(self.docker)
        active = self._active_usernames()
        self._sweep_containers(containers, active, dry_run)
        self._sweep_orphan_users(containers, active, dry_run)
        return {
            "containers_seen": len(containers),
            "reaped": len(self.reaped),
            "orphaned_users": len(self.orphaned),
            "failures": len(self.failures),
        }

    def _active_usernames(self):
        """Usernames with an open connection; None means 'signal missing'.
        Missing signal => idle reaping is disabled for this pass (never
        kill on an assumption)."""
        try:
            return gs.guac_active_usernames(self.opener, self.guac_url, self.token)
        except Exception:  # noqa: BLE001 - missing signal degrades the pass, never kills the cron run
            self.log(
                "WARNING: active-connections endpoint unavailable; idle reaping skipped this pass"
            )
            return None

    def _sweep_containers(self, containers, active, dry_run):
        # State hygiene: an entry whose container is gone for good (a failed
        # teardown had already rm'd it, or it was removed elsewhere) must not
        # linger forever -- the orphan-user pass and next passes cover it.
        seen = {c["name"] for c in containers}
        for stale in [n for n in self.state if n not in seen]:
            self.state.pop(stale)
        reap = []
        for c in containers:
            try:
                student, _ = gs.parse_container_name(c["name"])
            except gs.NamespaceViolation as exc:
                self.log(
                    f"REFUSED: {exc}"
                )  # docker_list_sessions already filtered; belt AND braces
                continue
            username = gs.GUAC_USER_PREFIX + student
            if c["state"] != "running":
                reap.append((c["name"], "container already exited"))
                continue
            entry = self.state.setdefault(c["name"], {})
            started = gs.docker_started_at(self.docker, c["name"])
            if started is None:
                started = entry.get("started_ts") or self.now()
                entry["started_ts"] = started
                self.log(
                    f"WARNING: {c['name']}: unreadable start time, using first-seen time for max-age"
                )
            else:
                entry["started_ts"] = started
            if active is not None:
                if username in active:
                    entry["inactive_since"] = None
                elif entry.get("inactive_since") is None:
                    entry["inactive_since"] = (
                        self.now()
                    )  # start the idle clock NOW, on first sighting
            if self.now() - entry["started_ts"] > self.max_age_seconds:
                reap.append((c["name"], "older than max-age"))
                continue
            inactive_since = entry.get("inactive_since")
            if (
                active is not None
                and inactive_since
                and self.now() - inactive_since > self.idle_seconds
            ):
                reap.append(
                    (
                        c["name"],
                        f"no open connection for over {int(self.idle_seconds // 60)} min",
                    )
                )
        for name, reason in reap:
            self._reap(name, reason, dry_run)

    def _reap(self, container_name, reason, dry_run):
        if dry_run:
            self.log(f"dry-run: would reap {container_name} ({reason})")
            return
        # No coordinator key is an operator configuration choice, not a
        # step failure that would keep the entry retrying forever: skip the
        # revocation call and say so LOUDLY every pass (the token expires on
        # its TTL). Mirrors the orphan pass' NOTE below.
        effective_api = (
            self.grading_api_url
            if (self.grading_api_url and self.coordinator_key)
            else None
        )
        if self.grading_api_url and not self.coordinator_key:
            self.log(
                f"NOTE: cannot revoke token for {container_name}: no coordinator key; it expires on its TTL"
            )
        report = gs.teardown_session(
            container_name,
            opener=self.opener,
            docker=self.docker,
            guac_url=self.guac_url,
            admin_user=self.admin_user,
            admin_pass=self.admin_pass,
            grading_api_url=effective_api,
            coordinator_key=self.coordinator_key or None,
            link_dir=self.link_dir,
        )
        bad = {step: err for step, err in report.items() if not gs.step_ok(err)}
        skipped = {
            step: err for step, err in report.items() if str(err).startswith("skipped")
        }
        if skipped:
            self.log(f"note for {container_name}: {skipped}")
        if bad:
            # Keep the state entry: next pass sees the container again and
            # retries whatever is still there (every library step is
            # idempotent-by-design: rm/user-delete tolerate 'not there').
            self.failures.append((container_name, bad))
            self.log(f"FAILED reap {container_name}: {bad} (will retry next pass)")
        else:
            self.state.pop(container_name, None)
            self.reaped.append(container_name)
            self.log(f"reaped {container_name} ({reason})")

    def _sweep_orphan_users(self, containers, active, dry_run):
        """Guacamole users we created that have NO container: the
        provisioning died halfway (#182 will make that rarer, the reaper
        makes it survivable). A live student always has their container."""
        students_with_container = set()
        for c in containers:
            try:
                student, _ = gs.parse_container_name(c["name"])
            except gs.NamespaceViolation:
                continue
            students_with_container.add(student)
        for username in gs.guac_list_users(self.opener, self.guac_url, self.token):
            try:
                student = gs.parse_guac_username(username)
            except gs.NamespaceViolation:
                continue  # guacadmin and anything else: outside our namespace, invisible to us
            if student in students_with_container:
                continue
            if active is not None and username in active:
                self.log(
                    f"WARNING: {username} is connected but has no container; leaving it alone, a human must look"
                )
                continue
            if dry_run:
                self.log(
                    f"dry-run: would clean orphan guacamole user {username} + its connections"
                )
                continue
            errors = []
            try:
                gs.guac_delete_user(self.opener, self.guac_url, self.token, username)
            except Exception as exc:  # noqa: BLE001 - one bad object must not stop the whole pass
                errors.append(f"user: {exc}")
            try:
                prefix = f"{gs.CONTAINER_PREFIX}{student}-"
                for conn_id, conn in gs.guac_list_connections(
                    self.opener, self.guac_url, self.token
                ).items():
                    name = conn.get("name") or ""
                    if name.startswith(
                        prefix
                    ):  # the prefix itself starts with guac-weasis-: guarded
                        gs.http_json(
                            self.opener,
                            "DELETE",
                            f"{self.guac_url}api/session/data/postgresql/connections/{conn_id}?token={self.token}",
                        )
            except Exception as exc:  # noqa: BLE001 - one bad object must not stop the whole pass
                errors.append(f"connections: {exc}")
            if self.grading_api_url:
                if not self.coordinator_key:
                    self.log(
                        f"NOTE: cannot revoke token for {student}: no coordinator key configured; it expires on its TTL"
                    )
                else:
                    try:
                        gs.revoke_token_for_student(
                            self.opener,
                            self.grading_api_url,
                            self.coordinator_key,
                            student,
                        )
                    except Exception as exc:  # noqa: BLE001 - one bad object must not stop the pass
                        errors.append(f"revoke: {exc}")
            if errors:
                self.failures.append(
                    (username, {"orphan": "; ".join(str(e)[:160] for e in errors)})
                )
                self.log(f"FAILED orphan clean {username} (will retry next pass)")
            else:
                self.orphaned.append(username)
                self.log(f"cleaned orphan guacamole user {username}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        # --help must be the operator's whole manual: the module docstring
        # carries the policy, the environment table and the defaults.
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--idle-minutes", type=float, default=DEFAULT_IDLE_MINUTES)
    ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--state-file", default="/tmp/guacamole-reaper-state.json")
    ap.add_argument(
        "--link-dir",
        default=os.environ.get("GUACAMOLE_LINK_DIR") or gs.DEFAULT_LINK_DIR,
        help="delete reaped sessions' 0600 link files here (default "
        "$GUACAMOLE_LINK_DIR or local/session-links)",
    )
    args = ap.parse_args(argv)
    guac_url = os.environ.get("GUACAMOLE_URL", "http://localhost:8090/guacamole/")
    admin_user = os.environ.get("GUACAMOLE_ADMIN_USERNAME", "guacadmin")
    admin_pass = os.environ.get("GUACAMOLE_ADMIN_PASSWORD", "guacadmin")
    grading_api_url = os.environ.get("GRADING_API_URL", "")
    coordinator_key = os.environ.get("GRADING_COORDINATOR_KEY", "")
    state = _load_state(args.state_file)
    reaper = Reaper(
        gs.real_docker,
        gs.real_opener,
        time.time,
        guac_url=guac_url,
        admin_user=admin_user,
        admin_pass=admin_pass,
        grading_api_url=grading_api_url,
        coordinator_key=coordinator_key,
        idle_minutes=args.idle_minutes,
        max_age_hours=args.max_age_hours,
        state=state,
        link_dir=args.link_dir,
    )
    try:
        summary = reaper.run(dry_run=args.dry_run)
    except gs.APIError as exc:
        print(f"ERROR: guacamole/grading-api HTTP failure: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: docker/OS failure: {exc.__class__.__name__}", file=sys.stderr)
        return 2
    if not args.dry_run:
        _save_state(args.state_file, reaper.state)
    parts = " ".join(f"{k}={v}" for k, v in sorted(summary.items()))
    print("reap pass: " + parts)
    return 1 if reaper.failures else 0


if __name__ == "__main__":
    sys.exit(main())
