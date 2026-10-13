"""
Root conftest.py, not tests/conftest.py -- pytest always inserts the
directory containing a discovered conftest.py into sys.path, which is what
makes `from app import db` / `from app.main import app` importable below
without any manual sys.path hacking (this file lives next to app/, which
is a real package -- see app/__init__.py).
"""

import os

# main.py reads GRADING_COORDINATOR_KEY at *module import time* (fails
# loudly if unset, same philosophy as ORTHANC_PASSWORD elsewhere in this
# project) -- has to be set before `from app.main import app` below runs,
# not inside a fixture (fixtures only apply once a test is already
# executing, well after this module's own top-level import has happened).
# Length >= 32 (issue #97): app/config.py now enforces a minimum key
# length for EVERY process importing the app, tests included -- no dev
# exception, so this key has to satisfy the same rule production does.
os.environ.setdefault("GRADING_COORDINATOR_KEY", "test-only-coordinator-key-0123456789abcdef")

import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A fresh, isolated SQLite file per test.

    Monkeypatches db.DB_PATH directly rather than the GRADING_DB_PATH env
    var it's read from in production: get_connection() reads the module
    global at call time (not baked into a closure at import time), so
    reassigning it here is enough -- no import-order or module-reload
    gymnastics needed, and it means every test starts from a genuinely
    empty database (fresh tables, freshly re-seeded cases), never sharing
    state with another test or a real deployment's data.
    """
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "grading-test.db"))
    # TestClient as a context manager runs FastAPI's startup event (which
    # calls db.init_db()) against the now-patched DB_PATH, then its
    # shutdown event on exit -- the standard FastAPI testing pattern,
    # not a hand-rolled substitute for it.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def mint_token(client):
    """Every endpoint except POST /session itself takes a token, never a
    bare student_id (see main.py's module docstring for why) -- this is
    the one path that creates one, so tests don't each hand-roll the same
    POST /session call."""

    def _mint(student_id: str, session_id: str = "sess_test") -> str:
        resp = client.post(
            "/session",
            json={"student_id": student_id, "session_id": session_id},
            headers={"X-Coordinator-Key": os.environ["GRADING_COORDINATOR_KEY"]},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    return _mint


@pytest.fixture()
def reset_on(monkeypatch):
    """POST /reset is OFF by default (issue #98, GRADING_RESET_ENABLED); the tests of the
    reset behaviour itself switch it on explicitly."""
    from app import main as main_module

    monkeypatch.setattr(main_module, "RESET_ENABLED", True)
