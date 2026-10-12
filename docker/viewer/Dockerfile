# Base image pinned by tag AND digest (issue #105): the tag says what it is, the digest
# makes the build repeatable. To update see docs/REPRODUCIBLE_BUILDS.md.
FROM nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de

# Pulls current Alpine security patches at build time -- the base image tag
# itself only gets rebuilt on nginx's own release cadence, so OS packages in
# it can lag Alpine's own package repo by days/weeks (confirmed via trivy:
# even a freshly-pulled nginx:1.29-alpine still had a few packages one patch
# release behind). This closes that gap on every build instead of waiting
# on the next upstream image publish.
RUN apk update && apk upgrade --no-cache

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

EXPOSE 8080
