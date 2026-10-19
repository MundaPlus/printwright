#!/usr/bin/env bash
# Build, slice and estimate one part, then refresh its bed view.
# Usage: tools/make.sh <part> [extra Orca overrides, e.g. --layer-height=0.16]
# Run it in a workspace (or set PRINTWRIGHT_WORKSPACE).
# Needs src/<part>.py with models <part> (STEP+STL) and <part>_on_bed (bed view).
# Imported parts (web/importer.py) mark their kind near the top of the file:
#   "# printwright: mesh"   an STL edited with manifold3d: cadgen's Python gets the mesh libraries
#   "# printwright: gcode"  a G-code with text edits: no CAD, no slicing, only the edits and the estimate
set -euo pipefail
# APP is the real location of this script's repo. A workspace reaches it through
# the tools symlink, so resolve the link first.
APP="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
WS="$(python3 "$APP/lib/workspace.py" root)"
export PRINTWRIGHT_WORKSPACE="$WS"
PART="${1:?usage: tools/make.sh <part> [Orca overrides]}"; shift
[ -f "$WS/src/$PART.py" ] || { echo "no part file: $WS/src/$PART.py" >&2; exit 1; }
KIND="$(python3 "$APP/lib/workspace.py" kind "$WS/src/$PART.py")"
cd "$WS"
if [ "$KIND" = gcode ]; then
  python3 "src/$PART.py"
  python3 "$APP/tools/printer.py" estimate "gcode/$PART.gcode"
  echo "gcode: $WS/gcode/$PART.gcode"
  exit 0
fi
PY=(uvx --no-config --managed-python --python 3.13 --from cadgen==0.7.24)
if [ "$KIND" = mesh ]; then
  read -ra WITH <<<"$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import workspace; print(*workspace.MESH_WITH)' "$APP/lib")"
  PY+=("${WITH[@]}")
fi
PY+=(python)
# build [--force]: run the part file, show the important lines, stop when it fails.
# A failed build must not go on to slice an old STL.
build() {
  local rc=0 out
  out="$("${PY[@]}" "src/$PART.py" "$@" 2>&1)" || rc=$?
  grep -E "wrote|warning|error|Error|FAILED" <<<"$out" || true
  [ "$rc" -eq 0 ] || { echo "build failed: src/$PART.py (exit $rc)" >&2; exit 1; }
}
build
"$APP/tools/slice.sh" "STL/$PART.stl" "$@"
# Second run: the bed view label reads the new gcode/<part>.json. cadgen only sees
# source changes, so --force makes it rebuild the bed view with the new label.
build --force
echo "bed view: $WS/STEP/${PART}_on_bed.step"
