#!/usr/bin/env python3
"""Start "the next doctor" for a demo: a fresh student id, a fresh session, the link on the clipboard.

Why this exists: starting over inside the app (POST /reset) deletes the doctor's recorded answers and
is OFF by default (issue #98, GRADING_RESET_ENABLED). To show the whole process more than once, give
each run to a NEW doctor instead: every student id starts at the learning stage with its own progress,
and everything the previous doctor answered stays in the database.

What it does, in order:
  1. (--end-previous) ends the previous demo session of the same flow: Guacamole through the shared
     teardown library (container, Guacamole user and connection, grading token, link file); Kasm through
     its developer API (destroy_kasm). Kasm Community Edition allows only 5 concurrent sessions, so end
     old ones before you pass that.
  2. picks the next student id, DEMO_001, DEMO_002, ... (a counter file; --student-id overrides);
  3. creates the session with the same scripts the real flow uses:
       kasm : scripts/create-session.py        (needs KASM_* variables, see that script)
       guac : scripts/provision-guacamole-session.py
  4. puts the session link on the clipboard and NEVER prints it. For Guacamole the link is also in a
     0600 file (printed path). Open it in a browser.

It remembers the last session per flow in local/demo-state.json (git-ignored) so --end-previous knows
what to end. It reads the repository's .env for GRADING_COORDINATOR_KEY etc.; Kasm credentials come
from the environment or --kasm-env FILE (KEY=VALUE lines).

Examples:
  python3 scripts/demo-next-doctor.py --flow guac
  python3 scripts/demo-next-doctor.py --flow kasm --kasm-env /tmp/kasm.env --insecure --end-previous
  python3 scripts/demo-next-doctor.py --flow guac --end-previous      # new doctor, previous one ended

Exit status: 0 = session ready and link copied; 1 = something failed (message on stderr, nothing half-made
is left behind by the underlying scripts); 2 = bad usage or missing configuration.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

STATE_FILE = REPO / "local" / "demo-state.json"
COUNTER_FILE = REPO / "local" / "demo-counter"
STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class DemoError(Exception):
    """A problem phrased for the person running the demo."""


# ---- small pure helpers (unit-tested) -------------------------------------------------------


def load_env_file(path: Path) -> dict:
    """KEY=VALUE lines; blank lines and # comments ignored; surrounding quotes stripped."""
    env = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip().strip("'\"")
    return env


def next_student_id(counter_path: Path = COUNTER_FILE) -> str:
    """DEMO_001, DEMO_002, ... The counter survives between runs; a corrupt file restarts at 1."""
    try:
        n = int(counter_path.read_text().strip())
    except (OSError, ValueError):
        n = 0
    n += 1
    counter_path.parent.mkdir(parents=True, exist_ok=True)
    counter_path.write_text(str(n))
    return f"DEMO_{n:03d}"


def read_state(path: Path = STATE_FILE) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state: dict, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))
    os.chmod(path, 0o600)


def parse_json_object(text: str) -> dict:
    """The scripts print one JSON object (maybe after warning lines): take the last {...} block."""
    start = text.find("{")
    if start < 0:
        raise DemoError("the session script printed no JSON result")
    try:
        data = json.loads(text[start:])
    except ValueError as exc:
        raise DemoError(
            f"the session script's result is not valid JSON ({exc})"
        ) from None
    if not isinstance(data, dict):
        raise DemoError("the session script's result is not a JSON object")
    return data


def copy_to_clipboard(text: str, runner=subprocess.run) -> str:
    """-> name of the tool used. Raises DemoError when none is available (the link is then NOT lost
    for Guacamole, whose file stays; for Kasm it cannot be recovered, so we say so)."""
    for tool, argv in (
        ("clip.exe", ["clip.exe"]),
        ("pbcopy", ["pbcopy"]),
        ("wl-copy", ["wl-copy"]),
        ("xclip", ["xclip", "-selection", "clipboard"]),
    ):
        if shutil.which(argv[0]):
            res = runner(argv, input=text.encode(), capture_output=True, check=False)
            if res.returncode == 0:
                return tool
    raise DemoError(
        "no clipboard tool found (clip.exe, pbcopy, wl-copy, xclip); install one or use --write-link FILE"
    )


