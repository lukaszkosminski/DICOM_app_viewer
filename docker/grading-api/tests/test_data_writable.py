"""Start-up writability check (issue #51 follow-up): a root-owned data volume must stop the
service at start-up with a clear message, not serve 500s on the first write."""

import os

import pytest

from app import db as db_module


def _deny(monkeypatch, *denied):
    real = os.access

    def fake(path, mode, *a, **kw):
        if os.path.abspath(str(path)) in {os.path.abspath(d) for d in denied}:
            return False
        return real(path, mode, *a, **kw)

    monkeypatch.setattr(db_module.os, "access", fake)


def test_a_writable_volume_starts_normally(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "grading.db"))
    db_module.init_db()
    db_module.init_db()  # and again, with the file now existing


def test_a_read_only_directory_stops_the_service_with_the_fix_in_the_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "grading.db"))
    _deny(monkeypatch, str(tmp_path))
    with pytest.raises(SystemExit) as exc:
        db_module.init_db()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "CONFIGURATION ERROR" in err and str(tmp_path) in err and "chown -R 10001:10001" in err
    assert "grading-db-init" in err


def test_a_root_owned_database_file_in_a_writable_directory_is_caught_too(tmp_path, monkeypatch, capsys):
    path = tmp_path / "grading.db"
    monkeypatch.setattr(db_module, "DB_PATH", str(path))
    db_module.init_db()  # creates the file
    _deny(monkeypatch, str(path))  # the directory stays writable: exactly the root-owned-file case
    with pytest.raises(SystemExit) as exc:
        db_module.init_db()
    assert exc.value.code == 2
    assert "database file" in capsys.readouterr().err


def test_nothing_is_created_or_backed_up_before_the_check_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "grading.db"))
    _deny(monkeypatch, str(tmp_path))
    with pytest.raises(SystemExit):
        db_module.init_db()
    assert list(tmp_path.iterdir()) == []
