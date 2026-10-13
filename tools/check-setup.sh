#!/usr/bin/env bash
# Check the programs that Printwright needs. It installs nothing.
# Usage: tools/check-setup.sh
# Exit code 0 when all required programs are present, 1 when one is missing.
# deploy/install.sh stops on exit code 1. tools/init-workspace.sh only warns.
set -uo pipefail
APP="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
IMAGE="${ORCA_IMAGE:-orcaslicer-cli:2.4.2}"
MISSING=0

ok() { echo "  ok       $*"; }
missing() { echo "  MISSING  $1" >&2; shift; for l in "$@"; do echo "           $l" >&2; done; MISSING=1; }
note() { echo "  note     $1"; shift; for l in "$@"; do echo "           $l"; done; }

echo "Checking the setup:"

# Claude Code runs the chat and the Ideas window.
if command -v claude >/dev/null 2>&1; then
  ok "Claude Code ($(command -v claude))"
  # The text-to-cad plugin by earthtojake writes and builds the CAD models.
  # Read the whole list first: "grep -q" in a pipe can end the pipe early.
  PLUGINS="$(claude plugin list 2>/dev/null || true)"
  if grep -q 'text-to-cad@' <<<"$PLUGINS"; then
    ok "text-to-cad plugin"
  else
    missing "the text-to-cad Claude Code plugin (https://github.com/earthtojake/text-to-cad). Install it:" \
      "claude plugin marketplace add earthtojake/text-to-cad" \
      "claude plugin install text-to-cad@earthtojake"
  fi
else
  missing "Claude Code (the 'claude' command). Install it from https://claude.com/claude-code and log in." \
    "Then install the text-to-cad plugin:" \
    "claude plugin marketplace add earthtojake/text-to-cad" \
    "claude plugin install text-to-cad@earthtojake"
fi

# uv runs cadgen (Python 3.13) and the web app.
if command -v uv >/dev/null 2>&1; then
  ok "uv"
else
  missing "uv. Install it from https://docs.astral.sh/uv/ (curl -LsSf https://astral.sh/uv/install.sh | sh)."
fi

# OrcaSlicer: the Flatpak on a desktop, or the Docker image on a server.
if command -v flatpak >/dev/null 2>&1 && flatpak info com.orcaslicer.OrcaSlicer >/dev/null 2>&1; then
  ok "OrcaSlicer (Flatpak com.orcaslicer.OrcaSlicer)"
elif command -v docker >/dev/null 2>&1; then
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    ok "OrcaSlicer (Docker image $IMAGE)"
  else
    note "OrcaSlicer: Docker is present, but the image $IMAGE is not built yet." \
      "deploy/install.sh builds it. Or build it yourself:" \
      "docker build -t $IMAGE $APP/docker/orcaslicer"
  fi
else
  missing "OrcaSlicer. Install the Flatpak, or install Docker and build the image:" \
    "flatpak install flathub com.orcaslicer.OrcaSlicer" \
    "docker build -t $IMAGE $APP/docker/orcaslicer"
fi

# The tools run on the system Python (3.10 or newer) and use git.
if python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  ok "python3 $(python3 -c 'import platform; print(platform.python_version())')"
else
  missing "python3 3.10 or newer."
fi
if command -v git >/dev/null 2>&1; then ok "git"; else missing "git."; fi

exit "$MISSING"
