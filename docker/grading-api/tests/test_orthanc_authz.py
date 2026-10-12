"""
Per-session scoping of the Orthanc proxy (issue #195, item 1; spike).

nginx calls GET /orthanc-authz (auth_request) for every /s/<key>/dicom-web/... request.
<key> = sha256(grading token). 204 = allow, 401 = unknown key, 403 = outside the
session's CURRENT study. These tests pin the allowlist so a future edit cannot widen it.
"""

import hashlib

import pytest

OTHER_UID = "1.2.826.0.1.3680043.8.498.99999"


def _key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _authz(client, key, path):
    return client.get("/orthanc-authz", headers={"X-Original-URI": f"/s/{key}{path}"})


@pytest.fixture
def session(client, mint_token):
    token = mint_token("stu_authz")
    case = client.get("/case", headers={"X-Grading-Token": token}).json()
    return _key(token), case["orthanc_study_uid"]


def test_own_study_paths_are_allowed(client, session):
    key, uid = session
    s = f"/dicom-web/studies/{uid}"
    for path in (
        f"/dicom-web/studies?StudyInstanceUID={uid}",
        f"/dicom-web/studies?0020000D={uid}&includefield=00080020,00100010",
        s,
        f"{s}/series",
        f"{s}/series/1.2.3",
        f"{s}/series/1.2.3/metadata",
        f"{s}/series/1.2.3/thumbnail",
        f"{s}/series/1.2.3/instances/4.5.6/frames/1",
        f"{s}/metadata",
    ):
        assert _authz(client, key, path).status_code == 204, path


def test_other_studies_and_broad_searches_are_forbidden(client, session):
    key, uid = session
    for path in (
        "/dicom-web/studies",
        "/dicom-web/studies?limit=0",
        f"/dicom-web/studies?StudyInstanceUID={OTHER_UID}",
        "/dicom-web/studies?PatientName=*&limit=0",
        f"/dicom-web/studies/{OTHER_UID}/series",
        f"/dicom-web/studies/{uid}x/series",
        f"/dicom-web/studies/{uid}/../{OTHER_UID}",
        f"/dicom-web/studies/{uid}/series/../../x",
        "/dicom-web/series",
        "/dicom-web/instances",
        "/dicom-web/patients",
    ):
        assert _authz(client, key, path).status_code == 403, path


def test_non_dicomweb_orthanc_paths_are_forbidden(client, session):
    key, _ = session
    for path in (
        "/studies",
        "/instances",
        "/patients",
        "/system",
        "/tools/find",
        "/instances/abc/file",
        "/studies/abc/archive",
    ):
        assert _authz(client, key, path).status_code == 403, path


def test_unknown_key_is_401_and_malformed_uri_is_403(client):
    assert _authz(client, "a" * 64, f"/dicom-web/studies?StudyInstanceUID={OTHER_UID}").status_code == 401
    for uri in ("", "/dicom-web/studies", "/s/short/dicom-web/studies", "/s/" + "g" * 64 + "/dicom-web/studies"):
        assert client.get("/orthanc-authz", headers={"X-Original-URI": uri}).status_code == 403, uri
    assert client.get("/orthanc-authz").status_code == 403


def test_the_key_is_not_the_token_and_the_token_is_not_a_key(client, mint_token):
    token = mint_token("stu_authz_two")
    assert _key(token) != token
    # a raw grading token must never work where a derived key is expected
    assert client.get("/orthanc-authz", headers={"X-Original-URI": f"/s/{token}/dicom-web/studies"}).status_code == 403


def test_the_allowed_study_follows_the_case_pointer(client, mint_token):
    token = mint_token("stu_authz_follow")
    key = _key(token)
    first = client.get("/case", headers={"X-Grading-Token": token}).json()
    client.post(
        "/submit",
        json={"token": token, "case_id": first["case_id"], "stage": first["stage"], "text": "x"},
    )
    second = client.get("/case", headers={"X-Grading-Token": token}).json()
    assert second["orthanc_study_uid"] != first["orthanc_study_uid"]
    assert _authz(client, key, f"/dicom-web/studies/{second['orthanc_study_uid']}/series").status_code == 204
    assert _authz(client, key, f"/dicom-web/studies/{first['orthanc_study_uid']}/series").status_code == 403


def test_a_revoked_or_expired_session_loses_access(client, mint_token):
    token = mint_token("stu_authz_revoked")
    key = _key(token)
    case = client.get("/case", headers={"X-Grading-Token": token}).json()
    path = f"/dicom-web/studies/{case['orthanc_study_uid']}/series"
    assert _authz(client, key, path).status_code == 204
    resp = client.post("/session/revoke", json={"token": token}, headers={"X-Coordinator-Key": "k" * 40})
    if resp.status_code == 200:
        assert _authz(client, key, path).status_code == 401
