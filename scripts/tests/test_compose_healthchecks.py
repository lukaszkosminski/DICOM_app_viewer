"""Compose health checks and start-up ordering (issue #69).

Reads the RESOLVED configuration (`docker compose config --format json`), so it checks what
Docker would really run. Skipped when Docker is not available. Secrets are given sentinel values
so the test can also assert that no health probe carries a password on its command line.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "SENTINEL-PASSWORD-must-not-appear-in-a-probe"
ENV = {
    "ORTHANC_PASSWORD": SENTINEL,
    "GRADING_COORDINATOR_KEY": "k" * 40,
    "GUACAMOLE_DB_PASSWORD": SENTINEL + "-db",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": "/tmp",
}

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")


def _config(*files):
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", str(ROOT / f)]
    cmd += ["config", "--format", "json"]
    out = subprocess.run(cmd, capture_output=True, text=True, env=ENV, cwd=ROOT, check=False)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["services"]


@pytest.fixture(scope="module", params=["docker-compose.yml", "docker-compose.remote-host.yml"])
def core(request):
    return _config(request.param)


@pytest.fixture(scope="module")
def guacamole():
    return _config("docker-compose.yml", "docker-compose.guacamole.yml")


def test_every_core_service_has_a_health_check(core):
    for name in ("orthanc", "grading-api", "viewer"):
        hc = core[name].get("healthcheck")
        assert hc and hc["test"], name
        assert hc.get("interval") and hc.get("retries") and hc.get("start_period"), name


def test_the_viewer_waits_for_healthy_backends(core):
    deps = core["viewer"]["depends_on"]
    for name in ("orthanc", "grading-api"):
        assert deps[name]["condition"] == "service_healthy", name


def test_guacamole_waits_for_its_database_and_guacd(guacamole):
    deps = guacamole["guacamole"]["depends_on"]
    for name in ("guacd", "guacamole-db"):
        assert deps[name]["condition"] == "service_healthy", name
    for name in ("guacamole", "guacd", "guacamole-db"):
        assert guacamole[name]["healthcheck"]["test"], name


def test_the_probes_target_the_real_readiness_endpoints(core):
    api = " ".join(core["grading-api"]["healthcheck"]["test"])
    assert "/healthz" in api and ":8000" in api
    assert "/system" in " ".join(core["orthanc"]["healthcheck"]["test"])
    assert "/api/healthz" in " ".join(core["viewer"]["healthcheck"]["test"])


def test_no_probe_carries_a_password_on_its_command_line(core, guacamole):
    for services in (core, guacamole):
        for name, svc in services.items():
            probe = json.dumps(svc.get("healthcheck", {}).get("test", []))
            assert SENTINEL not in probe, name
