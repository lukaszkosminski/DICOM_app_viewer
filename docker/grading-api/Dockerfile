# Base image pinned by tag AND digest (issue #105): the tag says what it is, the digest
# makes the build repeatable. To update see docs/REPRODUCIBLE_BUILDS.md.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# No `apt-get upgrade` here (issue #105): it made every build depend on whatever
# Debian published that day, so two builds of the same commit could differ and a
# deployed image could not be reproduced. OS security fixes now arrive by bumping
# the pinned base image digest above (procedure: docs/REPRODUCIBLE_BUILDS.md), and
# the security scan (trivy) is what tells us when that is due.

WORKDIR /srv
# requirements.lock is generated from requirements.txt and pins EVERY package,
# including transitive ones, with SHA-256 hashes; --require-hashes makes pip
# refuse anything not in it (docs/REPRODUCIBLE_BUILDS.md says how to regenerate).
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY app ./app

# SQLite file lives here, on a named volume (see docker-compose.yml) so
# progress survives container restarts.
ENV GRADING_DB_PATH=/data/grading.db
VOLUME /data

EXPOSE 8000
# --no-access-log: uvicorn's own default access log is redundant with (and
# differently-formatted from) the structured "request" log line
# RequestIdMiddleware now emits (issue #72) -- and, until issue #63 moves
# the session token out of query strings, uvicorn's default format would
# log the full request path+query string (including ?token=...) at INFO
# level on every request. Disabling it closes that specific leak vector
# now, ahead of #63's own full fix.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
