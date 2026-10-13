#!/usr/bin/env python3
"""Write complete OrcaSlicer presets for your printer.

Starts from OrcaSlicer's built-in profiles (names in "orca" in printwright.json),
then applies the limits the printer's own Klipper config reports, so the slicer's
time estimate uses the real machine. It needs the text-to-cad plugin's
orca_presets.py (the gcode skill) and an OrcaSlicer install with its profiles.

  tools/make_presets.py                 read limits from the printer (Moonraker)
  tools/make_presets.py --offline       use the limits saved below (Neptune 4 Pro)
  tools/make_presets.py --out DIR       write here (default: <workspace>/presets)

Then set "presets": "presets" in printwright.json so the tools use the new files.
Find profile names with: python3 <orca_presets.py> --list machine --match "Neptune 4"
"""
import argparse
import glob
import json
import os
import pathlib
import subprocess
import sys
import urllib.request

APP = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "lib"))
import workspace  # noqa: E402  (lib/workspace.py)

# Elegoo Neptune 4 Pro, read from its printer.cfg. Used with --offline.
KLIPPER = {"max_velocity": 500.0, "max_accel": 5000.0, "max_z_velocity": 20.0,
           "max_z_accel": 100.0, "square_corner_velocity": 9.0}
EXTRUDER = {"max_extrude_only_velocity": 60.0, "max_extrude_only_accel": 5000.0}


def orca_presets_script():
    """The text-to-cad gcode skill's orca_presets.py. ORCA_PRESETS overrides the search."""
    if os.environ.get("ORCA_PRESETS"):
        return pathlib.Path(os.environ["ORCA_PRESETS"])
    pattern = str(pathlib.Path.home() / ".claude/plugins/cache/*/text-to-cad/*/skills/gcode/scripts/orca_presets.py")
    found = sorted(glob.glob(pattern))
    if not found:
        sys.exit("orca_presets.py not found. Install the text-to-cad plugin, or set ORCA_PRESETS=/path/to/orca_presets.py.")
    return pathlib.Path(found[-1])


def read_limits(host):
    url = host + "/printer/objects/query?configfile=settings"
    with urllib.request.urlopen(url, timeout=10) as r:
        s = json.load(r)["result"]["status"]["configfile"]["settings"]
    k = {key: float(s["printer"][key]) for key in KLIPPER}
    e = {key: float(s["extruder"][key]) for key in EXTRUDER}
    return k, e


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="use the saved Neptune 4 Pro limits")
    ap.add_argument("--out", help="output folder (default: <workspace>/presets)")
    args = ap.parse_args()
    ws = workspace.find_workspace()
    cfg = workspace.load_config(ws)
    out = pathlib.Path(args.out).expanduser() if args.out else ws / "presets"
    out.mkdir(parents=True, exist_ok=True)
    k, e = (KLIPPER, EXTRUDER) if args.offline else read_limits(os.environ.get("PRINTER_URL") or cfg["moonraker_url"])
    orca = cfg["orca"]
    subprocess.run([sys.executable, str(orca_presets_script()), "--printer", orca["printer"],
                    "--process", orca["process"], "--filament", orca["filament"], "--out", str(out)],
                   check=True, stdout=subprocess.DEVNULL)
    path = out / "printer.json"
    p = json.loads(path.read_text())

    def both(v):  # Orca keeps [normal, stealth] pairs
        return [f"{v:g}", f"{v:g}"]

    p.update({
        "machine_max_speed_x": both(k["max_velocity"]),
        "machine_max_speed_y": both(k["max_velocity"]),
        "machine_max_speed_z": both(k["max_z_velocity"]),
        "machine_max_speed_e": both(e["max_extrude_only_velocity"]),
        "machine_max_acceleration_x": both(k["max_accel"]),
        "machine_max_acceleration_y": both(k["max_accel"]),
        "machine_max_acceleration_z": both(k["max_z_accel"]),
        "machine_max_acceleration_e": both(e["max_extrude_only_accel"]),
        "machine_max_acceleration_extruding": both(k["max_accel"]),
        "machine_max_acceleration_retracting": both(k["max_accel"]),
        "machine_max_acceleration_travel": both(k["max_accel"]),
        "machine_max_jerk_x": both(k["square_corner_velocity"]),
        "machine_max_jerk_y": both(k["square_corner_velocity"]),
    })
    path.write_text(json.dumps(p, indent=4))
    print(f"presets written to {out} with printer limits:", k)
    try:
        rel = out.resolve().relative_to(ws)
    except ValueError:
        rel = out.resolve()
    print(f'Set "presets": "{rel}" in printwright.json to use them.')


if __name__ == "__main__":
    main()
