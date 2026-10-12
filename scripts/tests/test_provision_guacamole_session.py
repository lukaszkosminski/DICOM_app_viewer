"""
Unit tests for scripts/provision-guacamole-session.py (issue #53): the two
invariants that make this container's total lack of a published port /
VNC auth actually matter, verified directly rather than only by manual
inspection.

No Docker, no real Guacamole/grading-api needed -- subprocess.run and
urllib.request.urlopen are both mocked, so this runs in well under a
second and never touches real infrastructure (unlike
scripts/test-guacamole-integration.sh, which is the real end-to-end
proof this actually works against a live stack).
"""

import importlib.util
import json
import os
import sys
import urllib.request
from unittest.mock import MagicMock, patch

import pytest

# >= 32 chars: grading-api's app/config.py enforces a minimum
# coordinator-key length for any process importing the app (issue #97);
# kept in sync here so both suites share one convention even though this
# script only forwards the key as an HTTP header.
os.environ.setdefault(
    "GRADING_COORDINATOR_KEY", "test-only-coordinator-key-0123456789abcdef"
)

# provision-guacamole-session.py has a hyphen in its filename, so it can't
# be `import`ed normally -- load it by path instead, same technique the
# script itself would need if it were ever imported rather than run
# directly.
_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "provision-guacamole-session.py"
)
_spec = importlib.util.spec_from_file_location(
    "provision_guacamole_session", _MODULE_PATH
)
provision = importlib.util.module_from_spec(_spec)
sys.modules["provision_guacamole_session"] = provision
_spec.loader.exec_module(provision)


class _FakeResponse:
    """Enough of urllib's response object for api_call()'s own
    read()+json.loads() to work against a canned payload."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(req, timeout=15):
    url = req.full_url
    if url.endswith("/api/session"):
        return _FakeResponse({"token": "fake-grading-token"})
    if url.endswith("/api/tokens"):
        return _FakeResponse({"authToken": "fake-admin-token"})
    if "/connections?token=" in url:
        if req.method == "GET":
            # rollback's by-name delete lists first: empty directory
            return _FakeResponse({})
        return _FakeResponse({"identifier": "42"})
    # users create/delete, permissions grant -- none of these need a
    # meaningful body, main() never reads their return value.
    return _FakeResponse({})


@pytest.fixture(autouse=True)
def isolated_link_dir(tmp_path, monkeypatch):
    """#191: main() now WRITES 0600 link files -- keep every test off the
    real repo local/ dir."""
    monkeypatch.setenv("GUACAMOLE_LINK_DIR", str(tmp_path / "links"))
    return tmp_path / "links"


@pytest.fixture
def mock_docker_run():
    """Captures every subprocess.run() call main() makes (docker rm -f,
    docker run) instead of actually invoking Docker."""
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(args)
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        result.stdout = ""  # issue #182: library helpers parse docker stdout
        return result

    with patch.object(provision.subprocess, "run", side_effect=_fake_run):
        yield calls


def _run_main(mock_docker_run, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["provision-guacamole-session.py", "--student-id", "STU_TEST"]
    )
    with patch.object(urllib.request, "urlopen", side_effect=_fake_urlopen):
        provision.main()
    docker_run_calls = [c for c in mock_docker_run if c[:2] == ["docker", "run"]]
    assert len(docker_run_calls) == 1, "expected exactly one `docker run` invocation"
    return docker_run_calls[0]


def test_docker_run_never_publishes_a_port(mock_docker_run, monkeypatch, capsys):
    """issue #53's own 'at minimum' ask: this container's VNC port must
    never be reachable from outside docker_network -- guard against a
    future -p/--publish regression, not just document the intent."""
    docker_run_args = _run_main(mock_docker_run, monkeypatch)
    capsys.readouterr()
    assert "-p" not in docker_run_args
    assert "--publish" not in docker_run_args


def test_docker_run_passes_a_real_vnc_password(mock_docker_run, monkeypatch, capsys):
    """issue #53's real fix: a per-session VNC_PASSWORD must reach the
    container's own environment, matching docker-entrypoint.sh's
    ${VNC_PASSWORD:?...} requirement."""
    docker_run_args = _run_main(mock_docker_run, monkeypatch)
    capsys.readouterr()
    vnc_password_env = [
        a
        for a in docker_run_args
        if isinstance(a, str) and a.startswith("VNC_PASSWORD=")
    ]
    assert len(vnc_password_env) == 1, (
        "expected exactly one -e VNC_PASSWORD=... argument"
    )
    password = vnc_password_env[0].split("=", 1)[1]
    assert len(password) > 0
    # Classic VNC/RFB auth only ever uses the first 8 bytes -- confirms
    # this is the deliberate token_hex(4) choice, not an accidental
    # longer secret whose extra length would just be silently ignored.
    assert len(password) == 8


def test_docker_run_uses_the_narrow_seccomp_profile_not_unconfined(
    mock_docker_run, monkeypatch, capsys
):
    """issue #52: `--security-opt seccomp=unconfined` exposed the full
    host kernel syscall surface. Guard against a regression back to it,
    and confirm the profile this actually points at is a real, existing
    file (not a typo'd path that would silently fall back to Docker's
    unconfined-if-file-missing... actually Docker just fails `docker run`
    outright on a missing profile path, but a real file is still the
    point: this profile has to be the one build-profile.py generates)."""
    docker_run_args = _run_main(mock_docker_run, monkeypatch)
    capsys.readouterr()

    security_opts = [
        a for a in docker_run_args if isinstance(a, str) and a.startswith("seccomp=")
    ]
    assert len(security_opts) == 1, "expected exactly one seccomp --security-opt"
    assert security_opts[0] != "seccomp=unconfined"

    profile_path = security_opts[0].split("=", 1)[1]
    assert os.path.isfile(profile_path), (
        f"seccomp profile path does not exist: {profile_path}"
    )
    assert os.path.basename(profile_path) == "profile.json"


def test_guacamole_connection_gets_the_same_vnc_password(
    mock_docker_run, monkeypatch, capsys
):
    """The two ends of the VNC handshake must agree: whatever password the
    container's own VNC_PASSWORD env var got must be exactly what
    Guacamole's connection config sends back during authentication."""
    captured_connection_data = {}

    real_api_call = provision.api_call

    def _spying_api_call(method, url, data=None, **kwargs):
        if "/connections?token=" in url and method == "POST":
            captured_connection_data.update(data)
        return real_api_call(method, url, data=data, **kwargs)

    docker_run_args = None
    with patch.object(provision, "api_call", side_effect=_spying_api_call):
        with patch.object(urllib.request, "urlopen", side_effect=_fake_urlopen):
            monkeypatch.setattr(
                sys,
                "argv",
                ["provision-guacamole-session.py", "--student-id", "STU_TEST"],
            )
            provision.main()
    capsys.readouterr()

    docker_run_args = [c for c in mock_docker_run if c[:2] == ["docker", "run"]][0]
    vnc_password_env = next(
        a
        for a in docker_run_args
        if isinstance(a, str) and a.startswith("VNC_PASSWORD=")
    )
    container_password = vnc_password_env.split("=", 1)[1]

    assert captured_connection_data["parameters"]["password"] == container_password


