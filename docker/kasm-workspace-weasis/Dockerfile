# Kasm workspace image for the Weasis-based viewer (issue #3, milestone
# "Weasis viewer migration" -- see that milestone for the full rationale:
# PM feedback wanted a familiar desktop-viewer feel, not Orthanc's web UI).
#
# This is a NEW, separate workspace image -- it does not replace
# docker/kasm-workspace/ (the Chrome-based one). Both can be registered in
# Kasm side by side while this is being validated.
#
# Base: Kasm's own maintained "core-ubuntu-noble" image (XFCE + KasmVNC,
# nothing else) -- deliberately NOT their "desktop" bundle (which layers on
# Chrome, Firefox, VS Code, GIMP, Zoom, Slack, etc.). None of that belongs
# in a locked-down exam kiosk; every extra app is extra attack surface.
# Tag 1.19.0 pinned to match the actual installed Kasm server version
# (confirmed available via `docker manifest inspect` before using it).
# Base image pinned by tag AND digest (issue #105): the tag says what it is, the digest
# makes the build repeatable. To update see docs/REPRODUCIBLE_BUILDS.md.
FROM kasmweb/core-ubuntu-noble:1.19.0@sha256:69730b0ee6a572635fad9f8cf3ecb3d40fb86e01000f964b2dab719c664f67f4

USER root

