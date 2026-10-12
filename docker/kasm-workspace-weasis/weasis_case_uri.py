#!/usr/bin/env python3
"""Build the weasis:// URI for the grading-api case that is current right now.

Single source of truth for two callers (issue #185), so the URI built at
session start and the URI built when the case changes can never drift apart:

  - custom_startup.sh        -> first study, passed to Weasis on launch
  - arrange_windows.sh       -> polls /api/case and re-sends the URI when the
                                case pointer moves (Weasis' weasis:// handling
                                is single-instance IPC: a second launch hands
                                the URI to the running instance)

Usage:
    weasis_case_uri.py uri CASE_JSON ORTHANC_URL    # prints the weasis:// URI
    weasis_case_uri.py uid CASE_JSON                # prints the study UID

Exit status (callers rely on it, tests pin it):
    0  success, OR nothing to open. "Nothing to open" prints nothing: the
       case is complete, the study UID is missing/empty, or the UID is not a
       valid DICOM UID. Callers must treat empty output as "keep what is
       shown now", never as "close the study".
    2  usage error
    3  CASE_JSON is not a JSON object (unreachable API, HTML error page, ...)

Why the URI is built this way is documented in custom_startup.sh's header
(Weasis ConfigData.splitArgToCmd, the dicom:rs command wrapped in weasis://).
The study UID goes straight into a command line, so only characters that cannot break out
of a quoted argument are accepted (letters, digits, . _ -, at most 64); anything else is
"nothing to open". See the comment at _UID_RE for why this is not a strict DICOM-UID check.
"""

import json
import re
import sys
import urllib.parse

# What this helper checks is COMMAND-LINE SAFETY, not DICOM conformance: the UID is put
# inside a quoted argument of a weasis:// command, so it may contain only characters that
# cannot break out of it (no quotes, spaces, &, =, +, %, slashes, shell characters).
# Letters, digits, dot, underscore and hyphen, 1-64 characters.
# Do NOT tighten this to "digits and dots": the radiologist's real studies carry
# non-conforming UIDs (14 characters, leading or trailing dot; issue #162). They are valid
# keys in Orthanc and used to load; a strict pattern made Weasis start without a study
# (found in a live test, issue #198). Conformance of the data is #162's business.
_UID_RE = re.compile(r"^[0-9A-Za-z._-]{1,64}$")


def parse_case(case_json: str) -> dict:
    """Return the case as a dict. Raises ValueError if it is not a JSON object."""
    try:
        case = json.loads(case_json)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not JSON: {exc}") from exc
    if not isinstance(case, dict):
        raise ValueError("not a JSON object")
    return case


def study_uid(case: dict) -> str:
    """The study UID to open, or "" when there is nothing to open."""
    if case.get("complete"):
        return ""
    uid = case.get("orthanc_study_uid")
    if not isinstance(uid, str) or not uid or len(uid) > 64 or not _UID_RE.fullmatch(uid):
        return ""
    return uid


def build_uri(uid: str, orthanc_url: str) -> str:
    """weasis:// URI that opens STUDY `uid` through Orthanc's DICOMweb endpoint."""
    dicomweb_url = orthanc_url.rstrip("/") + "/dicom-web"
    parts = [
        "$dicom:rs",
        "--url",
        f'"{dicomweb_url}"',
        "-r",
        f'"requestType=STUDY&studyUID={uid}"',
    ]
    return "weasis://?" + "+".join(urllib.parse.quote(p, safe="") for p in parts)


def main(argv: list) -> int:
    if len(argv) < 3 or argv[1] not in ("uri", "uid") or (argv[1] == "uri" and len(argv) != 4):
        print(__doc__.split("Usage:")[1].split("Exit status")[0].strip(), file=sys.stderr)
        return 2
    try:
        case = parse_case(argv[2])
    except ValueError as exc:
        print(f"weasis_case_uri: invalid case response ({exc})", file=sys.stderr)
        return 3
    uid = study_uid(case)
    if uid:
        print(build_uri(uid, argv[3]) if argv[1] == "uri" else uid)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