# ---------------------------------------------------------------------------
# issue #182: mid-provision failure must leave NOTHING behind un-unwound
# (the compensation pattern create-session.py uses for the Kasm flow).
# Tests drive main(), so they cover the real __main__ contract:
# SystemExit(1) + reverse-order undos + honest stderr reporting.
# ---------------------------------------------------------------------------

import io as _io  # noqa: E402  (HTTPError body fp)


def _http_error(url):
    return provision.urllib.error.HTTPError(
        url, 500, "boom", {}, _io.BytesIO(b"upstream exploded")
    )


def _failing_urlopen(fail_on, seen):
    def _fake(req, timeout=15):
        seen.append((req.method, req.full_url))
        if fail_on and fail_on in req.full_url:
            raise _http_error(req.full_url)
        return _fake_urlopen(req, timeout=timeout)

    return _fake


def _container_name_from(mock_docker_run):
    run = [c for c in mock_docker_run if c[:2] == ["docker", "run"]][0]
    return run[run.index("--name") + 1]


def test_connection_create_failure_rolls_back_user_container_token(
    mock_docker_run, monkeypatch, capsys
):
    provision._UNDO.clear()
    monkeypatch.setattr(provision.gs, "sibling_containers", lambda *a, **k: [])
    seen = []
    monkeypatch.setattr(
        sys, "argv", ["provision-guacamole-session.py", "--student-id", "STU_RB"]
    )
    with patch.object(
        urllib.request,
        "urlopen",
        side_effect=_failing_urlopen("/connections?token=", seen),
    ):
        with pytest.raises(SystemExit) as stop:
            provision.main()
    assert stop.value.code == 1
    err = capsys.readouterr().err
    name = _container_name_from(mock_docker_run)
    # reverse order: user DELETE (cascade-covers permissions) came before...
    deletes = [u for m, u in seen if m == "DELETE" and "/users/stu_STU_RB" in u]
    assert len(deletes) == 2  # one pre-cleanup, ONE from the rollback
    # ...the container rm, which is the LAST docker call (the pre-run rm
    # for name-squatting long predates it), and then...
    assert mock_docker_run[-1] == ["docker", "rm", "-f", name]
    # ...the token revoke, which ran because there were no siblings.
    assert any("/api/session/revoke" in u for _, u in seen)
    for expected in (
        "undone: guacamole user",
        "undone: container",
        "undone: grading token",
    ):
        assert expected in err, expected