# Weasis's own .deb release. Pinned to a specific release, not "latest",
# matching how the Chrome-based image pins kasmweb/chrome:1.16.0.
# Integrity (issue #50): the .deb is verified against a pinned SHA-256
# before it is installed. Provenance of the hash: the release publishes no
# separate checksum/signature file, so the value was taken from the digest
# GitHub records for the release asset (`gh api repos/nroduit/Weasis/
# releases/tags/v<ver>` -> assets[].digest) AND re-computed on a fresh
# download; both matched. That is integrity-against-GitHub's-record, not a
# vendor signature (upstream signing is still absent). Bumping
# WEASIS_VERSION REQUIRES bumping WEASIS_SHA256 -- a stale hash fails the
# build on purpose (sha256sum -c), it can never silently install an
# unverified file. Keep in sync with docker/guacamole-weasis/Dockerfile (scripts/lint.sh fails if the two pins differ).
ARG WEASIS_VERSION=4.7.3
ARG WEASIS_SHA256=b796c661cc513fcd8091f0c2385bad6a272489d88ed6fcdf43a8df6bd42024c4
RUN apt-get update && \
    curl -fsSL -o /tmp/weasis.deb \
      "https://github.com/nroduit/Weasis/releases/download/v${WEASIS_VERSION}/weasis_${WEASIS_VERSION}-1_amd64.deb" && \
    echo "${WEASIS_SHA256}  /tmp/weasis.deb" | sha256sum -c - && \
    apt-get install -y /tmp/weasis.deb && \
    rm -f /tmp/weasis.deb && \
    rm -rf /var/lib/apt/lists/*

# Patches Weasis's own base.json: the disclaimer dialog (issue #4) and the
# native export/import/send/Q-R lockdown (issue #7). See
# patch-weasis-config.py's own header comment for the full rationale
# behind every key it touches -- not repeated here to avoid the two
# drifting apart.
COPY patch-weasis-config.py /tmp/patch-weasis-config.py
RUN python3 /tmp/patch-weasis-config.py && rm /tmp/patch-weasis-config.py

# Same REMOTE_HOST/VIEWER_URL/ORTHANC_URL convention as
# docker/kasm-workspace/Dockerfile -- see that file's own comments for why
# the defaults are what they are (container-name DNS on the shared
# kasm_default_network; :8043 not Orthanc's own :8042, since that's the
# auth-injecting proxy -- Weasis is never taught Orthanc's real credentials).
ARG REMOTE_HOST=ipcmc-viewer
ENV VIEWER_URL="http://${REMOTE_HOST}:8080/"
ENV ORTHANC_URL="http://${REMOTE_HOST}:8043/"

# Issue #6 (Watermark Overlay): python3-gi and python3-cairo are already
# part of this XFCE base image (it's a GTK desktop), but the actual GI<->
# Cairo bridge PyGObject needs to hand a native Cairo context to a "draw"
# signal handler ships in a separate package -- confirmed the hard way
# ("Couldn't find foreign struct converter for 'cairo.Context'") before
# adding this.
#
# gir1.2-webkit2-4.1 is for grading_panel_window.py (the grading-panel
# display, a real gap found by the first real click-through test -- see
# that file's own header comment for why this is a locked-down WebKit2GTK
# embed, not a general-purpose browser install). libwebkit2gtk-4.1-0
# itself is already present in this base image as a transitive dependency
# of something else (confirmed via `apt-cache policy` before assuming an
# extra package was needed) -- only the GObject-Introspection bindings
# package is actually missing.
#
# picom (issue #150): a small standalone compositor, so the watermark overlay
# can be a genuinely transparent ARGB window instead of a window cut into a
# ~1,700-rectangle X shape that made the DICOM view flash on every re-cut.
# Measured in live sessions: 7 flashes/3 min (shape, 1 s), 3 (shape, 15 s),
# 0 (picom). ~0.5 MB package, one extra process per session. xfwm4's own
# compositor does not work on this KasmVNC X server; picom does.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      python3-gi-cairo \
      gir1.2-webkit2-4.1 \
      picom && \
    rm -rf /var/lib/apt/lists/*

COPY overlay.py watchdog.sh picom.conf /opt/watermark/
RUN chmod +x /opt/watermark/watchdog.sh

COPY grading_panel_window.py arrange_windows.sh weasis_case_uri.py /opt/grading-panel/
RUN chmod +x /opt/grading-panel/arrange_windows.sh

COPY custom_startup.sh /dockerstartup/custom_startup.sh
RUN chmod +x /dockerstartup/custom_startup.sh

# Desktop lockdown: this base image's default session (vnc_startup.sh's
# start_window_manager(), START_XFCE4=1) is a FULL XFCE desktop -- panel,
# desktop-icon right-click menu, file manager, and two separate terminal
# emulators are all present and reachable despite this Dockerfile's own
# header comment above already trying to avoid exactly that ("nothing
# else... every extra app is extra attack surface"). Confirmed via
# `dpkg -l` against this exact base image, not assumed.
#
# Fix is package removal, not switching window managers: `openbox` isn't
# even installed here, and arrange_windows.sh's wmctrl calls above are
# tuned specifically against xfwm4's real behavior (e.g. the maximized-
# window quirk documented in that file) -- swapping window managers would
# risk silently breaking already-tested window placement. xfwm4 and
# xfce4-session are deliberately NOT in this removal list.
#
# Verified in an isolated throwaway container (not assumed from package
# names alone): after this exact removal, `startxfce4` still launches,
# `wmctrl -m` still reports Xfwm4 as the active window manager (matching
# arrange_windows.sh's own wmctrl dependency), a real app window still
# opens/gets managed normally, and `which xterm xfce4-terminal
# gnome-terminal konsole` finds nothing anywhere on PATH. xfce4-panel and
# xfdesktop4 are what register a client-list with the WM in the first
# place -- with both gone, there is no panel, no taskbar, and no
# desktop-icon right-click menu at all, not merely a hidden one.
#
# Deliberately NOT followed by `apt-get autoremove`: tested that too, and
# it silently takes xfwm4 and xfce4-session down with it (they end up
# looking like orphaned dependencies of the now-gone xfce4 metapackage) --
# `startxfce4` stops existing at all. Caught only by actually running
# autoremove for real and checking `dpkg -l` afterwards; a `--dry-run`
# pass alone is not enough evidence here since its output list is long
# enough to hide exactly these two entries if it gets truncated.
RUN apt-get update && \
    apt-get remove -y --purge \
      xfce4-panel \
      xfdesktop4 \
      thunar \
      thunar-volman \
      xfce4-terminal \
      xterm \
      xfce4-appfinder \
      xfce4 \
      xorg && \
    rm -rf /var/lib/apt/lists/*

# Baked-in kasmvnc.yaml override (see that file's own comment for the real
# clipboard-DLP gap this closes) -- placed at the exact path
# vnc_startup.sh only creates a default for if nothing exists there yet,
# so this is used as-is rather than overwritten. --chown matches how this
# file is normally owned when vnc_startup.sh creates it itself.
COPY --chown=kasm-user:kasm-user kasmvnc.yaml /home/kasm-user/.vnc/kasmvnc.yaml

USER kasm-user
