"""scripts/demo-next-doctor.py: a fresh doctor per demo run, the link on the clipboard, never printed."""

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "demo-next-doctor.py"
spec = importlib.util.spec_from_file_location("demo_next_doctor", SCRIPT)
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)

LINK = "https://localhost/#/connect/SECRET-LINK-SENTINEL"


def done(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


class Runner:
    """Fake subprocess.run: records argv, answers by script name."""

    def __init__(self, kasm_ok=True, guac_ok=True, link_file=None, teardown_ok=True):
        self.calls, self.kasm_ok, self.guac_ok, self.link_file, self.teardown_ok = (
            [],
            kasm_ok,
            guac_ok,
            link_file,
            teardown_ok,
        )

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        joined = " ".join(map(str, argv))
        if argv[0] == "docker":
            return done("wt_ipcmc-internal\nkasm_default_network\n")
        if "create-session.py" in joined:
            return done(
                json.dumps(
                    {
                        "session_id": "111",
                        "kasm_id": "k1",
                        "user_id": "u1",
                        "link": LINK,
                    }
                ),
                code=0 if self.kasm_ok else 1,
                stderr="boom",
            )
        if "provision-guacamole-session.py" in joined:
            body = {
                "session_id": "222",
                "container_name": "guac-weasis-X-222",
                "link_file": str(self.link_file),
            }
            return done(json.dumps(body), code=0 if self.guac_ok else 1, stderr="boom")
        if "teardown-guacamole-session.py" in joined:
            return done("ok", code=0 if self.teardown_ok else 1)
        raise AssertionError(argv)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "REPO", tmp_path)  # no real .env is read
    monkeypatch.setenv("GRADING_COORDINATOR_KEY", "k" * 40)
    link = tmp_path / "guac.link"
    link.write_text(LINK + "\n")
    return tmp_path, link


def run_main(tmp, runner, *extra, clip=None, kasm_end=None):
    clip = clip or (lambda text, r=None: "fake-clip")
    argv = [
        "--state-file",
        str(tmp / "state.json"),
        "--counter-file",
        str(tmp / "counter"),
        *extra,
    ]
    return demo.main(
        argv,
        runner=runner,
        clipboard=clip,
        kasm_end=kasm_end or (lambda p, e, i: "ended"),
    )


# ---- helpers ----


def test_student_ids_count_up_and_a_corrupt_counter_restarts(tmp_path):
    c = tmp_path / "counter"
    assert [demo.next_student_id(c) for _ in range(3)] == [
        "DEMO_001",
        "DEMO_002",
        "DEMO_003",
    ]
    c.write_text("not a number")
    assert demo.next_student_id(c) == "DEMO_001"


def test_env_file_parsing(tmp_path):
    f = tmp_path / "e"
    f.write_text("# c\n\nA=1\nB='two'\nC = \"3\"\nbad line\n")
    assert demo.load_env_file(f) == {"A": "1", "B": "two", "C": "3"}


def test_json_result_is_taken_after_warning_lines_and_garbage_is_an_error():
    assert demo.parse_json_object('WARNING: x\n{"a": 1}') == {"a": 1}
    for bad in ("no json", "{broken", "[1]"):
        with pytest.raises(demo.DemoError):
            demo.parse_json_object(bad)


def test_network_detection_uses_the_running_stack_and_fails_clearly_without_it():
    assert demo.detect_docker_network(Runner()) == "wt_ipcmc-internal"
    with pytest.raises(demo.DemoError, match="ipcmc-grading-api"):
        demo.detect_docker_network(lambda argv, **kw: done("", code=1))


# ---- Guacamole flow ----


def test_a_guacamole_doctor_is_provisioned_with_the_detected_network(sandbox, capsys):
    tmp, link = sandbox
    r = Runner(link_file=link)
    assert run_main(tmp, r, "--flow", "guac") == 0
    prov = next(c for c in r.calls if "provision-guacamole-session.py" in " ".join(c))
    assert prov[-2:] == ["--student-id", "DEMO_001"]
    out = capsys.readouterr().out
    assert "doctor=DEMO_001" in out and "clipboard (fake-clip)" in out
    assert "SECRET-LINK-SENTINEL" not in out  # the link is never printed
    state = json.loads((tmp / "state.json").read_text())
    assert state["guac"]["session_id"] == "222" and "link" not in state["guac"]
    assert oct((tmp / "state.json").stat().st_mode & 0o777) == "0o600"