def test_rollback_skips_token_revoke_when_student_has_another_session(
    mock_docker_run, monkeypatch, capsys
):
    provision._UNDO.clear()
    monkeypatch.setattr(
        provision.gs, "sibling_containers", lambda *a, **k: ["guac-weasis-STU_SIB-111"]
    )
    seen = []
    monkeypatch.setattr(
        sys, "argv", ["provision-guacamole-session.py", "--student-id", "STU_SIB"]
    )
    with patch.object(
        urllib.request,
        "urlopen",
        side_effect=_failing_urlopen("/connections?token=", seen),
    ):
        with pytest.raises(SystemExit):
            provision.main()
    err = capsys.readouterr().err
    # revoke-by-student would kill the OTHER session's grading -- refused:
    assert not any("/api/session/revoke" in u for _, u in seen)
    assert "skipped token revoke" in err
    # everything per-SESSION was still undone:
    assert mock_docker_run[-1] == [
        "docker",
        "rm",
        "-f",
        _container_name_from(mock_docker_run),
    ]
    assert len([u for m, u in seen if m == "DELETE" and "/users/stu_STU_SIB" in u]) == 2


def test_docker_run_failure_leaves_no_orphan_token(
    mock_docker_run, monkeypatch, capsys
):
    provision._UNDO.clear()
    monkeypatch.setattr(provision.gs, "sibling_containers", lambda *a, **k: [])
    calls = mock_docker_run

    def _failing_run(args, **kwargs):
        calls.append(args)
        result = MagicMock()
        result.returncode = 1 if args[:2] == ["docker", "run"] else 0
        result.stderr = "image start failed" if result.returncode else ""
        result.stdout = ""
        return result

    seen = []
    monkeypatch.setattr(
        sys, "argv", ["provision-guacamole-session.py", "--student-id", "STU_RUN"]
    )
    with patch.object(provision.subprocess, "run", side_effect=_failing_run):
        with patch.object(
            urllib.request, "urlopen", side_effect=_failing_urlopen(None, seen)
        ):
            with pytest.raises(SystemExit) as stop:
                provision.main()
    assert stop.value.code == 1
    # the token minted BEFORE the failed run is revoked again...
    assert any("/api/session/revoke" in u for u in map(lambda t: t[1], seen))
    # ...and nothing Guacamole-side was ever created (we died before login).
    assert not any("/connections" in u or "/users?token" in u for _, u in seen)
    assert "docker run failed" in capsys.readouterr().err


def test_api_error_text_redacts_admin_token_and_echoed_secrets(capsys):
    # Owner credential scan on PR #191: every Guacamole URL carries
    # ?token=<admin token> and Guacamole echoes request objects back in
    # error bodies (connection attributes carry the VNC password). NO
    # error text -- raised or printed -- may contain either.
    admin_token = "SENTINEL-ADMIN-TOKEN-9f2c"
    vnc_secret = "SENTINEL-VNC-PASS-7a11"
    url = f"http://fake.local/app/api/session/data/postgresql/connections?token={admin_token}"
    body = json.dumps({"echo": {"parameters": {"password": vnc_secret}}}).encode()

    def _boom(req, timeout=15):
        raise provision.urllib.error.HTTPError(url, 400, "Bad Request", {}, _io.BytesIO(body))

    with patch.object(urllib.request, "urlopen", side_effect=_boom):
        with pytest.raises(provision.ProvisionAborted) as excinfo:
            provision.api_call("POST", url, data={"x": 1}, label="Guacamole (create connection)")
    msg = str(excinfo.value)
    assert admin_token not in msg
    assert vnc_secret not in msg
    assert "token=<redacted>" in msg
    assert '"password": "<redacted>"' in msg  # visibly redacted, not deleted
    assert len(msg) < 400

    with patch.object(urllib.request, "urlopen", side_effect=_boom):
        assert provision.api_call(
            "POST", url, data={"x": 1}, fatal=False, label="Guacamole (pre-cleanup)"
        ) is None
    err = capsys.readouterr().err
    assert admin_token not in err
    assert vnc_secret not in err


