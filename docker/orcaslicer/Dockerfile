# OrcaSlicer 2.4.2 command line, headless, in Docker.
# Build: docker build -t orcaslicer-cli:2.4.2 docker/orcaslicer
# Run:   tools/orca-docker.sh <same arguments as the orca-slicer binary>
# The CLI slices without a display. Thus the image has no Xvfb.

# Stage 1: download the AppImage and unpack it.
# Docker has no FUSE. Thus we extract the AppImage and do not mount it.
FROM ubuntu:24.04 AS extract
ARG ORCA_VERSION=2.4.2
ADD --chmod=755 https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v${ORCA_VERSION}/OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCA_VERSION}.AppImage /tmp/orca.AppImage
WORKDIR /tmp
RUN ./orca.AppImage --appimage-extract >/dev/null \
 && mv squashfs-root /opt/orcaslicer \
 # Remove GUI-only data: printer help pages, web UI, sample models, translations.
 && rm -rf /opt/orcaslicer/resources/hms \
           /opt/orcaslicer/resources/web \
           /opt/orcaslicer/resources/handy_models \
           /opt/orcaslicer/resources/i18n

# Stage 2: runtime.
# Install only the shared libraries that bin/orca-slicer links to.
# The binary links to WebKitGTK and GTK3, so they must be present.
FROM ubuntu:24.04
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libwebkit2gtk-4.1-0 \
      libgstreamer-plugins-base1.0-0 \
      libsecret-1-0 \
      libopengl0 libgl1 libglx0 libegl1 libglu1-mesa \
      libwayland-egl1 libwayland-server0 \
      libsm6 libice6 \
 && rm -rf /var/lib/apt/lists/* /usr/share/doc /usr/share/icons/Adwaita
COPY --from=extract /opt/orcaslicer /opt/orcaslicer
# Orca writes its config to $HOME. /tmp is writable for any user ID.
ENV HOME=/tmp
ENTRYPOINT ["/opt/orcaslicer/AppRun"]
