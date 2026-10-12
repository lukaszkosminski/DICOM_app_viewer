FROM nginx:1.29-alpine

# Pulls current Alpine security patches at build time -- the base image tag
# itself only gets rebuilt on nginx's own release cadence, so OS packages in
# it can lag Alpine's own package repo by days/weeks (confirmed via trivy:
# even a freshly-pulled nginx:1.29-alpine still had a few packages one patch
# release behind). This closes that gap on every build instead of waiting
# on the next upstream image publish.
RUN apk update && apk upgrade --no-cache

# issue #51: run as nginx's own existing unprivileged account (uid 101)
# instead of root. Both listened ports (8080, 8043) are already >1024, so
# nothing here needs a privilege to bind -- the root assumption lived only
# in four defaults of the stock config, retargeted below (audit #51 also
# asks for read_only:true in compose, which forces the log/temp choices:
# see docker-compose.yml):
#   - `user nginx;` -- setuid-from-root; meaningless (warned about) once
#     the master itself is non-root, removed.
#   - pid /run/nginx.pid -- /run is root-owned; /tmp is world-writable.
#   - error_log/access_log files under /var/log/nginx -- on a read_only
#     root filesystem they'd need a tmpfs; /dev/stderr + /dev/stdout land
#     the exact same lines in `docker logs` (scripts/test-log-leak.sh
#     reads them there, not from the files) with one less mount.
# Writable-by-nginx paths that must STAY files: conf.d -- 20-envsubst-on-
# templates.sh renders *.template into it at every start -- and
# /var/cache/nginx, nginx's compiled-in client-body/proxy temp dir, where
# /api/ responses over the proxy buffer size are written.
RUN sed -i \
      -e '/^[[:space:]]*user[[:space:]]\+nginx;/d' \
      -e 's|^pid[[:space:]].*;|pid /tmp/nginx.pid;|' \
      -e 's|/var/log/nginx/error\.log|/dev/stderr|' \
      -e 's|/var/log/nginx/access\.log|/dev/stdout|' \
      /etc/nginx/nginx.conf \
  && chown -R nginx:nginx /etc/nginx/conf.d /var/cache/nginx /var/log/nginx

# .envsh files here are sourced by the base image's own entrypoint, before its
# 20-envsubst-on-templates.sh substitutes *.template files under
# /etc/nginx/templates/ into /etc/nginx/conf.d/ -- both are stock nginx:alpine
# conventions, not custom to this project.
COPY 15-compute-orthanc-auth.envsh /docker-entrypoint.d/
RUN chmod +x /docker-entrypoint.d/15-compute-orthanc-auth.envsh
COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY watermark.html /usr/share/nginx/html/watermark.html
# Issue #5: trimmed-down, panel-only page for the Weasis flow -- see its own
# top-of-file comment for how it differs from watermark.html.
COPY grading-panel.html /usr/share/nginx/html/grading-panel.html

# Both servers listen >1024; the master no longer needs root for anything
# (see the sed/chown block above). nginx's own image ships no USER line --
# "who runs this" is now an explicit, reviewable property of THIS image.
USER nginx:nginx

EXPOSE 8080
