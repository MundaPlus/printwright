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

Layout: "arrange" in the plate's file (see lib/part_settings.py).
  auto  OrcaSlicer packs the copies tight. "turn" lets it turn copies around Z.
  grid  this script puts the copies in rows, "spacing" mm apart, at "align"
        (center or a corner), and OrcaSlicer slices them where they are.
        "turn" turns all copies 90 degrees when that fits better.
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
EDGE = 2.0  # mm from a copy (or its brim) to the edge of the bed


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


def grid(sizes: list[tuple[float, float]], bed: tuple[float, float], gap: float, align: str,
         edge: float) -> tuple[list[tuple[float, float]], float] | None:
    """Rows of copies, deepest first. sizes: X and Y of each copy.

    Returns the centre of each copy and the area of the block, or None when they do not fit.
    """
    room_x, room_y = bed[0] - 2 * edge, bed[1] - 2 * edge
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i][1])
    rows: list[list[int]] = [[]]
    widths = [0.0]
    for i in order:
        w = sizes[i][0]
        need = widths[-1] + (gap if rows[-1] else 0) + w
        if rows[-1] and need > room_x + 1e-6:
            rows.append([])
            widths.append(0.0)
            need = w
        if need > room_x + 1e-6:
            return None
        rows[-1].append(i)
        widths[-1] = need
    depths = [max(sizes[i][1] for i in r) for r in rows]
    block_x, block_y = max(widths), sum(depths) + gap * (len(rows) - 1)
    if block_y > room_y + 1e-6:
        return None
    side = align.split("_")[-1]  # center, left or right
    x0 = {"left": edge, "right": bed[0] - edge - block_x}.get(side, (bed[0] - block_x) / 2)
    y0 = {"front": edge, "back": bed[1] - edge - block_y}.get(align.split("_")[0], (bed[1] - block_y) / 2)
    centers: list[tuple[float, float]] = [(0.0, 0.0)] * len(sizes)
    y = y0
    for r, w_row, d_row in zip(rows, widths, depths):
        x = x0 + {"left": 0, "right": block_x - w_row}.get(side, (block_x - w_row) / 2)
        for i in r:
            w, d = sizes[i]
            centers[i] = (round(x + w / 2, 3), round(y + d / 2, 3))
            x += w + gap
        y += d_row + gap
    return centers, block_x * block_y


def place_grid(name: str, items: dict[str, int], ar: dict, out: Path) -> tuple[list[str] | None, str | None]:
    """Write one STL per copy at its place in the grid. Returns the STLs, or None and the reason."""
    bed = workspace.load_config(WS)["bed"]
    values = part_settings.effective(name, "plate", WS)["values"]
    brim = float(values.get("brim_width") or 0) if values.get("brim_type") != "no_brim" else 0.0
    edge = EDGE + brim
    copies = [(p, part_settings.placed_stl(p, WS)) for p, n in items.items() for _ in range(n)]
    size = {p: part_settings.stl_size(stl)[:2] for p, stl in set(copies)}
    best = None
    for turn in ((0, 90) if ar["turn"] else (0,)):
        sizes = [tuple(size[p][::-1]) if turn else tuple(size[p]) for p, _ in copies]
        found = grid(sizes, (bed[0], bed[1]), ar["spacing"], ar["align"], edge)
        if found and (best is None or found[1] < best[1] - 1e-6):
            best = (found[0], found[1], turn)
    if best is None:
        return None, (f"The copies do not fit on the bed in a grid {ar['spacing']:g} mm apart. "
                      "Use less spacing, fewer copies, turn on 'Turn copies to fit', or use the auto layout.")
    centers, _, turn = best
    stls = []
    for i, ((part, src), c) in enumerate(zip(copies, centers)):
        dst = out / "copies" / str(i) / f"{part}.stl"  # the file name names the object in the G-code
        part_settings.transform_stl(src, dst, [0, 0, turn], center=c)
        stls.append(str(dst))
    return stls, None


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
        ar = part_settings.arrange(name, WS)
        files = part_settings.slicer_files(name, out, WS)
        if ar["mode"] == "grid":
            stls, why = place_grid(name, items, ar, out)
            placing = ["--arrange", "0"]
        else:
            stls = [str(part_settings.placed_stl(p, WS)) for p, n in items.items() for _ in range(n)]
            why, placing = None, ["--arrange", "1"] + (["--allow-rotations"] if ar["turn"] else [])
    except part_settings.SettingsError as e:
        raise SystemExit(f"settings: {e}")
    layout = {"name": name, "items": items, "created": time.time(), "copies": sum(items.values()),
              "arrange": ar, "plates": 0, "objects": [], "fits": False, "error": why}
    rc, first = 1, out / "plate_1.gcode"
    if stls:
        cmd = orca_cmd() + stls + [
            "--load-settings", f"{files['PROCESS']};{PRESETS}/printer.json",
            "--load-filaments", files["FILAMENT"],
            *placing, "--orient", files["ORIENT"], "--slice", "0", "--outputdir", str(out)]
        log = out / "orca.log"
        with log.open("w") as fh:
            rc = subprocess.run(cmd, cwd=WS, stdout=fh, stderr=subprocess.STDOUT).returncode
        layout["plates"] = len(list(out.glob("plate_*.gcode")))
    if why:  # the grid does not fit: nothing was sliced
        layout["objects"] = []
    elif rc != 0 or not first.exists():
        layout["error"] = "OrcaSlicer failed: " + " | ".join(log.read_text(errors="replace").strip().splitlines()[-3:])
    else:
        layout["objects"] = objects(first)
        layout["fits"] = layout["plates"] == 1 and len(layout["objects"]) == layout["copies"]
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
