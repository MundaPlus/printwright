#!/usr/bin/env bash
# Build, slice and estimate one part, then refresh its bed view.
# Usage: tools/make.sh <part> [extra Orca overrides, e.g. --layer-height=0.16]
# Run it in a workspace (or set PRINTWRIGHT_WORKSPACE).
# Needs src/<part>.py with models <part> (STEP+STL) and <part>_on_bed (bed view).
set -euo pipefail
# APP is the real location of this script's repo. A workspace reaches it through
# the tools symlink, so resolve the link first.
APP="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
WS="$(python3 "$APP/lib/workspace.py" root)"
export PRINTWRIGHT_WORKSPACE="$WS"
PART="${1:?usage: tools/make.sh <part> [Orca overrides]}"; shift
[ -f "$WS/src/$PART.py" ] || { echo "no part file: $WS/src/$PART.py" >&2; exit 1; }
PY=(uvx --no-config --managed-python --python 3.13 --from cadgen==0.7.21 python)
cd "$WS"
"${PY[@]}" "src/$PART.py" 2>&1 | grep -E "wrote|error|Error" || true
"$APP/tools/slice.sh" "STL/$PART.stl" "$@"
# Second run: the bed view label reads the new gcode/<part>.json. cadgen only sees
# source changes, so --force makes it rebuild the bed view with the new label.
"${PY[@]}" "src/$PART.py" --force 2>&1 | grep -E "wrote|error|Error" || true
echo "bed view: $WS/STEP/${PART}_on_bed.step"
