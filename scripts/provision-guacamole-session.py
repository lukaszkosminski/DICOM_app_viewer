#!/usr/bin/env python3
"""
Mints a single-use, per-student Guacamole session URL for the DICOM
viewer -- the Guacamole flow's equivalent of scripts/create-session.py's
Kasm flow. See docker-compose.guacamole.yml's own header comment for why
this exists (Kasm Workspaces Community Edition's 5-concurrent-session cap
and non-commercial-use restriction, see CLAUDE.md) and
docker/guacamole-weasis/'s own comments for what's explicitly out of
scope right now (no anti-cheat/hardening -- basic functionality only).

Unlike Kasm's request_kasm API, Guacamole has no built-in per-session
container provisioning and no anonymous quick-link mechanism -- confirmed
via research before building this, not assumed (Guacamole's own
"quickconnect" extension explicitly requires prior authentication; a
community "noauth" fork exists but isn't maintained by Apache and isn't
version-compatibility-guaranteed). This script does both jobs itself:
  1. Mints a grading-api session token (same POST /session mechanism
     create-session.py uses -- identical authorization model regardless
     of which remote-display technology delivers the pixels).
  2. Launches a fresh docker/guacamole-weasis container via `docker run`
     (not docker-compose -- this is a per-student, ephemeral container,
     not a static service, same spirit as Kasm's own per-session
     containers).
  3. Creates a real Guacamole user + VNC connection pointed at that
     container, via Guacamole's REST API (guacamole-auth-jdbc) -- the
     officially-supported way to get a single, no-further-login-prompt
     link per student is a real per-student username/password pair with
     Guacamole's documented URL-parameter auto-login
     (#/?username=...&password=...).
  4. Writes the ready-to-open link to a 0600 file (NEVER prints it; the
     stdout JSON carries the link_file path instead -- CodeQL fix owned
     by #191).

Requires:
  GRADING_COORDINATOR_KEY  -- same secret create-session.py uses
  GUACAMOLE_URL            -- default http://localhost:8090/guacamole/
  GUACAMOLE_ADMIN_USERNAME -- default guacadmin (Guacamole's own default
                              admin account -- change this for anything
                              beyond a local PoC)
  GUACAMOLE_ADMIN_PASSWORD -- default guacadmin
  GRADING_API_URL          -- default http://localhost:8080/
  DOCKER_NETWORK           -- default dicom_app_viewer_ipcmc-internal
                              (the compose-generated network name;
                              override if your project name differs --
                              check with `docker network ls`)
  GUACAMOLE_WEASIS_IMAGE   -- default ipcmc/guacamole-weasis:poc
  GUACAMOLE_USER_VALID_HOURS -- default 48; day-grained account backstop
  GUACAMOLE_LINK_DIR       -- default local/session-links (or --link-dir)

Usage:
  GRADING_COORDINATOR_KEY=... python3 scripts/provision-guacamole-session.py --student-id STU_12345

SECURITY: the auto-login link is NEVER printed. It is written to a file
created with O_CREAT|O_EXCL and mode 0600 (refuses to overwrite anything)
inside --link-dir / $GUACAMOLE_LINK_DIR (default local/session-links,
created 0700, git-ignored via local/*). Operator flow: `cat` the printed
link_file path, hand the ONE line to exactly one student, then delete the
file -- teardown-guacamole-session.py and the reaper delete it for you
when the session ends. A leaked link file is still single-session scoped
(rollback/teardown/reaper end the session; valid-until ends the account),
which is the difference from the long-lived key of the 2022-09-22
incident recorded in create-session.py. Guidance ships in
docs/GUACAMOLE.md (#183).

Companion teardown script: scripts/teardown-guacamole-session.py
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# issue #182: rollback shares ONE naming/shape truth with the reaper and
# teardown (merged in #187) -- a half-provisioned session must be undoable
# by exactly the code that reaps fully-provisioned ones.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guacamole_session as gs

# issue #52: a narrow custom seccomp profile, not blanket
# `seccomp=unconfined` -- see docker/guacamole-weasis/seccomp/
# build-profile.py's own docstring for how this was derived and verified.
_SECCOMP_PROFILE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "docker",
    "guacamole-weasis",
    "seccomp",
    "profile.json",
)


class ProvisionAborted(Exception):
    """A provisioning step failed. main() responds by running every undo
    registered so far, in reverse order (issue #182, the compensation
    pattern create-session.py uses for the Kasm flow)."""


# Steps register their undo ONLY after their step succeeded. One-shot CLI,
# so a process-level list is safe; main() owns its lifecycle (clear on
# start, drain on abort, clear without running after success -- a live
# session must never be undone).
_UNDO = []


# User account expiry (#191 owner directive), VERIFIED against the
# guacamole-client tag 1.6.0 sources (p2 cross-checked the same files):
#   extensions/guacamole-auth-jdbc/modules/guacamole-auth-jdbc-base/
#     src/main/java/org/apache/guacamole/auth/jdbc/user/ModeledUser.java
#     lines 71-101 (attribute names), 122-123 (they are DateFields)
#   guacamole-ext/src/main/java/org/apache/guacamole/form/DateField.java:36
#     (DateField.FORMAT)
# Facts: the JDBC directory's attribute names are "valid-from"/"valid-until"
# -- NO "guac-" prefix (the old guac-* names belonged to the pre-1.x core
# GuacUser surface) -- the value format is DATE ONLY ("yyyy-MM-dd",
# day-grained), and per ModeledUser/UserModel javadoc the account stays
# usable until MIDNIGHT OF THE DAY AFTER valid-until. Consequences owned in
# the docs: this is a day-level BACKSTOP, not an hours-precise lifetime
# (valid-until is the DATE of the now+lifetime moment and the account
# lives until 23:59:59.999 of that day, so account-side expiry can trail
# the request by up to ~24 h); the reaper (#181) remains the hard limit. "timezone" is set
# to UTC so Guacamole's day-boundary comparisons are deterministic
# server-side regardless of DB/host TZ. "valid-from" is deliberately NOT
# set: accounts should be usable immediately.
DEFAULT_USER_VALID_HOURS = 48.0  # matches the reaper's DEFAULT_MAX_AGE_HOURS
MAX_USER_VALID_HOURS = 24 * 365  # a 1-year account is already absurd here


def parse_valid_until(lifetime_hours, now=None):
    """-> (valid_until_date_str, ends_moment_iso).

    valid-until is the DATE of the expiry moment (now + lifetime); the
    account stays usable until 23:59:59.999 of that day in the user's
    timezone (pinned UTC here), per ModeledUser -- so the account-side
    expiry can trail the requested lifetime by up to ~24 h, and stdout
    must show the ACCOUNT truth, not the requested moment.

    Raises ValueError on anything a sane operator did not mean:
    non-numeric, NaN, non-positive, above MAX_USER_VALID_HOURS; raises
    OverflowError (caught by the caller) for magnitudes timedelta cannot
    represent, e.g. float('inf')."""
    hours = float(lifetime_hours)  # 'inf' -> float ok; timedelta below overflows
    if not (hours > 0):  # also catches NaN (NaN > 0 is False)
        raise ValueError(f"lifetime must be > 0 hours, got {lifetime_hours!r}")
    if hours > MAX_USER_VALID_HOURS:
        raise ValueError(
            f"lifetime {hours!r} h exceeds the {MAX_USER_VALID_HOURS} h cap"
            " (longer-lived access belongs in provisioning, not a user row)"
        )
    moment = (now or datetime.now(timezone.utc)) + timedelta(hours=hours)
    return moment.date().isoformat(), moment.isoformat(timespec="seconds")


def _rollback(reason):
    print(f"provisioning failed: {reason}", file=sys.stderr)
    print(
        f"rolling back {len(_UNDO)} completed step(s), most recent first:",
        file=sys.stderr,
    )
    failed = []
    while _UNDO:
        label, undo = _UNDO.pop()
        try:
            undo()
            print(f"  undone: {label}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - one bad undo must not skip the rest
            failed.append(label)
            print(
                f"  COULD NOT UNDO {label}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
    if failed:
        print(
            "leftovers needing attention: "
            + ", ".join(failed)
            + " -- scripts/teardown-guacamole-session.py <container-name> is "
            "idempotent and sibling-aware; grading tokens not revoked expire "
            "on their TTL.",
            file=sys.stderr,
        )


def _undo_token(grading_api_url, coordinator_key, student_id, container_name):
    """Revoke is PER-STUDENT (grading-api's one-live-token rule), so an
    unrelated live session of the same student must not lose ITS token --
    same sibling rule #187's teardown obeys. Unknown siblings counts as
    'present': the un-revoked leftover token dies on its TTL, which is
    strictly safer than killing a live session's grading."""
    try:
        siblings = gs.sibling_containers(gs.real_docker, student_id, container_name)
    except Exception as exc:  # noqa: BLE001 - fail closed, see docstring
        print(
            f"  sibling check failed ({exc}); treating siblings as present",
            file=sys.stderr,
        )
        siblings = ["<unknown>"]
    if siblings:
        print(
            f"  skipped token revoke: student has other sessions ({', '.join(siblings)}); "
            "the orphan token expires on its TTL instead",
            file=sys.stderr,
        )
        return
    gs.revoke_token_for_student(
        gs.real_opener, grading_api_url, coordinator_key, student_id
    )


def api_call(method, url, data=None, headers=None, fatal=True, label="API"):
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # Credential-hygiene rule: EVERY guacamole URL carries
        # ?token=<admin token>, and Guacamole echoes request objects back in
        # error bodies -- connection attributes carry the VNC password. Any
        # 4xx/5xx would otherwise write a live admin token and echoed
        # password to stderr/cron/CI logs (the #182 rollback re-reports these
        # messages verbatim). Same discipline as guacamole_session.http_json:
        # scrub the token param and truncate the echoed body at the ONE
        # site that builds API error text.
        safe_url = re.sub(r"([?&])token=[^&]*", r"\1token=<redacted>", url)
        # Truncation alone is NOT sanitization: a short echoed object fits
        # entirely inside 200 chars. Scrub password fields and any token
        # params inside the body itself before excerpting.
        body = e.read().decode(errors="replace")
        body = re.sub(r'("password"\s*:\s*)"[^"]*"', r'\1"<redacted>"', body)
        body = re.sub(r"token=[^&\s\"']+", "token=<redacted>", body)
        message = f"{label} error calling {safe_url}: {e.code} {body[:200]}"
        if fatal:
            raise ProvisionAborted(message)
        print(f"  (non-fatal) {message}", file=sys.stderr)
        return None


def guac_login(base_url, username, password):
    resp = api_call(
        "POST",
        f"{base_url}api/tokens",
        data=urllib.parse.urlencode(
            {"username": username, "password": password}
        ).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        label="Guacamole login",
    )
    return resp["authToken"]


def _provision():
    parser = argparse.ArgumentParser()
    parser.add_argument("--student-id", required=True)
    parser.add_argument("--session-id", default=None, help="defaults to a timestamp")
    parser.add_argument(
        "--link-dir",
        default=os.environ.get("GUACAMOLE_LINK_DIR") or gs.DEFAULT_LINK_DIR,
        help="directory for 0600 session-link files (created 0700 when missing); "
        "default $GUACAMOLE_LINK_DIR or local/session-links. Operator: cat the "
        "printed link_file, hand it to ONE student, delete afterwards.",
    )
    args = parser.parse_args()

    guac_url = os.environ.get("GUACAMOLE_URL", "http://localhost:8090/guacamole/")
    if not guac_url.endswith("/"):
        guac_url += "/"
    admin_user = os.environ.get("GUACAMOLE_ADMIN_USERNAME", "guacadmin")
    admin_pass = os.environ.get("GUACAMOLE_ADMIN_PASSWORD", "guacadmin")
    coordinator_key = os.environ["GRADING_COORDINATOR_KEY"]
    grading_api_url = os.environ.get("GRADING_API_URL", "http://localhost:8080/")
    docker_network = os.environ.get("DOCKER_NETWORK", "dicom_app_viewer_ipcmc-internal")
    image = os.environ.get("GUACAMOLE_WEASIS_IMAGE", "ipcmc/guacamole-weasis:poc")

    student_id = args.student_id
    session_id = args.session_id or str(int(time.time()))

    # Validated FIRST: an unusable lifetime must abort before the token
    # mint, so there is nothing to roll back (and the CLI exits 1 with a
    # clear message via main()'s ProvisionAborted path).
    raw_valid_hours = os.environ.get("GUACAMOLE_USER_VALID_HOURS", "") or str(
        DEFAULT_USER_VALID_HOURS
    )
    try:
        valid_until_date, _requested_ends = parse_valid_until(raw_valid_hours)
    except (ValueError, OverflowError) as exc:
        raise ProvisionAborted(
            f"GUACAMOLE_USER_VALID_HOURS: {exc} (Guacamole account expiry is"
            " day-grained; the reaper is the hour-grained limit)"
        ) from None

    # 1. grading-api token -- identical mechanism to create-session.py.
    session_created = api_call(
        "POST",
        f"{grading_api_url.rstrip('/')}/api/session",
        data={"student_id": student_id, "session_id": session_id},
        headers={
            "Content-Type": "application/json",
            "X-Coordinator-Key": coordinator_key,
        },
        label="grading-api",
    )
    grading_token = session_created["token"]
    # container_name is defined a few lines below; the closure reads it at
    # UNDO time, by which point it is always bound.
    _UNDO.append(
        (
            "grading token (revoked only if this student has no other live session)",
            lambda: _undo_token(
                grading_api_url, coordinator_key, student_id, container_name
            ),
        )
    )

    # 2. Fresh per-student container. Its name doubles as the VNC
    # "hostname" Guacamole connects to -- Docker's embedded DNS resolves
    # container names on a shared network, no need to inspect an IP.
    #
    # issue #53: real VNC auth, not -SecurityTypes None. token_hex(4) (8
    # hex chars) deliberately, not token_urlsafe like guac_password below
    # -- classic VNC/RFB auth only ever uses the first 8 bytes of the
    # password, a protocol limitation, not a mistake to "fix" by
    # generating something longer. Handed to the container via
    # VNC_PASSWORD (docker-entrypoint.sh writes it into TigerVNC's own
    # password file) and to Guacamole's connection config below, as the
    # same secret both ends of that VNC handshake need to agree on.
    vnc_password = secrets.token_hex(4)
    container_name = f"guac-weasis-{student_id}-{session_id}"
    subprocess.run(
        ["docker", "rm", "-f", container_name], capture_output=True, check=False
    )
    # Undo registered BEFORE the run: a failing `docker run -d` can still
    # have created the container (image pull ok, start failed), and even a
    # half-created one holds the name that would poison every later retry.
    _UNDO.append(
        (
            f"container {container_name}",
            lambda: gs.docker_rm(gs.real_docker, container_name),
        )
    )
    run_result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container_name,
            "--network",
            docker_network,
            # No -p/--publish -- this container's VNC port must only ever
            # be reachable from guacd over docker_network, never a
            # published host port (issue #53; see
            # docker/guacamole-weasis/docker-entrypoint.sh's own comment).
            # See scripts/tests/test_provision_guacamole_session.py for
            # the regression test guarding this.
            #
            # See docker/guacamole-weasis/launch-session.sh's own comment:
            # the grading panel's browser needs unprivileged user
            # namespaces for its own internal sandboxing (bubblewrap) --
            # confirmed the hard way this container's default seccomp
            # profile blocks that outright.
            #
            # issue #52: this used to be blanket `seccomp=unconfined`,
            # exposing the full host kernel syscall surface. Replaced with
            # a narrow custom profile (Docker's own default profile plus
            # only the namespace/mount syscalls bwrap actually needs) --
            # see docker/guacamole-weasis/seccomp/build-profile.py's own
            # docstring for exactly how this was derived and verified
            # (built the real image, confirmed epiphany's bwrap-wrapped
            # processes start under this profile exactly as they do under
            # unconfined, and separately confirmed bpf/keyctl/io_uring/
            # userfaultfd/perf_event_open all still return EPERM under it).
            "--security-opt",
            f"seccomp={_SECCOMP_PROFILE_PATH}",
            "-e",
            f"STUDENT_ID={student_id}",
            "-e",
            f"SESSION_ID={session_id}",
            "-e",
            f"GRADING_TOKEN={grading_token}",
            "-e",
            f"VNC_PASSWORD={vnc_password}",
            image,
        ],
        capture_output=True,
        text=True,
    )
    if run_result.returncode != 0:
        raise ProvisionAborted(f"docker run failed: {run_result.stderr}")

    # Best-effort, not a hard readiness gate (matches create-session.py's
    # own non-fatal-readiness-check philosophy for the Kasm flow) -- gives
    # TigerVNC a moment to actually start listening before Guacamole's
    # first connection attempt.
    time.sleep(3)

    # 3. Guacamole user + connection, via the REST API. See this file's
    # own module docstring for why a real per-student user, not an
    # anonymous quick-link (FOSS Guacamole has no supported equivalent).
    admin_token = guac_login(guac_url, admin_user, admin_pass)

    guac_username = f"stu_{student_id}"
    guac_password = secrets.token_urlsafe(16)

    # Pre-cleanup: a re-run for the same student_id (e.g. minting a fresh
    # link after their previous one expired) would otherwise 400 on a
    # duplicate username. Best-effort, not fatal if there's nothing to
    # clean up yet.
    api_call(
        "DELETE",
        f"{guac_url}api/session/data/postgresql/users/{guac_username}?token={admin_token}",
        fatal=False,
        label="Guacamole (pre-cleanup)",
    )
    api_call(
        "POST",
        f"{guac_url}api/session/data/postgresql/users?token={admin_token}",
        data={
            "username": guac_username,
            "password": guac_password,
            "attributes": {"valid-until": valid_until_date, "timezone": "UTC"},
        },
        headers={"Content-Type": "application/json"},
        label="Guacamole (create user)",
    )
    # Deleting the user cascades its permission grants, so this one undo
    # also covers a later failed permission PATCH.
    _UNDO.append(
        (
            f"guacamole user {guac_username}",
            lambda: gs.guac_delete_user(
                gs.real_opener, guac_url, admin_token, guac_username
            ),
        )
    )
    connection = api_call(
        "POST",
        f"{guac_url}api/session/data/postgresql/connections?token={admin_token}",
        data={
            "parentIdentifier": "ROOT",
            "name": container_name,
            "protocol": "vnc",
            # "password" here is the VNC/RFB auth secret (issue #53), a
            # completely separate credential from guac_password above
            # (that one's Guacamole's own web-login password for this
            # student's account) -- same vnc_password the container's own
            # VNC_PASSWORD env var was given, above.
            "parameters": {
                "hostname": container_name,
                "port": "5901",
                "password": vnc_password,
            },
            "attributes": {},
        },
        headers={"Content-Type": "application/json"},
        label="Guacamole (create connection)",
    )
    # By-NAME delete (library): correct even when the response body is
    # malformed and no identifier was ever parsed out of it.
    _UNDO.append(
        (
            f"guacamole connection {container_name}",
            lambda: gs.guac_delete_connection_by_name(
                gs.real_opener, guac_url, admin_token, container_name
            ),
        )
    )
    try:
        connection_id = connection["identifier"]
    except (TypeError, KeyError) as exc:
        raise ProvisionAborted("create-connection returned no identifier") from exc

    api_call(
        "PATCH",
        f"{guac_url}api/session/data/postgresql/users/{guac_username}/permissions?token={admin_token}",
        data=[
            {
                "op": "add",
                "path": f"/connectionPermissions/{connection_id}",
                "value": "READ",
            }
        ],
        headers={"Content-Type": "application/json"},
        label="Guacamole (grant permission)",
    )

    link = f"{guac_url}#/?username={urllib.parse.quote(guac_username)}&password={urllib.parse.quote(guac_password)}"

    # #191 CodeQL fix, owner directive: the credential travels ONLY as a
    # 0600 file (O_EXCL: never overwrite; owner-fixed modes; the link dir
    # lives under git-ignored local/*). A failed write aborts and rolls
    # back the WHOLE provision -- there is deliberately no fallback that
    # prints the link, because stdout goes to shell history, CI logs and
    # chat paste-aways.
    link_dir = args.link_dir
    link_path = os.path.join(link_dir, gs.link_file_name(container_name))
    # Directory policy (p2 review): chmod ONLY what this run created. A
    # pre-existing --link-dir must already be private; silently tightening
    # (or being silently opened up by) somebody else's directory -- /tmp,
    # a home dir -- is the worse bug. Pre-existing dirs must be mode 07xx
    # AND owned by us, or we refuse and tell the operator the exact fix.
    try:
        os.makedirs(link_dir, mode=0o700)
        os.chmod(link_dir, 0o700)  # ours; mkdir's mode is ANDed with umask
    except FileExistsError:
        if not os.path.isdir(link_dir):
            raise ProvisionAborted(
                f"--link-dir {link_dir} exists and is not a directory"
            )
        st = os.stat(link_dir)
        if (st.st_mode & 0o077) or st.st_uid != os.getuid():
            raise ProvisionAborted(
                f"--link-dir {link_dir} is not private (mode "
                f"{oct(st.st_mode & 0o777)}, uid {st.st_uid}, expected mode "
                "07xx and your uid); run chmod 700 on it yourself -- this "
                "tool never chmods a directory it did not create"
            )
    try:
        fd = os.open(link_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        # O_EXCL also ENOTDIR/EEXIST-honest: a pre-existing file is NEVER
        # clobbered, and no undo is registered because we created nothing.
        raise ProvisionAborted(
            f"could not write session link file {link_path}: {exc}"
        ) from None
    # Undo registered only AFTER os.open succeeded: it may delete nothing
    # but the file THIS run created (a rollback must not eat an unrelated
    # pre-existing file -- caught by the overwrite test).
    _UNDO.append(
        (f"link file {link_path}", lambda: gs.remove_link_file(link_dir, container_name))
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(link + "\n")
    except OSError as exc:
        raise ProvisionAborted(
            f"could not finish writing session link file {link_path}: {exc}"
        ) from None

    # stdout is NON-SECRET BY DESIGN (CodeQL alert fixed in code, not
    # dismissed): everything needed to FETCH the credential, minus the
    # credential itself. Never add the link back into this payload.
    print(
        json.dumps(
            {
                "student_id": student_id,
                "session_id": session_id,
                "container_name": container_name,
                "guacamole_username": guac_username,
                "connection_id": connection_id,
                # ACCOUNT truth (what Guacamole actually enforces), not the
                # requested-lifetime moment -- see parse_valid_until.
                "account_valid_until": valid_until_date,
                "account_expires_at": f"{valid_until_date}T23:59:59Z",
                "link_file": link_path,
            },
            indent=2,
        )
    )


def main():
    _UNDO.clear()
    try:
        _provision()
    except ProvisionAborted as exc:
        _rollback(str(exc))
        _UNDO.clear()  # a failed run must never leave live undos for anything else to trip on
        sys.exit(1)
    _UNDO.clear()  # success: the session is LIVE -- its registered undos are dropped, NOT run


if __name__ == "__main__":
    main()