def detect_docker_network(runner=subprocess.run) -> str:
    """The Docker network of the running grading-api container whose name ends in ipcmc-internal
    (the Compose project name differs between checkouts, so it is never hardcoded)."""
    res = runner(
        [
            "docker",
            "inspect",
            "-f",
            "{{range $k, $v := .NetworkSettings.Networks}}{{$k}}\n{{end}}",
            "ipcmc-grading-api",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    names = [n for n in (res.stdout or "").split() if n.endswith("ipcmc-internal")]
    if res.returncode != 0 or not names:
        raise DemoError(
            "cannot find the stack: is the container ipcmc-grading-api running? (docker compose up -d)"
        )
    return names[0]


# ---- the two flows -------------------------------------------------------------------------


def start_guacamole(student_id: str, env: dict, runner=subprocess.run) -> dict:
    env = dict(env)
    env.setdefault("DOCKER_NETWORK", detect_docker_network(runner))
    res = runner(
        [
            sys.executable,
            str(REPO / "scripts" / "provision-guacamole-session.py"),
            "--student-id",
            student_id,
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        check=False,
    )
    if res.returncode != 0:
        raise DemoError(
            f"Guacamole provisioning failed:\n{(res.stderr or '').strip()[-600:]}"
        )
    data = parse_json_object(res.stdout)
    link_file = data.get("link_file")
    if not link_file:
        raise DemoError("provisioning result has no link_file")
    path = Path(link_file) if Path(link_file).is_absolute() else REPO / link_file
    try:
        link = path.read_text().strip()
    except OSError as exc:
        raise DemoError(f"cannot read the link file {path}: {exc}") from None
    return {
        "flow": "guac",
        "student_id": student_id,
        "session_id": str(data["session_id"]),
        "container_name": data["container_name"],
        "link": link,
        "link_file": str(path),
    }


def start_kasm(
    student_id: str, env: dict, insecure: bool, runner=subprocess.run
) -> dict:
    missing = [
        k
        for k in (
            "KASM_SERVER",
            "KASM_API_KEY",
            "KASM_API_KEY_SECRET",
            "KASM_IMAGE_ID",
            "GRADING_COORDINATOR_KEY",
        )
        if not env.get(k)
    ]
    if missing:
        raise DemoError(
            f"missing configuration for Kasm: {', '.join(missing)} (environment or --kasm-env FILE)"
        )
    argv = [
        sys.executable,
        str(REPO / "scripts" / "create-session.py"),
        "--student-id",
        student_id,
    ]
    if insecure:
        argv.append("--insecure")
    res = runner(argv, capture_output=True, text=True, env=env, cwd=REPO, check=False)
    if res.returncode != 0:
        raise DemoError(
            f"Kasm session creation failed:\n{(res.stderr or '').strip()[-600:]}"
        )
    data = parse_json_object(res.stdout)
    if not data.get("link"):
        raise DemoError("the Kasm result has no link")
    return {
        "flow": "kasm",
        "student_id": student_id,
        "session_id": str(data.get("session_id", "")),
        "kasm_id": data.get("kasm_id"),
        "user_id": data.get("user_id"),
        "link": data["link"],
    }


def end_guacamole(prev: dict, env: dict, runner=subprocess.run) -> str:
    env = dict(env)
    env.setdefault("DOCKER_NETWORK", detect_docker_network(runner))
    res = runner(
        [
            sys.executable,
            str(REPO / "scripts" / "teardown-guacamole-session.py"),
            "--student-id",
            prev["student_id"],
            "--session-id",
            prev["session_id"],
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO,
        check=False,
    )
    if res.returncode != 0:
        raise DemoError(
            f"ending the previous Guacamole session failed:\n{(res.stdout + res.stderr).strip()[-600:]}"
        )
    return f"ended Guacamole session of {prev['student_id']}"


def end_kasm(prev: dict, env: dict, insecure: bool, opener=None) -> str:
    import ssl
    import urllib.request

    if not (prev.get("kasm_id") and prev.get("user_id")):
        raise DemoError(
            "the previous Kasm session was not recorded completely; end it in the Kasm admin UI"
        )
    body = json.dumps(
        {
            "api_key": env["KASM_API_KEY"],
            "api_key_secret": env["KASM_API_KEY_SECRET"],
            "kasm_id": prev["kasm_id"],
            "user_id": prev["user_id"],
        }
    ).encode()
    req = urllib.request.Request(
        env["KASM_SERVER"].rstrip("/") + "/api/public/destroy_kasm",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        resp = (opener or urllib.request.urlopen)(req, timeout=30, context=ctx)
        status = getattr(resp, "status", 200)
    except Exception as exc:  # noqa: BLE001 - the message names the type only, never the request (it holds the API key)
        raise DemoError(
            f"ending the previous Kasm session failed ({type(exc).__name__}); end it in the Kasm admin UI"
        ) from None
    if status != 200:
        raise DemoError(f"Kasm refused to end the previous session (HTTP {status})")
    return f"ended Kasm session of {prev['student_id']}"


# ---- main ------------------------------------------------------------------------------------


def main(
    argv=None, runner=subprocess.run, clipboard=copy_to_clipboard, kasm_end=end_kasm
) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--flow", required=True, choices=["kasm", "guac"])
    ap.add_argument("--student-id", help="override the next DEMO_nnn id")
    ap.add_argument(
        "--end-previous",
        action="store_true",
        help="end this flow's previous demo session first",
    )
    ap.add_argument("--kasm-env", help="file with KASM_* variables (KEY=VALUE lines)")
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="Kasm with a self-signed certificate (local lab only)",
    )
    ap.add_argument(
        "--write-link",
        help="also write the link to this FILE with mode 0600 (instead of relying on the clipboard)",
    )
    ap.add_argument("--state-file", default=str(STATE_FILE), help=argparse.SUPPRESS)
    ap.add_argument("--counter-file", default=str(COUNTER_FILE), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    env = dict(os.environ)
    try:
        if (REPO / ".env").exists():
            for k, v in load_env_file(REPO / ".env").items():
                env.setdefault(k, v)
        if args.kasm_env:
            env.update(load_env_file(Path(args.kasm_env)))
        env.setdefault("GRADING_API_URL", "http://localhost:8080")
        if not env.get("GRADING_COORDINATOR_KEY"):
            raise DemoError(
                "GRADING_COORDINATOR_KEY is not set (it is in the repository's .env)"
            )
        if args.student_id and not STUDENT_ID_RE.match(args.student_id):
            raise DemoError(
                "--student-id may contain only letters, digits, _ . - (max 64)"
            )

        state_path, counter_path = Path(args.state_file), Path(args.counter_file)
        state = read_state(state_path)
        if args.end_previous:
            prev = state.get(args.flow)
            if prev:
                print(
                    end_guacamole(prev, env, runner)
                    if args.flow == "guac"
                    else kasm_end(prev, env, args.insecure)
                )
                state.pop(args.flow, None)
                write_state(state, state_path)
            else:
                print(f"no previous {args.flow} demo session recorded; nothing to end")

        student_id = args.student_id or next_student_id(counter_path)
        session = (
            start_guacamole(student_id, env, runner)
            if args.flow == "guac"
            else start_kasm(student_id, env, args.insecure, runner)
        )
        record = {k: v for k, v in session.items() if k != "link"}
        state[args.flow] = record
        write_state(state, state_path)

        if args.write_link:
            fd = os.open(args.write_link, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(session["link"] + "\n")
        try:
            tool = clipboard(session["link"], runner)
            where = f"the link is on your clipboard ({tool})"
        except DemoError as exc:
            if args.flow == "guac" or args.write_link:
                where = f"{exc}; the link is in {session.get('link_file') or args.write_link}"
            else:
                raise
        print(
            f"OK  flow={args.flow}  doctor={student_id}  session={session['session_id']}"
        )
        print(f"    {where}")
        if session.get("link_file"):
            print(f"    link file (mode 0600): {session['link_file']}")
        print(
            "    Open it in a browser. The doctor starts at the learning stage with empty progress."
        )
        return 0
    except DemoError as exc:
        print(f"demo-next-doctor: {exc}", file=sys.stderr)
        return (
            2
            if "missing configuration" in str(exc)
            or "not set" in str(exc)
            or "--student-id" in str(exc)
            else 1
        )


if __name__ == "__main__":
    sys.exit(main())
