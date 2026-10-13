"""Startup configuration validation (issue #97).

Before this module, GRADING_COORDINATOR_KEY was read with a bare
`os.environ[...]` in main.py (missing -> naked KeyError traceback at
import time, no mention of what to do about it) and its VALUE was never
checked at all: an empty or one-character key imported fine, and
`secrets.compare_digest` against a known-empty secret means anyone who
guesses "empty string" can mint session tokens for any student_id.
GRADING_TOKEN_TTL_SECONDS had the same shape of problem one level down
(`int(os.environ.get(...))` -> naked ValueError on "8 godzin").

The compose files' `${VAR:?}` guards are NOT the fix being replaced
here: they don't cover a direct `docker run`, CI, or anyone flipping a
compose default to `${VAR:-}` -- this module is the last line of defense
that holds no matter how the process was started.

Every validation failure raises SystemExit with a message that names the
OFFENDING VARIABLE (and, for non-secret values like the TTL, its bad
input) and says how to fix it -- but NEVER echoes the secret's own
value, so the failure message itself can't become another log-leak
vector (issue #93's lesson applied to startup).
"""

import os

# Minimum LENGTH -- note this is a brute-force floor, not an entropy
# guarantee (`'k'*32` passes; the CR #111 nit is fair, but rejecting
# low-complexity-by-pattern keys server-side is security theater the
# moment any real `openssl rand` output clears it by 2x anyway). 32
# characters is where brute-forcing the mint-your-own-token endpoint
# stops being absurd. Deliberately NO dev/test exception: every
# environment that can import this app proves the same property, so a
# test suite can't silently ship a deployment that skipped it.
MIN_COORDINATOR_KEY_CHARS = 32

DEFAULT_TOKEN_TTL_SECONDS = 8 * 60 * 60

# CR #111 item 3: an unbounded TTL turns "leaked token" into "permanent
# impersonation". A session token maps to one training/exam sitting;
# 7 days gives a weekend-plus-break margin over the 8h design point
# while keeping any leaked token's blast radius bounded by a week.
MAX_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60

# Bad non-secret values get quoted into error messages (they're not
# secrets -- a TTL is); capped so a megabyte of garbage in the env can't
# itself bloat a startup log line.
_MAX_ECHOED_VALUE_CHARS = 60


class ConfigError(ValueError):
    """A startup-config problem, phrased for a human operator (names the
    variable, never a secret's value). _exit_on_config_error() turns this
    into a clean SystemExit at import time; tests call the pure
    validators directly and assert on this instead."""


def validate_coordinator_key(raw: str | None) -> str:
    if raw is None:
        raise ConfigError(
            "GRADING_COORDINATOR_KEY is not set. It must be a shared secret of at "
            f"least {MIN_COORDINATOR_KEY_CHARS} characters known only to the coordinator "
            "(generate one, e.g.: openssl rand -hex 32). The service refuses to start "
            "without it: without a real secret, anyone could call POST /session and "
            "mint a token for any student_id."
        )
    if not raw.strip():
        # 32 spaces would sail past a bare len() check; .env files pick
        # up exactly this kind of junk (CR #111 item 1).
        raise ConfigError(
            "GRADING_COORDINATOR_KEY is set but contains only whitespace. Generate a "
            "real secret, e.g.: openssl rand -hex 32."
        )
    if raw != raw.strip():
        # Rejected, not silently stripped: scripts/create-session.py
        # sends the key's exact bytes from ITS env -- if the server
        # quietly normalized while the client didn't, a trailing space
        # becomes a silent 401 mystery instead of a loudly fixed typo.
        raise ConfigError(
            "GRADING_COORDINATOR_KEY has leading/trailing whitespace (a common .env "
            "artifact). Remove it -- the key is matched byte-for-byte against what "
            "scripts/create-session.py sends."
        )
    if len(raw) < MIN_COORDINATOR_KEY_CHARS:
        # The LENGTH is reported, never the value (and never via a repr
        # that could leak surrounding quoting/whitespace oddities either).
        raise ConfigError(
            f"GRADING_COORDINATOR_KEY is too short ({len(raw)} character(s), minimum "
            f"{MIN_COORDINATOR_KEY_CHARS}). Generate one, e.g.: openssl rand -hex 32."
        )
    return raw


def validate_token_ttl_seconds(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_TOKEN_TTL_SECONDS
    try:
        ttl = int(raw)
    except ValueError:
        raise ConfigError(
            f"GRADING_TOKEN_TTL_SECONDS must be a whole number of seconds, got "
            f"{raw[:_MAX_ECHOED_VALUE_CHARS]!r} (e.g. 28800 for 8 hours)."
        ) from None
    if ttl <= 0:
        raise ConfigError(
            f"GRADING_TOKEN_TTL_SECONDS must be greater than 0, got {ttl} -- "
            f"every minted token would be born expired (default: {DEFAULT_TOKEN_TTL_SECONDS})."
        )
    if ttl > MAX_TOKEN_TTL_SECONDS:
        raise ConfigError(
            f"GRADING_TOKEN_TTL_SECONDS={ttl} exceeds the {MAX_TOKEN_TTL_SECONDS}-second "
            f"(7 day) ceiling -- session tokens must not outlive the longest plausible "
            f"sitting, or a leaked token stays valid indefinitely "
            f"(default: {DEFAULT_TOKEN_TTL_SECONDS})."
        )
    return ttl


def validate_reset_enabled(raw: str | None) -> bool:
    """GRADING_RESET_ENABLED: whether POST /reset (a doctor starting over) is allowed.

    Off by default: /reset deletes the doctor's recorded answers (issue #98), and those answers
    feed a scientific publication. It exists for local testing only, until retakes are modelled
    as archived, separate attempts. Accepts 1/true/yes/on and 0/false/no/off (any case)."""
    if raw is None or raw.strip() == "":
        return False
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(
        f"GRADING_RESET_ENABLED must be 1/true/yes/on or 0/false/no/off, got "
        f"{raw[:_MAX_ECHOED_VALUE_CHARS]!r} (default: off)."
    )


def _exit_on_config_error(exc: ConfigError) -> None:
    # SystemExit (not a bare raise) so `docker run`/uvicorn exits with a
    # clear one-line operator message instead of an import-time traceback
    # ending in a KeyError -- the failure mode this module exists to
    # replace. Goes to stderr: it is fatal, and stdout in this service is
    # reserved for structured JSON log lines (see logging_config.py).
    import sys

    print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
    raise SystemExit(2)


try:
    COORDINATOR_KEY = validate_coordinator_key(os.environ.get("GRADING_COORDINATOR_KEY"))
    TOKEN_TTL_SECONDS = validate_token_ttl_seconds(os.environ.get("GRADING_TOKEN_TTL_SECONDS"))
    RESET_ENABLED = validate_reset_enabled(os.environ.get("GRADING_RESET_ENABLED"))
except ConfigError as exc:
    _exit_on_config_error(exc)
