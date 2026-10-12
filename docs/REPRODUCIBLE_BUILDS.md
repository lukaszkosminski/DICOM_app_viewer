# Reproducible builds

Status: issue #105 (supply chain and releases). Scope: what is pinned, how to update it, and what is still open.

## What is pinned

| Item | Where | How |
|---|---|---|
| Base images | the `FROM` line of every `docker/*/Dockerfile` | `name:tag@sha256:<digest>` |
| Orthanc image | `docker-compose.yml`, `docker-compose.remote-host.yml` | `name:tag@sha256:<digest>` (issue #50) |
| Weasis `.deb` | `docker/kasm-workspace-weasis/Dockerfile`, `docker/guacamole-weasis/Dockerfile` | version plus SHA-256, checked with `sha256sum -c` (issue #50) |
| Python packages of grading-api | `docker/grading-api/requirements.lock` | every package, including transitive ones, with SHA-256 hashes; installed with `pip --require-hashes` |

`scripts/lint.sh` (stage 1 of CI) fails when a `FROM` line has no digest, when `requirements.lock` does not match `requirements.txt`, when the grading-api Dockerfile loses `--require-hashes`, or when it runs `apt-get upgrade` again. `scripts/tests/test_reproducible_builds.py` repeats these checks in the unit-test stage.

## Why there is no `apt-get upgrade`

It made every build depend on what Debian published that day, so two builds of one commit could differ. OS security fixes now arrive by bumping the base image digest. The security scan (`scripts/security-scan.sh`, trivy) is what tells us a bump is due.
Not changed in this issue: `docker/viewer/Dockerfile` still runs `apk upgrade` (it is not part of the issue's scope; the same trade-off applies and the owner can decide separately).

## How to update a base image

1. Pick the new tag (or keep the tag and take the newer build of it).
2. Get the digest of the multi-arch index, not of one platform:
   `docker buildx imagetools inspect <name>:<tag>` and copy the `Digest:` line.
3. Put `<name>:<tag>@sha256:<digest>` on the `FROM` line.
4. Build the image (`./scripts/build-test.sh`), run the tests of that component, run `./scripts/security-scan.sh`.
5. Open a PR that says old digest, new digest, and why (a CVE, a needed feature).

## How to regenerate `requirements.lock`

Generate it for the Python version of the image (3.12), not the version on your laptop:

```
cd docker/grading-api
docker run --rm -v "$PWD":/w -w /w python:3.12-slim@sha256:<digest from the Dockerfile> sh -c \
  "pip install -q pip-tools==7.5.0 && pip-compile --quiet --generate-hashes --allow-unsafe \
   --strip-extras --output-file=requirements.lock requirements.txt && chown $(id -u):$(id -g) requirements.lock"
```

Then run `./scripts/lint.sh` and rebuild the image. Edit `requirements.txt` for direct dependencies only; never edit the lock by hand.
Test-only packages (`requirements-dev.txt`) are not in the lock on purpose: they never enter the shipped image.

## Known difference: two Kasm base images

The Chrome workspace image stays on `kasmweb/chrome:1.16.0` and the Weasis image uses `kasmweb/core-ubuntu-noble:1.19.0`. This is deliberate (see the comments in `docker/kasm-workspace/Dockerfile`); both are now pinned by digest.

## Still open in issue #105

- No git tags and no GitHub Releases: a deployed artefact cannot yet be tied to a verified release.
- No deployment by image digest (there is no registry; images are built locally).
- Optional: an automated bump tool (Renovate or Dependabot) for digests and the lock. Not adopted; it needs the owner's decision.
