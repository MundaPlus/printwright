#!/usr/bin/env python3
"""Write complete OrcaSlicer filament presets for more materials (PETG, TPU).

The settings panel and presets/parts/<part>.json choose a material by name.
"PETG" uses presets/filaments/petg.json. PLA is filament-1.json, the default preset.

Each preset starts from OrcaSlicer's own vendor-neutral Generic profile, through the
text-to-cad plugin's orca_presets.py. Then this tool sets the values in MATERIALS:

  PETG  240 C nozzle, 80 C bed. Generic PETG says 255/70; 240 C strings less on a
        0.4 mm nozzle, and 80 C holds PETG on a PEI sheet.
  TPU   225 C nozzle, 50 C bed. Generic TPU says 240/35. Its volumetric limit of
        3.2 mm3/s keeps the print slow, as flexible filament needs.

It also empties the filament start G-code: the Generic profiles switch an exhaust fan
with "M106 P3", and on a plain Klipper printer that command drives the part fan.
The presets name no printer, so OrcaSlicer accepts them with any printer preset.

  tools/make_filaments.py                    write the app's presets/filaments/
  tools/make_filaments.py --out DIR          write here instead, e.g. <workspace>/presets/filaments
  tools/make_filaments.py --material PETG    only this material (give it more than once)

Check the temperatures against your filament's label before you print.
"""
import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile

APP = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "tools"))
from make_presets import orca_presets_script  # noqa: E402  (tools/make_presets.py)

FLATPAK_PROFILES = "share/OrcaSlicer/profiles"

# material: (OrcaSlicer filament preset, values to set)
MATERIALS = {
    "PETG": ("Generic PETG", {
        "nozzle_temperature": "240", "nozzle_temperature_initial_layer": "240",
        "bed": "80",
    }),
    "TPU": ("Generic TPU", {
        "nozzle_temperature": "225", "nozzle_temperature_initial_layer": "225",
        "bed": "50",
    }),
}
BED_KEYS = ["hot_plate_temp", "cool_plate_temp", "eng_plate_temp", "textured_plate_temp",
            "supertack_plate_temp"]


def profile_folders():
    """OrcaSlicer's profile folders: the Flatpak's resources, else ORCA_PROFILES."""
    found = []
    if os.environ.get("ORCA_PROFILES"):
        found.append(os.environ["ORCA_PROFILES"])
    try:
        loc = subprocess.run(["flatpak", "info", "--show-location", "com.orcaslicer.OrcaSlicer"],
                             capture_output=True, text=True, check=True).stdout.strip()
        if (pathlib.Path(loc) / "files" / FLATPAK_PROFILES).is_dir():
            found.append(str(pathlib.Path(loc) / "files" / FLATPAK_PROFILES))
    except (OSError, subprocess.CalledProcessError):
        pass
    return found


def build(material, source, values, presets, out):
    """Make one complete filament preset and write <out>/<material>.json."""
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [sys.executable, str(orca_presets_script()),
               "--printer", str(presets / "printer.json"), "--process", str(presets / "process.json"),
               "--filament", source, "--out", tmp]
        for folder in profile_folders():
            cmd += ["--profiles", folder]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        f = json.loads((pathlib.Path(tmp) / "filament-1.json").read_text())
    for key in ("nozzle_temperature", "nozzle_temperature_initial_layer"):
        f[key] = [values[key]]
    for key in BED_KEYS:
        for k in (key, key + "_initial_layer"):
            if k in f:  # set only the plate types this OrcaSlicer knows
                f[k] = [values["bed"]]
    f.update({
        "name": f"Printwright {material}",
        "filament_settings_id": [f"Printwright {material}"],
        "filament_type": [material],
        "filament_start_gcode": ["; filament start gcode\n"],
        "compatible_printers": [],
        "compatible_printers_condition": "",
    })
    path = out / f"{material.lower()}.json"
    path.write_text(json.dumps(f, indent=1) + "\n")
    print(f"{material}: {path} from \"{source}\" | nozzle {values['nozzle_temperature']} C, bed {values['bed']} C, "
          f"max {f.get('filament_max_volumetric_speed', ['?'])[0]} mm3/s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="output folder (default: the app's presets/filaments)")
    ap.add_argument("--material", action="append", choices=sorted(MATERIALS), help="only this material")
    args = ap.parse_args()
    # The app's default presets name the printer and the process for orca_presets.py.
    presets = APP / "presets"
    out = pathlib.Path(args.out).expanduser() if args.out else APP / "presets" / "filaments"
    out.mkdir(parents=True, exist_ok=True)
    for material in args.material or sorted(MATERIALS):
        source, values = MATERIALS[material]
        build(material, source, values, presets, out)


if __name__ == "__main__":
    main()
