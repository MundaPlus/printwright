#!/usr/bin/env bash
# Slice one STL with OrcaSlicer and the presets from printwright.json.
# Usage: tools/slice.sh STL/part.stl [--layer-height=0.16 ...extra Orca overrides]
# Output: gcode/<part>.gcode and gcode/<part>.json (print time estimate) in the workspace.
set -euo pipefail
APP="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
WS="$(python3 "$APP/lib/workspace.py" root)"
export PRINTWRIGHT_WORKSPACE="$WS"
PRESETS="$(python3 "$APP/lib/workspace.py" presets)"
STL="$(realpath "${1:?usage: tools/slice.sh STL/part.stl [Orca overrides]}")"; shift
NAME="$(basename "${STL%.*}")"
for f in printer.json process.json filament-1.json; do
  [ -f "$PRESETS/$f" ] || { echo "missing preset: $PRESETS/$f (see \"presets\" in printwright.json)" >&2; exit 1; }
done
mkdir -p "$WS/gcode"
OUT="$WS/gcode/$NAME"
rm -rf "$OUT" && mkdir -p "$OUT"

# Flatpak on desktops; Docker image on servers (tools/orca-docker.sh).
if command -v flatpak >/dev/null 2>&1 && flatpak info com.orcaslicer.OrcaSlicer >/dev/null 2>&1; then
  ORCA=(flatpak run --filesystem="$WS" --filesystem="$APP" --filesystem="$PRESETS" com.orcaslicer.OrcaSlicer)
else
  ORCA=("$APP/tools/orca-docker.sh")
fi
# Per-part settings (lib/part_settings.py): presets/parts/<part>.json in the workspace
# or in the presets folder. Its process keys go over process.json, "material" picks the
# filament preset, "placement" turns the STL. Without the file nothing changes.
PREP="$(python3 "$APP/lib/part_settings.py" prepare "$NAME" "$STL" "$OUT")"
eval "$PREP"  # sets PROCESS, FILAMENT, STL and ORIENT
# --allow-rotations=0 keeps the turn on the bed: with supports on, Orca's arrange
# turns a part by 90 degrees, and then the G-code no longer matches the bed view.
"${ORCA[@]}" "$STL" \
  --load-settings "$PROCESS;$PRESETS/printer.json" \
  --load-filaments "$FILAMENT" \
  --arrange 1 --allow-rotations=0 --orient "$ORIENT" --slice 0 "$@" \
  --outputdir "$OUT" >"$OUT/orca.log" 2>&1 || { tail -20 "$OUT/orca.log"; exit 1; }

mv "$OUT/plate_1.gcode" "$WS/gcode/$NAME.gcode"
rm -rf "$OUT"
grep -E '^; (printer_model|nozzle_diameter|filament_type|nozzle_temperature|nozzle_temperature_initial_layer|hot_plate_temp|layer_height|sparse_infill_pattern|sparse_infill_density|enable_support|support_type) =' \
  "$WS/gcode/$NAME.gcode" | sort -u
python3 "$APP/tools/printer.py" estimate "$WS/gcode/$NAME.gcode"
echo "gcode: $WS/gcode/$NAME.gcode"
