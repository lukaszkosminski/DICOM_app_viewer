#!/usr/bin/env bats
# weasis_case_uri.py (issue #185): the single place that turns a grading-api case into a
# weasis:// URI. Exit-status contract: 0 + output, 0 + nothing, 2 usage, 3 not JSON.
H="$BATS_TEST_DIRNAME/weasis_case_uri.py"
O="http://viewer:8043/"

@test "uri: builds the percent-encoded dicom:rs URI for a study" {
  run python3 "$H" uri '{"orthanc_study_uid":"1.2.840.1"}' "$O"
  [ "$status" -eq 0 ]
  [[ "$output" == weasis://\?* ]]
  decoded=$(python3 -c "import sys,urllib.parse; print(urllib.parse.unquote(sys.argv[1]))" "$output")
  [[ "$decoded" == *'requestType=STUDY&studyUID=1.2.840.1'* ]]
  [[ "$decoded" == *'http://viewer:8043/dicom-web'* ]]
}

@test "uid: prints the study UID" {
  run python3 "$H" uid '{"orthanc_study_uid":"1.2.840.1"}'
  [ "$status" -eq 0 ] && [ "$output" = "1.2.840.1" ]
}

@test "real-data UIDs (non-conforming: 14 characters, leading or trailing dot) are accepted (issue #198)" {
  for u in ".1234567890123" "1234567890123." "12345678901.34" "1..2" "ABC_def-1.2"; do
    run python3 "$H" uri "{\"orthanc_study_uid\":\"$u\"}" "$O"
    [ "$status" -eq 0 ]
    [ -n "$output" ]
    run python3 "$H" uid "{\"orthanc_study_uid\":\"$u\"}"
    [ "$output" = "$u" ]
  done
}

@test "a UID without any letter or digit (dots, hyphens, underscores only) is refused: it would become a path segment" {
  for u in "." ".." "..." "-" "_" "._-"; do
    run python3 "$H" uri "{\"orthanc_study_uid\":\"$u\"}" "$O"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
  done
  run python3 "$H" uid '{"orthanc_study_uid":".1"}'
  [ "$output" = ".1" ]
}

@test "nothing to open prints nothing and exits 0 (complete, missing, empty, wrong type, unsafe characters, too long)" {
  for j in '{"complete":true,"orthanc_study_uid":"1.2"}' '{}' '{"orthanc_study_uid":""}' '{"orthanc_study_uid":5}' \
           '{"orthanc_study_uid":"1.2;x"}' '{"orthanc_study_uid":"1 2"}' '{"orthanc_study_uid":"1\"2"}' \
           '{"orthanc_study_uid":"1&2=3"}' '{"orthanc_study_uid":"1+2%20"}' '{"orthanc_study_uid":"a/b"}' \
           "{\"orthanc_study_uid\":\"1.$(printf '2%.0s' {1..70})\"}"; do
    run python3 "$H" uri "$j" "$O"
    [ "$status" -eq 0 ]
    [ -z "$output" ]
  done
}

@test "a response that is not a JSON object exits 3" {
  for j in '<html>' '' '[1,2]' '"text"' 'null'; do
    run python3 "$H" uri "$j" "$O"
    [ "$status" -eq 3 ]
  done
}

@test "bad usage exits 2" {
  run python3 "$H"
  [ "$status" -eq 2 ]
  run python3 "$H" uri '{}'
  [ "$status" -eq 2 ]
  run python3 "$H" bogus '{}' "$O"
  [ "$status" -eq 2 ]
}
