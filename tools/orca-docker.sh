#!/usr/bin/env bash
# Run the OrcaSlicer command line in Docker. It replaces the Flatpak OrcaSlicer.
# Usage: tools/orca-docker.sh <the same arguments as the orca-slicer binary>
# Build the image first: docker build -t orcaslicer-cli:2.4.2 docker/orcaslicer
set -euo pipefail

# The app root is the parent of tools/ (after the workspace symlink is resolved).
APP="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
WS="$(python3 "$APP/lib/workspace.py" root)"
PRESETS="$(python3 "$APP/lib/workspace.py" presets)"

# Set ORCA_IMAGE to use a different image tag.
IMAGE="${ORCA_IMAGE:-orcaslicer-cli:2.4.2}"

# Mount the app, the workspace and the presets at the same paths in the container.
# Thus absolute paths work unchanged. Skip a folder that an earlier mount holds:
# Docker refuses the same mount point twice.
MOUNTS=()
DONE=()
for d in "$APP" "$WS" "$PRESETS"; do
  d="$(realpath "$d")"
  for m in "${DONE[@]+"${DONE[@]}"}"; do
    case "$d/" in "$m/"*) continue 2 ;; esac
  done
  DONE+=("$d")
  MOUNTS+=(-v "$d:$d")
done

# Run as the calling user. Thus the output files belong to you, not to root.
# HOME=/tmp gives Orca a writable config dir. The container is removed after the run.
exec docker run --rm \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -v /etc/localtime:/etc/localtime:ro \
  "${MOUNTS[@]}" \
  -w "$PWD" \
  "$IMAGE" "$@"
