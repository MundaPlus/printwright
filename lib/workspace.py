#!/usr/bin/env python3
"""Find the Printwright workspace and read its printwright.json.

The app (this repo) holds the code. A workspace holds your parts, projects and
config. Every tool finds the workspace the same way:

  1. the environment variable PRINTWRIGHT_WORKSPACE, else
  2. the current folder.

The folder must contain printwright.json. Create a workspace with
tools/init-workspace.sh <dir>.

Stdlib only. It runs on the system Python (3.8+) and under cadgen's Python.

Command line (for shell scripts):
  workspace.py root            print the workspace path
  workspace.py presets         print the presets folder
  workspace.py get KEY         print one config value (JSON for lists)
  workspace.py kind FILE       print the kind of a part file: cad, mesh or gcode
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
CONFIG_NAME = "printwright.json"

DEFAULTS = {
    "owner": "the user",
    "printer_name": "3D printer",
    "moonraker_url": "http://printer.local",
    "bed": [235.0, 230.0, 265.0],
    "nozzle": 0.4,
    "material": "PLA",
    "file_prefix": "AI_",
    "presets": "",
    "orca": {
        "printer": "Elegoo Neptune 4 Pro (0.4 nozzle)",
        "process": "0.20mm Standard @Elegoo Neptune4Pro (0.4 nozzle)",
        "filament": "Elegoo Generic PLA",
    },
}


class WorkspaceError(SystemExit):
    """No workspace, or a bad config. Exits with a clear message."""


def find_workspace(hints: list[Path] | None = None) -> Path:
    """Return the workspace folder. Raise WorkspaceError when there is none."""
    env = os.environ.get("PRINTWRIGHT_WORKSPACE", "").strip()
    if env:
        ws = Path(env).expanduser().resolve()
        if not (ws / CONFIG_NAME).is_file():
            raise WorkspaceError(
                f"PRINTWRIGHT_WORKSPACE={env} has no {CONFIG_NAME}. "
                f"Create the workspace with: {APP}/tools/init-workspace.sh {env}")
        return ws
    for d in [Path.cwd(), *(hints or [])]:
        if (d / CONFIG_NAME).is_file():
            return d.resolve()
    raise WorkspaceError(
        f"No {CONFIG_NAME} in {Path.cwd()}. Run this command inside a Printwright workspace, "
        f"or set PRINTWRIGHT_WORKSPACE. Create a workspace with: {APP}/tools/init-workspace.sh <dir>")


def load_config(ws: Path | None = None) -> dict:
    """Read printwright.json over the defaults and check the values."""
    ws = ws or find_workspace()
    path = ws / CONFIG_NAME
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise WorkspaceError(f"{path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise WorkspaceError(f"{path} must hold a JSON object.")
    cfg = {**DEFAULTS, **{k: v for k, v in data.items() if not k.startswith("_")}}
    cfg["orca"] = {**DEFAULTS["orca"], **(data.get("orca") or {})}
    bed = cfg["bed"]
    if not (isinstance(bed, list) and len(bed) == 3 and all(isinstance(v, (int, float)) and v > 0 for v in bed)):
        raise WorkspaceError(f"{path}: \"bed\" must be [x, y, z] in mm, for example [235, 230, 265].")
    cfg["bed"] = [float(v) for v in bed]
    cfg["nozzle"] = float(cfg["nozzle"])
    cfg["moonraker_url"] = str(cfg["moonraker_url"]).rstrip("/")
    return cfg


def presets_dir(ws: Path | None = None, cfg: dict | None = None) -> Path:
    """Folder with printer.json, process.json and filament-1.json.

    "presets" in printwright.json: empty means the app's presets/; a relative
    path is relative to the workspace.
    """
    ws = ws or find_workspace()
    cfg = cfg or load_config(ws)
    value = str(cfg.get("presets") or "").strip()
    if not value:
        return APP / "presets"
    p = Path(value).expanduser()
    return (p if p.is_absolute() else ws / p).resolve()


# Part kinds. A part file in src/ is "cad" (build123d), unless a line near its top says
# "# printwright: mesh" (an imported STL edited with manifold3d) or "# printwright: gcode"
# (an imported G-code with text edits). tools/make.sh and the web app read this mark.
KIND_RE = re.compile(r"^# printwright: (mesh|gcode)\s*$", re.M)
# uvx options that add the mesh libraries to cadgen's Python, for "mesh" parts.
MESH_WITH = ["--with", "manifold3d==3.5.4", "--with", "trimesh==5.1.1", "--with", "networkx==3.7"]  # networkx: trimesh hole repair


def part_kind(path: Path) -> str:
    """'cad', 'mesh' or 'gcode' for a part file (src/<part>.py)."""
    try:
        with open(path, errors="replace") as f:
            head = f.read(2000)
    except OSError:
        return "cad"
    m = KIND_RE.search(head)
    return m.group(1) if m else "cad"


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 1
    if argv[0] == "kind" and len(argv) == 2:  # needs no workspace
        print(part_kind(Path(argv[1])))
        return 0
    ws = find_workspace()
    if argv[0] == "root":
        print(ws)
    elif argv[0] == "presets":
        print(presets_dir(ws))
    elif argv[0] == "get" and len(argv) == 2:
        value = load_config(ws).get(argv[1])
        print(json.dumps(value) if isinstance(value, (list, dict)) else value)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