# ---------------------------------------------------------------------------
# #191 owner directive: per-student Guacamole accounts must expire.
# Verified against guacamole-client tag 1.6.0 (see provisioner's comment):
# JDBC attribute names are "valid-until"/"valid-from" (NO guac- prefix),
# DateField.FORMAT = "yyyy-MM-dd" (date only), validity ends at midnight
# AFTER the given day. The env var is in HOURS; day granularity is the
# documented consequence, the reaper stays the hard limit.
# ---------------------------------------------------------------------------

def test_parse_valid_until_date_semantics_and_rejection():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc)
    date_str, requested = provision.parse_valid_until(2, now=now)
    # p2 review: valid-until is the DATE of the expiry moment; the account
    # lives until 23:59:59.999 of that day -- NOT a "ceil" of anything.
    assert date_str == "2026-10-13"  # moment crosses midnight -> next day
    assert requested == "2026-10-13T01:30:00+00:00"
    for bad in ("0", "-3", "abc", "nan", "NaN", "inf", "-inf", "1e12", "9000"):
        with pytest.raises((ValueError, OverflowError)):
            provision.parse_valid_until(bad, now=now)


def test_stdout_shows_account_truth_not_requested_moment(
    mock_docker_run, monkeypatch, capsys
):
    provision._UNDO.clear()
    monkeypatch.setenv("GUACAMOLE_USER_VALID_HOURS", "36")
    monkeypatch.setattr(sys, "argv", ["provision-guacamole-session.py", "--student-id", "STU_OUT"])
    with patch.object(urllib.request, "urlopen", side_effect=_fake_urlopen):
        provision.main()
    payload = json.loads(capsys.readouterr().out)
    date_str, requested = provision.parse_valid_until(36)
    assert payload["account_valid_until"] == date_str
    assert payload["account_expires_at"] == f"{date_str}T23:59:59Z"
    # the misleading exact-lifetime moment stays out of operator output
    assert "expires_at" not in payload
    assert requested not in json.dumps(payload)


def test_user_create_payload_carries_expiry_attributes(mock_docker_run, monkeypatch):
    provision._UNDO.clear()
    monkeypatch.setenv("GUACAMOLE_USER_VALID_HOURS", "36")
    monkeypatch.setattr(sys, "argv", ["provision-guacamole-session.py", "--student-id", "STU_EXP"])
    bodies = []
    real_urlopen = _fake_urlopen

    def spy(req, timeout=15):
        if req.data:
            try:
                bodies.append(json.loads(req.data.decode()))
            except (ValueError, UnicodeDecodeError):
                pass  # form-encoded login body; not what this test probes
        return real_urlopen(req, timeout=timeout)

    with patch.object(urllib.request, "urlopen", side_effect=spy):
        provision.main()
    user_bodies = [
        d
        for d in bodies
        if isinstance(d, dict) and d.get("username", "").startswith("stu_")
    ]
    assert len(user_bodies) == 1
    attrs = user_bodies[0]["attributes"]
    assert attrs["timezone"] == "UTC"
    expected_date, _ = provision.parse_valid_until(36)
    assert attrs["valid-until"] == expected_date


def test_empty_lifetime_env_falls_back_to_default(mock_docker_run, monkeypatch):
    provision._UNDO.clear()
    monkeypatch.setenv("GUACAMOLE_USER_VALID_HOURS", "")
    monkeypatch.setattr(sys, "argv", ["provision-guacamole-session.py", "--student-id", "STU_DEF"])
    with patch.object(urllib.request, "urlopen", side_effect=_fake_urlopen):
        provision.main()  # must NOT abort: empty means "use the default"
    expected_default, _ = provision.parse_valid_until(
        provision.DEFAULT_USER_VALID_HOURS
    )
    assert expected_default == provision.parse_valid_until("48")[0]