def test_the_next_doctor_ends_the_previous_one_first(sandbox, capsys):
    tmp, link = sandbox
    r = Runner(link_file=link)
    run_main(tmp, r, "--flow", "guac")
    r.calls.clear()
    assert run_main(tmp, r, "--flow", "guac", "--end-previous") == 0
    kinds = [
        (
            "teardown"
            if "teardown" in " ".join(c)
            else "provision"
            if "provision" in " ".join(c)
            else "docker"
        )
        for c in r.calls
    ]
    assert kinds.index("teardown") < kinds.index("provision")
    td = next(c for c in r.calls if "teardown-guacamole-session.py" in " ".join(c))
    assert td[-4:] == ["--student-id", "DEMO_001", "--session-id", "222"]
    assert "doctor=DEMO_002" in capsys.readouterr().out


def test_a_failed_provisioning_is_reported_and_leaves_no_state(sandbox, capsys):
    tmp, link = sandbox
    assert run_main(tmp, Runner(guac_ok=False, link_file=link), "--flow", "guac") == 1
    assert "Guacamole provisioning failed" in capsys.readouterr().err
    assert not (tmp / "state.json").exists()


def test_without_a_clipboard_the_guacamole_link_is_still_in_its_file(sandbox, capsys):
    tmp, link = sandbox

    def no_clip(text, r=None):
        raise demo.DemoError("no clipboard tool found")

    assert run_main(tmp, Runner(link_file=link), "--flow", "guac", clip=no_clip) == 0
    assert str(link) in capsys.readouterr().out


# ---- Kasm flow ----

KASM_ENV = {
    "KASM_SERVER": "https://localhost",
    "KASM_API_KEY": "a",
    "KASM_API_KEY_SECRET": "b",
    "KASM_IMAGE_ID": "img",
}


def test_a_kasm_doctor_needs_its_configuration_and_says_what_is_missing(
    sandbox, capsys
):
    tmp, _ = sandbox
    assert run_main(tmp, Runner(), "--flow", "kasm") == 2
    err = capsys.readouterr().err
    assert "KASM_SERVER" in err and "KASM_API_KEY" in err


def test_a_kasm_doctor_is_created_and_the_ids_needed_to_end_it_are_remembered(
    sandbox, monkeypatch, capsys
):
    tmp, _ = sandbox
    for k, v in KASM_ENV.items():
        monkeypatch.setenv(k, v)
    r = Runner()
    assert run_main(tmp, r, "--flow", "kasm", "--insecure") == 0
    create = next(c for c in r.calls if "create-session.py" in " ".join(c))
    assert "--insecure" in create and create[2:4] == ["--student-id", "DEMO_001"]
    state = json.loads((tmp / "state.json").read_text())["kasm"]
    assert state["kasm_id"] == "k1" and state["user_id"] == "u1" and "link" not in state
    assert "SECRET-LINK-SENTINEL" not in capsys.readouterr().out


def test_a_kasm_link_that_cannot_be_copied_is_an_error_because_it_cannot_be_recovered(
    sandbox, monkeypatch
):
    tmp, _ = sandbox
    for k, v in KASM_ENV.items():
        monkeypatch.setenv(k, v)

    def no_clip(text, r=None):
        raise demo.DemoError("no clipboard tool found")

    assert run_main(tmp, Runner(), "--flow", "kasm", clip=no_clip) == 1
    assert (
        run_main(
            tmp,
            Runner(),
            "--flow",
            "kasm",
            "--write-link",
            str(tmp / "k.link"),
            clip=no_clip,
        )
        == 0
    )
    assert oct((tmp / "k.link").stat().st_mode & 0o777) == "0o600"


def test_ending_a_kasm_session_never_puts_the_api_key_in_an_error(monkeypatch):
    env = {**KASM_ENV, "KASM_API_KEY": "SECRET-API-KEY-SENTINEL"}

    def boom(req, timeout, context):
        raise OSError(f"cannot reach {req.data}")

    with pytest.raises(demo.DemoError) as exc:
        demo.end_kasm(
            {"student_id": "S", "kasm_id": "k", "user_id": "u"}, env, True, opener=boom
        )
    assert "SECRET-API-KEY-SENTINEL" not in str(exc.value)
    with pytest.raises(demo.DemoError, match="recorded completely"):
        demo.end_kasm({"student_id": "S"}, env, True)


# ---- usage errors ----


def test_bad_arguments_are_refused_before_anything_is_created(
    sandbox, monkeypatch, capsys
):
    tmp, link = sandbox
    r = Runner(link_file=link)
    assert run_main(tmp, r, "--flow", "guac", "--student-id", "bad id!") == 2
    monkeypatch.delenv("GRADING_COORDINATOR_KEY")
    assert run_main(tmp, r, "--flow", "guac") == 2
    assert r.calls == []
