"""POST /reset is off by default (issue #98): it deletes the doctor's recorded answers, which feed
a scientific publication. The setting, the 403, and what the panel is told."""

import pytest

from app import config

H = "X-Grading-Token"


@pytest.mark.parametrize("raw", [None, "", "  ", "0", "false", "No", "OFF"])
def test_the_setting_defaults_to_off(raw):
    assert config.validate_reset_enabled(raw) is False


@pytest.mark.parametrize("raw", ["1", "true", "True", "YES", "on", " 1 "])
def test_the_setting_can_be_switched_on(raw):
    assert config.validate_reset_enabled(raw) is True


def test_garbage_is_rejected_loudly_and_quoted_safely():
    with pytest.raises(config.ConfigError) as exc:
        config.validate_reset_enabled("maybe" * 40)
    assert "GRADING_RESET_ENABLED" in str(exc.value) and len(str(exc.value)) < 300


def test_reset_is_refused_by_default_and_changes_nothing(client, mint_token):
    token = mint_token("stu_noreset")
    client.post("/submit", json={"token": token, "case_id": 1, "stage": "learning", "text": "keep me"})
    resp = client.post("/reset", json={"token": token})
    assert resp.status_code == 403 and resp.json()["error_code"] == "RESET_DISABLED"
    # the recorded answer is still there
    case = client.get("/case", headers={H: token}).json()
    assert case["stage"] == "assessment"


def test_a_disabled_reset_still_needs_a_valid_token(client):
    assert client.post("/reset", json={"token": "nope"}).status_code == 401


def test_the_panel_is_told_whether_to_offer_the_restart_button(client, mint_token, monkeypatch):
    from app import main as main_module

    token = mint_token("stu_flag")
    for body in ({"text": "x"}, {"category": "3", "modifier_s": False}, {"category": "2", "modifier_s": True}):
        case = client.get("/case", headers={H: token}).json()
        client.post("/submit", json={"token": token, "case_id": case["case_id"], "stage": case["stage"], **body})
    client.post("/finalize", json={"token": token})
    assert client.get("/review", headers={H: token}).json()["reset_enabled"] is False
    assert client.get("/results", headers={H: token}).json()["reset_enabled"] is False
    monkeypatch.setattr(main_module, "RESET_ENABLED", True)
    assert client.get("/results", headers={H: token}).json()["reset_enabled"] is True