@pytest.mark.parametrize("bad", ["0", "-3", "abc", "inf", "nan", "1e12", "9000"])
def test_bad_lifetime_aborts_before_any_side_effect(mock_docker_run, monkeypatch, capsys, bad):
    provision._UNDO.clear()
    monkeypatch.setenv("GUACAMOLE_USER_VALID_HOURS", bad)
    monkeypatch.setattr(sys, "argv", ["provision-guacamole-session.py", "--student-id", "STU_ZERO"])
    seen = []

    def spy(req, timeout=15):
        seen.append(req.full_url)
        return _fake_urlopen(req, timeout=timeout)

    with patch.object(urllib.request, "urlopen", side_effect=spy):
        with pytest.raises(SystemExit) as stop:
            provision.main()
    assert stop.value.code == 1
    err = capsys.readouterr().err
    assert "GUACAMOLE_USER_VALID_HOURS" in err
    # the abort happened BEFORE the token mint and before any docker call:
    assert seen == []
    assert mock_docker_run == []


# ---------------------------------------------------------------------------
# #191 CodeQL fix: the credential arrives ONLY as a 0600 file; stdout is
# non-secret; a failed write rolls the whole provision back.
# ---------------------------------------------------------------------------

def test_link_file_is_0600_dir_0700_and_stdout_names_only_the_path(
    mock_docker_run, monkeypatch, capsys, isolated_link_dir
):
    provision._UNDO.clear()
    monkeypatch.setattr(sys, "argv", ["provision-guacamole-session.py", "--student-id", "STU_LK"])
    with patch.object(urllib.request, "urlopen", side_effect=_fake_urlopen):
        provision.main()
    payload = json.loads(capsys.readouterr().out)
    path = payload["link_file"]
    assert oct(os.stat(isolated_link_dir).st_mode & 0o777) == "0o700"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600" if False else oct(os.stat(path).st_mode & 0o777) == "0o600"
    content = open(path).read()
    assert content.startswith("http://localhost:8090/guacamole/#/?username=stu_STU_LK&password=")
    # and the secret itself never reached stdout/stderr:
    secret = content.split("password=", 1)[1].strip()
    captured = capsys.readouterr()
    assert secret not in captured.out and secret not in captured.err
    assert "link" not in payload or payload["link"] == path
    assert "link_file" in payload and "link" not in payload


def test_link_file_write_refuses_to_overwrite_and_rolls_back(
    mock_docker_run, monkeypatch, capsys, isolated_link_dir
):
    # O_EXCL promise: a pre-existing file is never clobbered -- and, by the
    # undo-ordering fix, never DELETED by the rollback either.
    provision._UNDO.clear()
    isolated_link_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(isolated_link_dir, 0o700)  # pass the pre-existing-dir privacy check
    pre = isolated_link_dir / "guac-STU_PRE-1.link"
    pre.write_text("PRE-EXISTING DO NOT CLOBBER")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "provision-guacamole-session.py",
            "--student-id",
            "STU_PRE",
            "--session-id",
            "1",
        ],
    )
    seen = []

    def spy(req, timeout=15):
        seen.append(req.full_url)
        return _fake_urlopen(req, timeout=timeout)

    with patch.object(urllib.request, "urlopen", side_effect=spy):
        with pytest.raises(SystemExit) as stop:
            provision.main()
    assert stop.value.code == 1
    assert pre.read_text() == "PRE-EXISTING DO NOT CLOBBER"
    err = capsys.readouterr().err
    assert "could not write session link file" in err
    # rollback order: ... container rm, then the token undo's OWN
    # sibling-check (`docker ps -a`) is the final call -- assert presence,
    # not position.
    assert ["docker", "rm", "-f", "guac-weasis-STU_PRE-1"] in mock_docker_run
    assert any("/api/session/revoke" in u for u in seen)  # token undid


def test_link_write_oserror_rolls_back_and_never_prints_the_link(
    mock_docker_run, monkeypatch, capsys, tmp_path
):
    # Make the link dir UNWRITABLE at the makedirs/chmod stage by pointing
    # --link-dir AT an existing plain file (no global os.open patching).
    provision._UNDO.clear()
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "provision-guacamole-session.py",
            "--student-id",
            "STU_WF",
            "--link-dir",
            str(blocker),
        ],
    )
    with patch.object(urllib.request, "urlopen", side_effect=_fake_urlopen):
        with pytest.raises(SystemExit) as stop:
            provision.main()
    assert stop.value.code == 1
    captured = capsys.readouterr()
    assert "exists and is not a directory" in captured.err
    # no credential fallback printing, stdout OR stderr:
    assert "password=" not in captured.out + captured.err
    assert "#/?username=" not in captured.out + captured.err
    assert captured.out == ""  # stdout JSON never emitted on abort
    rm_calls = [
        c
        for c in mock_docker_run
        if c[:3] == ["docker", "rm", "-f"]
        and c[3].startswith("guac-weasis-STU_WF-")
    ]
    assert rm_calls, "this run's container must have been removed"


