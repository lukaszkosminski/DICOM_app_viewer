# Configuration surface (issue #91, DoD P22)

Single source of truth for every environment variable the stack reads.
`scripts/check-config-surface.py` (PR-2, same issue) gates CI on this table
staying in sync with `.env.example`, the compose files (`docker compose
config --variables`), Python sources (`ast`-scanned literal `os.environ` /
`os.getenv` keys) and, as warnings, conservative shell scans. This file is
maintained BY HAND on purpose: the classes below are intent, and a generated
table would silently erase that intent.

**klasa (class) semantics**

- `config-usera` — operator/user configuration; MUST have an entry in
  `.env.example` (the gate enforces this).
- `wstrzykiwana-per-sesja` — injected per session by the launchers
  (`create-session.py` / `custom_startup.sh` / Kasm); has no place in `.env`.
- `szew-testowy` — test seam; unset in production, tests override it.
- `wewnetrzna` — internal default tuned by the image; changing it is a
  code/deployment decision, not user config.

`wrazliwa` means the VALUE is a secret: never logged, never committed, never
echoed by an error message (issues #63/#93/#94 lineage). `walidacja` names the
enforcement site.

## 1. config-usera (lives in `.env` / operator environment)

| zmienna | domyslna | wymagana | wrazliwa | klasa | walidacja | czytana przez |
|---|---|---|---|---|---|---|
| `ORTHANC_PASSWORD` | none | yes | yes | config-usera | compose `:?` (fail loudly) | compose (both), `load-sample-studies.sh`, smoke tests (`ORTHANC_PASS` fallback) |
| `GRADING_COORDINATOR_KEY` | none | yes | yes | config-usera | `config.py`: >= 32 chars, leading/trailing whitespace rejected (#97) | compose (both), `app/config.py`, `create-session.py`, `provision-guacamole-session.py`, `rehearse-migration.py` |
| `GUACAMOLE_DB_PASSWORD` | none | only for the Guacamole overlay | yes | config-usera | compose `:?` | `docker-compose.guacamole.yml` |
| `BIND_ADDR` | `0.0.0.0` | no | no | config-usera | none (address literal) | `docker-compose.remote-host.yml` (publishes 8042/8080/8043) — see security note below |
| `GRADING_TOKEN_TTL_SECONDS` | `28800` (8 h) | no | no | config-usera | `config.py`: integer, `1..604800` (#97) | `app/config.py` |
| `GRADING_DB_PATH` | `/data/grading.db` | no | no | config-usera | none | `app/db.py`, `scripts/rehearse-migration.py` |
| `GRADING_CASES_FILE` | empty (placeholder cases) | no | path only -- the file it points at holds the answer key: keep it git-ignored, mode 0600 (issue #87) | config-usera | `cases_file.py`: name/field-only errors; startup refuses to boot on a malformed file (`SystemExit`, `db.py`) | compose (both), `app/db.py` -> `app/cases_file.py`, `build-cases-file.py` |
| `TLS_CA_BUNDLE` | empty (system trust) | no | no | config-usera | path used by `ssl` | `scripts/create-session.py` (#103/#143 docs split) |
| `GRADING_API_URL` | `http://localhost:8080` | no | no | config-usera | URL used directly | `create-session.py`, `provision-guacamole-session.py`, smoke/VR harnesses |
| `KASM_SERVER` | none | yes for the Kasm flow | no | config-usera | URL | `create-session.py` |
| `KASM_API_KEY` | none | yes for the Kasm flow | yes | config-usera | none | `create-session.py` |
| `KASM_API_KEY_SECRET` | none | yes for the Kasm flow | yes | config-usera | none | `create-session.py` |
| `KASM_IMAGE_ID` | none | yes for the Kasm flow | no | config-usera | Kasm image id lookup | `create-session.py` |
| `GUACAMOLE_URL` | `http://localhost:8090/guacamole/` | no | no | config-usera | URL | `provision-`/`teardown-guacamole-session.py` |
| `GUACAMOLE_ADMIN_USERNAME` | `guacadmin` | no | no | config-usera | none | `provision-`/`teardown-guacamole-session.py` |
| `GUACAMOLE_ADMIN_PASSWORD` | `guacadmin` | no | yes | config-usera | none — the DEFAULT IS A WEAK SECRET; PoC seam only, production must set it | `provision-`/`teardown-guacamole-session.py` |
| `GUACAMOLE_WEASIS_IMAGE` | `ipcmc/guacamole-weasis:poc` | no | no | config-usera | image name | `provision-guacamole-session.py`, `guacamole-iac.sh` |
| `GUACAMOLE_USER_VALID_HOURS` | `48` (matches the reaper's max-age) | no | no | config-operator | float > 0; sets the JDBC user's `valid-until` (day-grained, rounded UP -- see #191 source notes in the provisioner); reaper remains the hard limit | `provision-guacamole-session.py` |
| `DOCKER_NETWORK` | `dicom_app_viewer_ipcmc-internal` | no | no | config-usera | docker network name | `provision-guacamole-session.py` |

Security note on `BIND_ADDR` (`0.0.0.0` default): publishing `:8043`
(Orthanc DICOMWeb) on all interfaces is the open question tracked by #103 /
#146 / #122. Until the topology decision lands, treat `BIND_ADDR=127.0.0.1`
as the secure choice on shared hosts.

## 2. wstrzykiwana-per-sesja (launcher/Kasm environment; NEVER in `.env`)

| zmienna | domyslna | wymagana | wrazliwa | klasa | walidacja | czytana przez |
|---|---|---|---|---|---|---|
| `STUDENT_ID` | none | yes per session | pseudonym | wstrzykiwana-per-sesja | minted via API payload (server-side pattern #99) | `overlay.py`, `grading_panel_window.py`, `custom_startup.sh` |
| `SESSION_ID` | shell: `date +%s` fallback in test seams | yes per session | no | wstrzykiwana-per-sesja | server-side pattern (#99) | same |
| `GRADING_TOKEN` | none | yes per session | **yes** | wstrzykiwana-per-sesja | opaque `token_urlsafe`; header-only transport (#94) | `custom_startup.sh`, `grading_panel_window.py` |
| `VIEWER_URL` | none | yes per session | no | wstrzykiwana-per-sesja | URL | `custom_startup.sh`, `grading_panel_window.py` |
| `ORTHANC_URL` | `http://localhost:8042` | no | no | wstrzykiwana-per-sesja (per deployment) | URL | viewer pages (query param), loader scripts, smoke |

## 3. szew-testowy (unset in production)

| zmienna | domyslna | wrazliwa | klasa | walidacja | czytana przez |
|---|---|---|---|---|---|
| `OVERLAY_SCRIPT` | `/opt/watermark/overlay.py` | no | szew-testowy | path | `watchdog.sh` (BATS stub seam) |
| `WATCHDOG_LOG` | `/tmp/watermark-overlay.log` | no | szew-testowy | path | `watchdog.sh` |
| `WATCHDOG_LOG_MAX_BYTES` | `1048576` | no | szew-testowy | integer | `watchdog.sh` rotation (#66) |
| `WATCHDOG_LOG_KEEP_LINES` | `2000` | no | szew-testowy | integer | `watchdog.sh` rotation |
| `WATCHDOG_BACKOFF_MAX` | `5` | no | szew-testowy | integer; raising it weakens the watermark (#156) — CI keeps a test |
| `WATCHDOG_STABLE_RESET_SECONDS` | `60` | no | szew-testowy | integer | `watchdog.sh` backoff reset |
| `ARRANGE_POLL_INTERVAL_S` | `2` | no | szew-testowy | number | `arrange_windows.sh` |
| `ARRANGE_MAX_ITERATIONS` | `0` (run forever) | no | szew-testowy | integer | `arrange_windows.sh` (BATS bound) |
| `ARRANGE_PANEL_CMD` | `python3 /opt/grading-panel/grading_panel_window.py` | no | szew-testowy | command line | `arrange_windows.sh` |
| `ARRANGE_WEASIS_BIN` | `/opt/weasis/bin/Weasis` | no | szew-testowy | path | `arrange_windows.sh` |
| `ARRANGE_WEASIS_COOLDOWN_S` | `20` | no | szew-testowy | integer | `arrange_windows.sh` relaunch rate limit |
| `ARRANGE_WEASIS_URI` | empty | no | szew-testowy | URI | `arrange_windows.sh` study re-launch |
| `MAX_WEASIS_RELAUNCHES` | `5` | no | szew-testowy | integer | `arrange_windows.sh` (#151) |
| `API_RETRY_BACKOFF` | `2` | no | szew-testowy | number | `custom_startup.sh` readiness wait |
| `API_ATTEMPTS` | (script default) | no | szew-testowy | integer | `custom_startup.sh` |
| `SHAPE_RECT_BUDGET` | per overlay shape budget test | no | szew-testowy | integer | `scripts/test-overlay-shape.sh` |
| `LOGLEAK_PORT` | `18980` | no | szew-testowy | port | `scripts/test-log-leak.sh` |
| `WEASIS_BIN` | (image path) | no | szew-testowy | path | smoke/BATS seams |
| `BROWSER_BIN` | `epiphany` | no | szew-testowy | command | guacamole PoC launch seam |
| `NEEDS_JSON` | stdin when unset | no | szew-testowy (CI-only) | JSON | `scripts/ci-gate.py` |

## 4. wewnetrzna (image-internal defaults)

| zmienna | domyslna | wrazliwa | klasa | czytana przez |
|---|---|---|---|---|
| `WATERMARK_TILES_PER_SCREEN` | `4.4` | no | wewnetrzna | `overlay.py` (#150/#155 flicker budget) |
| `GRADING_PANEL_WIDTH` | `420` | no | wewnetrzna | `grading_panel_window.py`, `arrange_windows.sh` |
| `PICOM_CONF` | `/opt/watermark/picom.conf` | no | wewnetrzna | compositor supervision (#155) |
| `VNC_DISPLAY` / `VNC_GEOMETRY` | KasmVNC defaults | no | wewnetrzna | Kasm image internals |
| `VNC_PASSWORD` | Kasm-managed | yes | wewnetrzna | KasmVNC internal (never leaves the container) |
| `WEASIS_BASE_JSON_PATH` | image path | no | wewnetrzna | `patch-weasis-config.py` |

## 5. Explicitly NOT configuration

The shell scan of PR-2 treats these as scanner noise (shell locals,
test-internal temporaries), not config: `BASH_SOURCE`, `PORT`, `TOKEN`,
`CASE_CODE`, `CODE` variants, `DATA_DIR`, `DEMO_SERVER`, `SENTINEL_HEADER`,
`SENTINEL_QUERY`, `REVOKE_CODE`, `REVOKEN`, `RETRIEVE_URL`,
`PLAYWRIGHT_VERSION`, plus the usual `PATH`/`HOME`/`DISPLAY`/`TERM`/`IFS`.
Anything the scan flags that is NOT in this file is reported as a WARNING
(first PR-2 iteration); hard-fail applies only to `config-usera` drift
between this table, `.env.example` and `docker compose config --variables`.
