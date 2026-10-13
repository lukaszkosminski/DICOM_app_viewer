# Running the demo more than once

Status: 2026-10-13. Purpose: show the whole process (learning, assessment, test, review, reveal) to several
audiences in a row, without deleting anything a previous doctor answered.

## The idea

Starting over inside the app (`POST /reset`, the "Rozpocznij od początku" button) deletes the doctor's recorded
answers. Those answers feed a scientific publication, so it is **off by default** (`GRADING_RESET_ENABLED`, issue
#98). Instead, every run is a **new doctor**: a new student id (`DEMO_001`, `DEMO_002`, ...) that starts at the
learning stage with empty progress, in its own session. Earlier doctors' answers stay in the database.

## Before the demo

1. Stack up: `docker compose up -d --build` (Kasm flow), plus `docker compose -f docker-compose.yml -f docker-compose.guacamole.yml up -d`
   for the Guacamole flow. Check `curl -s localhost:8080/api/healthz` returns `{"status":"ok"}`.
2. Workspace images built from the current branch (`docker build -t ipcmc/dicom-viewer-weasis:mvp docker/kasm-workspace-weasis`;
   Guacamole: `docker build --build-context kasm=docker/kasm-workspace-weasis -t ipcmc/guacamole-weasis:poc docker/guacamole-weasis`).
3. Kasm only: a file with `KASM_API_KEY`, `KASM_API_KEY_SECRET`, `KASM_SERVER` and `KASM_IMAGE_ID` (KEY=VALUE lines), outside the repository.

## Each run: start the next doctor

```
python3 scripts/demo-next-doctor.py --flow guac --end-previous
python3 scripts/demo-next-doctor.py --flow kasm --kasm-env /path/to/kasm.env --insecure --end-previous
```

- `--end-previous` first ends the previous doctor's session of the same flow (container, Guacamole user, grading token, link
  file; Kasm session through its API). Leave it out to keep several doctors open at once.
- The session link is put on the clipboard and **never printed**. Paste it into a browser. For Guacamole it is also in a
  `0600` file under `local/session-links/` (the path is printed).
- `--student-id NAME` chooses the id yourself (letters, digits, `_ . -`, up to 64). Otherwise the next `DEMO_nnn` is used.
- `--insecure` is only for Kasm's self-signed certificate on a lab machine.
- Kasm Community Edition allows **5 concurrent sessions**. Do not build the demo around exceeding that; end old ones.

## Showing both flows side by side

Run the two commands one after the other, paste the Kasm link into one browser window and open the Guacamole link in
another. Each flow has its own doctor id, so their progress does not interfere.

## What each doctor sees

Learning (free text, then the reference report), assessment (category + modifier S, immediate verdict), test (no feedback),
then **"Podsumowanie odpowiedzi"** with their own answers, **"Zatwierdź i zakończ"**, and finally **"Wyniki końcowe"**
with the correct answers and the score. There is no restart button (see above).

## Showing the results

```
python3 scripts/export-results.py                  # demo doctors (student ids DEMO_*) from the running stack
python3 scripts/export-results.py --all            # every student
python3 scripts/export-results.py --db FILE        # a database file, for example a backup copy
```

It prints a summary and writes two files under `local/exports/<UTC time>/` (git-ignored, directory mode 0700, files 0600):
`attempts.csv` (one row per answer, opens in Excel or LibreOffice) and `summary.txt`. The summary shows:

1. **Completion:** doctors started, finished all stages, approved the final review.
2. **Per doctor:** assessment and test accuracy, median time per case, total time.
3. **Confusion matrix:** the true Lung-RADS category (rows) against the doctor's category (columns), so you can see which
   categories are mixed up (for example 4A answered as 4B). Modifier S agreement is reported separately because it is not part of the verdict.
4. **Per case:** how many answered, the share correct, median time, which shows the hard cases.

Notes: the export is read-only (it never changes the database) and contains only the pseudonymous student ids, no patient data.
`attempts.csv` also contains the answer key (ground truth), so share it with the coordinator and the researchers only. The free-text
answers of the learning stage are left out unless you add `--include-text`.

## After the demo

- Nothing is deleted automatically: the `DEMO_*` students and their answers remain in the grading database. They are test data;
  archiving and cleaning test students is part of issue #98 (archiving attempts), not a script here.
- End the last sessions: rerun with `--end-previous` once more and close the browser tabs, or use `scripts/teardown-guacamole-session.py`.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `cannot find the stack: is the container ipcmc-grading-api running?` | start the stack (`docker compose up -d`) |
| `missing configuration for Kasm: ...` | pass `--kasm-env FILE` or export the variables |
| `no clipboard tool found` | Guacamole: open the printed link file. Kasm: use `--write-link FILE` |
| Weasis window empty in a new session | rebuild the workspace image from the current branch (fix #198 must be in it) |
| `ending the previous Kasm session failed` | end it in the Kasm admin UI; the new session is created anyway on the next run |