def test_preexisting_public_link_dir_is_refused_and_untouched(
    mock_docker_run, monkeypatch, capsys, isolated_link_dir
):
    # p2 review: never chmod a directory this run did not create.
    provision._UNDO.clear()
    isolated_link_dir.mkdir(parents=True, mode=0o755)
    monkeypatch.setattr(
        sys, "argv", ["provision-guacamole-session.py", "--student-id", "STU_PUB"]
    )
    seen = []

    def spy(req, timeout=15):
        seen.append(req.full_url)
        return _fake_urlopen(req, timeout=timeout)

    with patch.object(urllib.request, "urlopen", side_effect=spy):
        with pytest.raises(SystemExit) as stop:
            provision.main()
    assert stop.value.code == 1
    err = capsys.readouterr().err
    assert "is not private" in err and "chmod 700" in err
    assert oct(os.stat(isolated_link_dir).st_mode & 0o777) == "0o755"  # unchanged
    # the half-built session was still rolled back:
    assert any(c[:3] == ["docker", "rm", "-f"] for c in mock_docker_run)
    assert any("/api/session/revoke" in u for u in seen)


def test_teardown_step_deletes_the_link_file(tmp_path):
    """Library contract used by BOTH CLIs: teardown removes the file."""
    import guacamole_session as gs

    link_dir = tmp_path / "links"
    link_dir.mkdir()
    name = gs.link_file_name("guac-weasis-STU_TD-9")
    f = link_dir / name
    f.write_text("secret link\n")
    assert gs.remove_link_file(str(link_dir), "guac-weasis-STU_TD-9") is True
    assert not f.exists()
    # idempotent: second removal is a benign no-op
    assert gs.remove_link_file(str(link_dir), "guac-weasis-STU_TD-9") is False
    # namespace guard: the name comes only from the container, never a path
    import pytest as _p
    with _p.raises(gs.NamespaceViolation):
        gs.link_file_name("guac-weasis-bad/../etc-passwd-1")


def test_nonnumeric_session_id_refused_before_any_side_effect(
    mock_docker_run, monkeypatch, capsys
):
    # Owner decision after the e2e CI break: digits-only, enforced BEFORE
    # the token mint -- the old "e2e" id used to blow up LATE (raw
    # NamespaceViolation from link_file_name) after real objects existed.
    provision._UNDO.clear()
    monkeypatch.setattr(
        sys,
        "argv",
        ["provision-guacamole-session.py", "--student-id", "STU_X", "--session-id", "e2e"],
    )
    seen = []

    def spy(req, timeout=15):
        seen.append(req.full_url)
        return _fake_urlopen(req, timeout=timeout)

    with patch.object(urllib.request, "urlopen", side_effect=spy):
        with pytest.raises(SystemExit) as stop:
            provision.main()
    assert stop.value.code == 1
    err = capsys.readouterr().err
    assert "session-id must be 1-20 digits" in err
    assert seen == [] and mock_docker_run == []  # zero side effects
    assert "Traceback" not in err


def test_late_unexpected_error_goes_through_rollback_not_traceback(
    mock_docker_run, monkeypatch, capsys, isolated_link_dir
):
    # p2 item 2: ANY exception after the first side effect must exit via
    # the compensation path -- here we make link_file_name explode the way
    # the pre-guard e2e session id did.
    provision._UNDO.clear()
    monkeypatch.setattr(
        sys, "argv", ["provision-guacamole-session.py", "--student-id", "STU_LATE"]
    )

    def boom(_name):
        raise RuntimeError("synthetic late failure")

    seen = []

    def spy(req, timeout=15):
        seen.append(req.full_url)
        return _fake_urlopen(req, timeout=timeout)

    monkeypatch.setattr(provision.gs, "link_file_name", boom)
    with patch.object(urllib.request, "urlopen", side_effect=spy):
        with pytest.raises(SystemExit) as stop:
            provision.main()
    assert stop.value.code == 1
    err = capsys.readouterr().err
    assert "unexpected failure: RuntimeError: synthetic late failure" in err
    assert "Traceback" not in err
    assert any(c[:3] == ["docker", "rm", "-f"] for c in mock_docker_run)  # container undid
    assert any("/api/session/revoke" in u for u in seen)  # token undid
