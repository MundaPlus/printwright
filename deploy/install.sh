#!/usr/bin/env bash
# Install or update the Printwright web app as a systemd user service.
# 1. Checks Claude Code, the text-to-cad plugin, uv and OrcaSlicer (tools/check-setup.sh).
#    It stops when one is missing. It never installs the plugin for you.
# 2. Creates ~/.config/printwright/env on the first run (then stops so you can check it).
# 3. Creates or repairs the workspace from PRINTWRIGHT_WORKSPACE (never overwrites files).
# 4. Builds the OrcaSlicer Docker image when no Flatpak OrcaSlicer is present.
# 5. Installs the Python deps, installs the systemd user unit and restarts it.
set -euo pipefail
APP="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
cd "$APP"

if ! tools/check-setup.sh; then
  echo "Install the missing programs above, then run deploy/install.sh again." >&2
  exit 1
fi

ENV="$HOME/.config/printwright/env"
if [ ! -f "$ENV" ]; then
  mkdir -p "$(dirname "$ENV")"
  sed "s|^PRINTWRIGHT_WORKSPACE=.*|PRINTWRIGHT_WORKSPACE=$HOME/printwright-workspace|" \
    deploy/printwright.env.example > "$ENV"
  chmod 600 "$ENV"
  echo "Wrote $ENV. Check PRINTWRIGHT_WORKSPACE and the port there, then run deploy/install.sh again."
  exit 0
fi

# Read the env file the way systemd does: plain KEY=VALUE lines.
WS="$(sed -n 's/^PRINTWRIGHT_WORKSPACE=//p' "$ENV" | tail -1)"
case "$WS" in
  /*) ;;
  *) echo "Set PRINTWRIGHT_WORKSPACE in $ENV to an absolute path." >&2; exit 1 ;;
esac
PRINTWRIGHT_SKIP_CHECK=1 tools/init-workspace.sh "$WS"

# check-setup.sh made sure that the Flatpak or Docker is present.
if ! { command -v flatpak >/dev/null 2>&1 && flatpak info com.orcaslicer.OrcaSlicer >/dev/null 2>&1; }; then
  if ! docker image inspect orcaslicer-cli:2.4.2 >/dev/null 2>&1; then
    docker build -t orcaslicer-cli:2.4.2 docker/orcaslicer
  fi
fi

uv sync -q --project web
UNIT="$HOME/.config/systemd/user/printwright.service"
mkdir -p "$(dirname "$UNIT")"
sed "s|@APP@|$APP|g" deploy/printwright.service > "$UNIT"
systemctl --user daemon-reload
systemctl --user enable -q printwright.service
systemctl --user restart printwright.service
sleep 3
systemctl --user is-active printwright.service
PORT="$(sed -n 's/^PRINT_WEB_PORT=//p' "$ENV" | tail -1)"
echo "Printwright runs on port ${PORT:-8420}. Logs: journalctl --user -u printwright -f"
