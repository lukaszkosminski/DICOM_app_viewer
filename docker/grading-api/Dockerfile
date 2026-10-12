FROM python:3.12-slim

# Picks up Debian security patches released after this base image tag was
# last rebuilt upstream (e.g. a fixed openssl/libssl3t64 CVE) -- without
# this, the image's OS packages are only ever as current as whenever
# python:3.12-slim itself last happened to be rebuilt, not as current as
# what's actually available in Debian's own repos at build time.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# issue #51: run as a dedicated non-root user. Neither a container-escape
# bug nor a compromised app process should ever hand out root inside the
# namespace it sits in -- the app needs no privilege at all: uvicorn binds
# 8000 (>1024, no NET_BIND_SERVICE needed), pip installs happen at BUILD
# time as root above, and every runtime write goes to /data (the named
# volume) or /tmp. Fixed UID (not "whatever uid 10001 the adduser defaults
# pick") so the grading-db volume's ownership is reviewable from the
# Dockerfile alone and stable across rebuilds.
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin grading

# SQLite file lives here, on a named volume (see docker-compose.yml) so
# progress survives container restarts.
ENV GRADING_DB_PATH=/data/grading.db
# /data is pre-created and chowned deliberately: docker-compose.yml mounts
# the NAMED grading-db volume here, and on FIRST creation docker seeds an
# empty named volume from the image path it overlays -- ownership included
# -- so the fresh volume arrives writable by 10001 with no init hack or
# entrypoint chown. (An UPGRADE keeps a pre-#51, root-owned volume: see the
# one-time chown note in docker-compose.yml.)
RUN mkdir -p /data && chown -R grading /srv /data
VOLUME /data

# Numeric UID:GID (same value, named at runtime for ps readability) --
# also what Kubernetes' runAsNonRoot check accepts without a matching
# /etc/passwd entry, should this ever move off compose.
USER 10001:10001

EXPOSE 8000
# --no-access-log: uvicorn's own default access log is redundant with (and
# differently-formatted from) the structured "request" log line
# RequestIdMiddleware now emits (issue #72) -- and, until issue #63 moves
# the session token out of query strings, uvicorn's default format would
# log the full request path+query string (including ?token=...) at INFO
# level on every request. Disabling it closes that specific leak vector
# now, ahead of #63's own full fix.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
