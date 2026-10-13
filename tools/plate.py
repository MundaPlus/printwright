#!/usr/bin/env python3
"""Arrange several parts on one plate and slice them together with OrcaSlicer.

Usage: tools/plate.py <plate_name> <part>[:count] [<part>[:count] ...]
Example: tools/plate.py plate_desk clip:4 base:1

Writes:
  gcode/<plate_name>.gcode         the plate, ready to print
  gcode/<plate_name>.json          print time and grams (printer.py estimate)
  gcode/<plate_name>.layout.json   where each copy sits on the bed (for the preview)
Exit code 0 when all copies fit on one plate, 2 when they do not fit, 1 on errors.
Run it in a workspace (or set PRINTWRIGHT_WORKSPACE). All paths above are in the workspace.

Settings: OrcaSlicer's command line has no settings per object. Thus the plate uses
its own file presets/parts/<plate_name>.json (process keys and "material"), not the
files of its parts. Each part keeps its own placement (turn): its turned STL is used.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import os
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]  # the app repo: tools, presets
sys.path.insert(0, str(APP / "lib"))
import workspace  # noqa: E402  (lib/workspace.py)
import part_settings  # noqa: E402  (lib/part_settings.py)

WS = workspace.find_workspace()  # parts, STL and gcode
PRESETS = workspace.presets_dir(WS)
os.environ["PRINTWRIGHT_WORKSPACE"] = str(WS)  # for orca-docker.sh and printer.py
NAME_RE = re.compile(r"^[a-z0-9_]+$")
OBJ_RE = re.compile(r"^EXCLUDE_OBJECT_DEFINE NAME=(\S+) CENTER=([\d.,-]+) POLYGON=(\[.*\])\s*$")
MAX_COPIES = 50


def orca_cmd() -> list[str]:
    """Flatpak OrcaSlicer on desktops, the Docker image on servers (same as tools/slice.sh)."""
    flatpak = subprocess.run(["flatpak", "info", "com.orcaslicer.OrcaSlicer"], capture_output=True) \
        if shutil.which("flatpak") else None
    if flatpak and flatpak.returncode == 0:
        return ["flatpak", "run", f"--filesystem={WS}", f"--filesystem={APP}", f"--filesystem={PRESETS}",
                "com.orcaslicer.OrcaSlicer"]
    return [str(APP / "tools" / "orca-docker.sh")]


def parse_items(args: list[str]) -> dict[str, int]:
    items: dict[str, int] = {}
    for arg in args:
        part, _, count = arg.partition(":")
        n = int(count or 1)
        if not NAME_RE.match(part) or n < 0:
            raise SystemExit(f"bad item: {arg}")
        if not (WS / "STL" / f"{part}.stl").exists():
            raise SystemExit(f"no STL for {part}: run tools/make.sh {part} first")
        if n:
            items[part] = items.get(part, 0) + n
    total = sum(items.values())
    if not total or total > MAX_COPIES:
        raise SystemExit(f"give 1 to {MAX_COPIES} copies in total")
    return items


def objects(gcode: Path) -> list[dict]:
    """Copies on the plate, from Klipper's EXCLUDE_OBJECT_DEFINE lines."""
    found = []
    for line in gcode.open(errors="replace"):
        m = OBJ_RE.match(line)
        if m:
            cx, cy = (float(v) for v in m.group(2).split(","))
            poly = json.loads(m.group(3))
            xs, ys = [p[0] for p in poly], [p[1] for p in poly]
            found.append({"part": m.group(1).split(".stl")[0], "center": [cx, cy],
                          "footprint": [round(max(xs) - min(xs), 2), round(max(ys) - min(ys), 2)],
                          "polygon": poly})
        elif line.startswith("G1") and found:
            break  # the object list sits in the header
    return found


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 1
    name, items = argv[0], parse_items(argv[1:])
    if not NAME_RE.match(name):
        raise SystemExit(f"bad plate name: {name}")
    out = WS / "gcode" / f"{name}.work"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    try:
        stls = [str(part_settings.placed_stl(p, WS)) for p, n in items.items() for _ in range(n)]
        files = part_settings.slicer_files(name, out, WS)
    except part_settings.SettingsError as e:
        raise SystemExit(f"settings: {e}")
    cmd = orca_cmd() + stls + [
        "--load-settings", f"{files['PROCESS']};{PRESETS}/printer.json",
        "--load-filaments", files["FILAMENT"],
        "--arrange", "1", "--orient", files["ORIENT"], "--slice", "0", "--outputdir", str(out)]
    log = out / "orca.log"
    with log.open("w") as fh:
        rc = subprocess.run(cmd, cwd=WS, stdout=fh, stderr=subprocess.STDOUT).returncode
    plates = sorted(out.glob("plate_*.gcode"))
    layout = {"name": name, "items": items, "created": time.time(), "copies": sum(items.values()),
              "plates": len(plates), "objects": [], "fits": False, "error": None}
    first = out / "plate_1.gcode"
    if rc != 0 or not first.exists():
        layout["error"] = "OrcaSlicer failed: " + " | ".join(log.read_text(errors="replace").strip().splitlines()[-3:])
    else:
        layout["objects"] = objects(first)
        layout["fits"] = len(plates) == 1 and len(layout["objects"]) == layout["copies"]
        if not layout["fits"]:
            layout["error"] = (f"Only {len(layout['objects'])} of {layout['copies']} copies fit on the bed. "
                               "Remove some copies or make the parts smaller.")
    gcode = WS / "gcode" / f"{name}.gcode"
    gcode.unlink(missing_ok=True)
    (WS / "gcode" / f"{name}.json").unlink(missing_ok=True)
    if layout["fits"]:
        first.replace(gcode)
        est = subprocess.run([sys.executable, str(APP / "tools" / "printer.py"), "estimate", str(gcode)],
                             capture_output=True, text=True)
        print(est.stdout.strip())
    (WS / "gcode" / f"{name}.layout.json").write_text(json.dumps(layout, indent=1))
    shutil.rmtree(out, ignore_errors=True)
    print(f"plate {name}: {layout['copies']} copies, fits={layout['fits']}" +
          (f", {layout['error']}" if layout["error"] else ""))
    return 0 if layout["fits"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
