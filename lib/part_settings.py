#!/usr/bin/env python3
"""Print settings per part and per plate: presets/parts/<name>.json.

A settings file holds OrcaSlicer process keys. Their values are strings, as in
process.json, and they go over process.json when the part is sliced:

  {"sparse_infill_pattern": "gyroid", "sparse_infill_density": "25%",
   "enable_support": "1", "support_type": "tree(auto)",
   "material": "PETG",
   "placement": {"rotate": [0, 0, 90], "auto_orient": false}}

Two keys belong to Printwright, not to OrcaSlicer:

  material    the filament preset. "PETG" uses presets/filaments/petg.json.
              No material: printwright.json "material". When that is the type of
              filament-1.json, the slicer uses filament-1.json.
  placement   rotate: degrees around X, then Y, then Z (bed axes).
              X and Y take 0, 90, 180 or 270; Z takes any angle.
              auto_orient: OrcaSlicer turns the part (--orient 1). The bed view
              then shows the part as designed, not as printed.

The slicer and the bed view (lib/bed.py) read the same file, so both agree.
A plate (tools/plate.py) uses its own file presets/parts/plate_<project>.json for
the process and the material. Each part on the plate keeps its own placement.
OrcaSlicer's command line has no settings per object, so a plate cannot mix settings.

The tools look in <workspace>/presets/parts first, then in the presets folder.
The web app writes to <workspace>/presets/parts.

Stdlib only. It runs on the system Python (3.8+) and under cadgen's Python.

Command line:
  part_settings.py prepare NAME STL OUTDIR   for tools/slice.sh: prints shell variables
  part_settings.py show NAME                 the effective settings as JSON
  part_settings.py materials                 the filament presets you can choose
"""
from __future__ import annotations

import json
import math
import re
import shlex
import struct
import sys
from pathlib import Path

try:  # as src/lib/part_settings.py in a part model (package "lib")
    from . import workspace
except ImportError:  # as a script or with lib/ on sys.path
    import workspace  # type: ignore[no-redef]

APP = Path(__file__).resolve().parents[1]
NAME_RE = re.compile(r"^[a-z0-9_]+$")
OWN_KEYS = ("material", "placement")  # Printwright keys; never passed to OrcaSlicer

# Fields the settings panel edits. Values are OrcaSlicer process keys and values.
# Pattern names are checked against OrcaSlicer 2.4.2: it turns an unknown name
# into another pattern without a warning, so only these pass validation.
INFILL_PATTERNS = [
    ("grid", "Grid"), ("gyroid", "Gyroid"), ("cubic", "Cubic"), ("honeycomb", "Honeycomb"),
    ("lightning", "Lightning"), ("rectilinear", "Rectilinear"), ("line", "Line"),
    ("adaptivecubic", "Adaptive cubic"), ("alignedrectilinear", "Aligned rectilinear"),
    ("triangles", "Triangles"), ("tri-hexagon", "Tri-hexagon"), ("3dhoneycomb", "3D honeycomb"),
    ("crosshatch", "Cross hatch"), ("concentric", "Concentric"),
]
LAYER_HEIGHTS = ["0.12", "0.16", "0.2", "0.24", "0.28"]
FIELDS = [
    {"key": "layer_height", "label": "Layer height", "group": "Quality", "type": "choice", "unit": "mm",
     "options": [[v, f"{float(v):.2f} mm"] for v in LAYER_HEIGHTS]},
    {"key": "sparse_infill_pattern", "label": "Infill pattern", "group": "Strength", "type": "choice",
     "options": [list(p) for p in INFILL_PATTERNS]},
    {"key": "sparse_infill_density", "label": "Infill density", "group": "Strength", "type": "percent",
     "min": 0, "max": 100, "unit": "%"},
    {"key": "wall_loops", "label": "Walls", "group": "Strength", "type": "int", "min": 1, "max": 12},
    {"key": "top_shell_layers", "label": "Top layers", "group": "Strength", "type": "int", "min": 0, "max": 30},
    {"key": "bottom_shell_layers", "label": "Bottom layers", "group": "Strength", "type": "int", "min": 0, "max": 30},
    {"key": "enable_support", "label": "Supports", "group": "Supports", "type": "bool"},
    {"key": "support_type", "label": "Support type", "group": "Supports", "type": "choice",
     "options": [["normal(auto)", "Normal"], ["tree(auto)", "Tree"]]},
    {"key": "support_on_build_plate_only", "label": "Only from the bed", "group": "Supports", "type": "bool"},
    {"key": "support_threshold_angle", "label": "Overhang angle", "group": "Supports", "type": "int",
     "min": 0, "max": 90, "unit": "°"},
    {"key": "brim_type", "label": "Brim", "group": "Adhesion", "type": "choice",
     "options": [["no_brim", "None"], ["auto_brim", "Auto"], ["outer_only", "Outer"]]},
    {"key": "brim_width", "label": "Brim width", "group": "Adhesion", "type": "float", "min": 0, "max": 30, "unit": "mm"},
]
FIELD = {f["key"]: f for f in FIELDS}
# OrcaSlicer's own default for keys that a process preset may leave out.
ORCA_DEFAULTS = {"brim_type": "auto_brim", "brim_width": "5", "support_on_build_plate_only": "0",
                 "support_threshold_angle": "30", "support_type": "normal(auto)", "enable_support": "0"}
AXIS_STEPS = (0, 90, 180, 270)
NO_PLACEMENT = {"rotate": [0.0, 0.0, 0.0], "auto_orient": False}


class SettingsError(ValueError):
    """A bad value. The message says which one and why."""


# --- files ------------------------------------------------------------------

def _ws(ws: Path | None) -> Path:
    return ws or workspace.find_workspace()


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise SettingsError(f"{path} is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise SettingsError(f"{path} must hold a JSON object.")
    return data


def settings_file(name: str, ws: Path | None = None) -> Path | None:
    """The settings file the slicer uses for this part or plate, or None."""
    ws = _ws(ws)
    for d in (ws / "presets" / "parts", workspace.presets_dir(ws) / "parts"):
        if (d / f"{name}.json").is_file():
            return d / f"{name}.json"
    return None


def load(name: str, ws: Path | None = None) -> dict:
    """The raw settings of a part or plate ({} when it has no file)."""
    path = settings_file(name, ws)
    return _read_json(path) if path else {}


def process_defaults(ws: Path | None = None) -> dict:
    """The process preset (process.json) with OrcaSlicer's defaults for missing keys."""
    ws = _ws(ws)
    proc = _read_json(workspace.presets_dir(ws) / "process.json")
    return {**ORCA_DEFAULTS, **proc}


# --- materials --------------------------------------------------------------

def _first(value):
    return value[0] if isinstance(value, list) and value else value


def _filament_dirs(ws: Path) -> list[Path]:
    dirs = [workspace.presets_dir(ws) / "filaments", ws / "presets" / "filaments", APP / "presets" / "filaments"]
    out: list[Path] = []
    for d in dirs:
        if d.is_dir() and d.resolve() not in [o.resolve() for o in out]:
            out.append(d)
    return out


def _filament_summary(path: Path, mid: str, source: str) -> dict:
    f = _read_json(path)
    return {"id": mid, "type": str(_first(f.get("filament_type")) or mid), "name": f.get("name", path.stem),
            "nozzle": _first(f.get("nozzle_temperature")),
            "nozzle_first": _first(f.get("nozzle_temperature_initial_layer")),
            "bed": _first(f.get("hot_plate_temp")), "bed_first": _first(f.get("hot_plate_temp_initial_layer")),
            "file": str(path), "source": source}


def materials(ws: Path | None = None) -> list[dict]:
    """Filament presets you can choose: filament-1.json first, then presets/filaments/*.json."""
    ws = _ws(ws)
    base = workspace.presets_dir(ws) / "filament-1.json"
    out = [_filament_summary(base, "", "default")]
    out[0]["id"] = out[0]["type"].upper()
    seen = {out[0]["id"]}
    for d in _filament_dirs(ws):
        source = "app" if d.resolve() == (APP / "presets" / "filaments").resolve() else "workspace"
        for p in sorted(d.glob("*.json")):
            mid = p.stem.upper()
            if NAME_RE.match(p.stem.lower()) and mid not in seen:
                seen.add(mid)
                out.append(_filament_summary(p, mid, source))
    return out


def default_material(ws: Path | None = None) -> str:
    return str(workspace.load_config(_ws(ws))["material"]).strip().upper() or "PLA"


def filament_file(material: str | None, ws: Path | None = None) -> Path:
    """The filament preset for a material. None means the default material."""
    ws = _ws(ws)
    mid = (material or default_material(ws)).strip().upper()
    for m in materials(ws):
        if m["id"] == mid:
            return Path(m["file"])
    raise SettingsError(f"No filament preset for {mid}. Add presets/filaments/{mid.lower()}.json "
                        "(tools/make_filaments.py writes them).")


# --- placement --------------------------------------------------------------

def placement(name: str, ws: Path | None = None, raw: dict | None = None) -> dict:
    """Rotation [x, y, z] in degrees and the auto-orient flag of a part."""
    raw = load(name, ws) if raw is None else raw
    p = raw.get("placement") or {}
    rot = p.get("rotate") or [0, 0, 0]
    try:
        rot = [float(v) for v in rot][:3] + [0.0] * (3 - len(rot[:3]))
    except (TypeError, ValueError):
        rot = [0.0, 0.0, 0.0]
    return {"rotate": rot, "auto_orient": bool(p.get("auto_orient"))}


def is_turned(pl: dict) -> bool:
    return any(abs(v % 360) > 1e-9 for v in pl["rotate"])


def rotation_matrix(rx: float, ry: float, rz: float) -> list[list[float]]:
    """R = Rz * Ry * Rx: turn around X first, then Y, then Z (fixed bed axes)."""
    def m(axis, deg):
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        if abs(c) < 1e-12:
            c = 0.0
        if abs(s) < 1e-12:
            s = 0.0
        return {"x": [[1, 0, 0], [0, c, -s], [0, s, c]],
                "y": [[c, 0, s], [0, 1, 0], [-s, 0, c]],
                "z": [[c, -s, 0], [s, c, 0], [0, 0, 1]]}[axis]

    def mul(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return mul(m("z", rz), mul(m("y", ry), m("x", rx)))


def _read_stl(path: Path) -> list[tuple]:
    """Triangles as (v1, v2, v3) from a binary or ASCII STL."""
    data = path.read_bytes()
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    tris = []
    if len(data) == 84 + 50 * count:
        for i in range(count):
            v = struct.unpack_from("<9f", data, 84 + 50 * i + 12)
            tris.append((v[0:3], v[3:6], v[6:9]))
        return tris
    verts = [tuple(float(c) for c in m.groups())
             for m in re.finditer(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)]
    return [tuple(verts[i:i + 3]) for i in range(0, len(verts) - 2, 3)]


def transform_stl(src: Path, dst: Path, rot: list[float]) -> list[float]:
    """Write src turned by rot (degrees X, Y, Z) as a binary STL.

    The result sits with its bottom on Z=0, centred on X=0, Y=0. Returns the size [x, y, z].
    """
    r = rotation_matrix(*rot)
    out = []
    for tri in _read_stl(src):
        out.append([tuple(sum(r[i][k] * v[k] for k in range(3)) for i in range(3)) for v in tri])
    if not out:
        raise SettingsError(f"{src} holds no triangles.")
    lo = [min(v[a] for t in out for v in t) for a in range(3)]
    hi = [max(v[a] for t in out for v in t) for a in range(3)]
    shift = [-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]]
    buf = bytearray(b"Printwright placed STL".ljust(80, b" "))
    buf += struct.pack("<I", len(out))
    for t in out:
        p = [[v[a] + shift[a] for a in range(3)] for v in t]
        u = [p[1][a] - p[0][a] for a in range(3)]
        w = [p[2][a] - p[0][a] for a in range(3)]
        n = [u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]]
        ln = math.sqrt(sum(c * c for c in n)) or 1.0
        buf += struct.pack("<12fH", *(c / ln for c in n), *p[0], *p[1], *p[2], 0)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    tmp.write_bytes(bytes(buf))
    tmp.replace(dst)
    return [round(hi[a] - lo[a], 2) for a in range(3)]


def placed_stl(name: str, ws: Path | None = None) -> Path:
    """The STL the slicer gets: STL/<name>.stl, or a turned copy in STL/placed/.

    The copy is made again when the STL or the settings file is newer.
    """
    ws = _ws(ws)
    src = ws / "STL" / f"{name}.stl"
    pl = placement(name, ws)
    if not is_turned(pl) or not src.exists():
        return src
    dst = ws / "STL" / "placed" / f"{name}.stl"
    sf = settings_file(name, ws)
    newest = max(src.stat().st_mtime, sf.stat().st_mtime if sf else 0)
    if not dst.exists() or dst.stat().st_mtime < newest:
        transform_stl(src, dst, pl["rotate"])
    return dst


# --- validation and effective settings ---------------------------------------

def _num(value, field: dict, kind=float):
    s = str(value).strip().rstrip("%").strip()
    try:
        v = float(s)
        if not math.isfinite(v):
            raise ValueError
    except ValueError:
        raise SettingsError(f"{field['label']}: give a number, not {value!r}.")
    if kind is int:
        if v != int(v):
            raise SettingsError(f"{field['label']}: give a whole number.")
        v = int(v)
    if not (field["min"] <= v <= field["max"]):
        raise SettingsError(f"{field['label']}: give {field['min']} to {field['max']}.")
    return v


def clean_value(key: str, value) -> str:
    """Check one panel value and return it in OrcaSlicer's form (a string)."""
    f = FIELD[key]
    if f["type"] == "choice":
        v = str(value).strip()
        if key == "layer_height":
            try:
                v = f"{float(v):g}"  # "0.20" -> "0.2"
            except ValueError:
                pass
        if v not in [o[0] for o in f["options"]]:
            raise SettingsError(f"{f['label']}: choose one of {', '.join(o[1] for o in f['options'])}.")
        return v
    if f["type"] == "bool":
        if value in (True, 1, "1", "true", "on"):
            return "1"
        if value in (False, 0, "0", "false", "off"):
            return "0"
        raise SettingsError(f"{f['label']}: give on or off.")
    if f["type"] == "percent":
        return f"{_num(value, f, int)}%"
    if f["type"] == "int":
        return str(_num(value, f, int))
    return f"{_num(value, f):g}"


def _same(key: str, a, b) -> bool:
    """True when two values mean the same (0.2 and 0.20, 15% and 15)."""
    try:
        return math.isclose(float(str(a).rstrip("%")), float(str(b).rstrip("%")))
    except (TypeError, ValueError):
        return str(a) == str(b)


def clean_placement(value) -> dict:
    if not isinstance(value, dict):
        raise SettingsError("placement must be an object.")
    rot = value.get("rotate", [0, 0, 0])
    if not isinstance(rot, list) or len(rot) != 3:
        raise SettingsError("placement.rotate must be [x, y, z] in degrees.")
    try:
        rx, ry, rz = (float(v) for v in rot)
    except (TypeError, ValueError):
        raise SettingsError("placement.rotate must hold numbers.")
    for label, v in (("X", rx), ("Y", ry)):
        if v % 360 not in AXIS_STEPS:
            raise SettingsError(f"Turn around {label}: choose 0, 90, 180 or 270 degrees.")
    if not -360 <= rz <= 360:
        raise SettingsError("Turn around Z: give -360 to 360 degrees.")
    rz = round(rz % 360, 2)
    return {"rotate": [rx % 360, ry % 360, 0.0 if rz == 360 else rz], "auto_orient": bool(value.get("auto_orient"))}


def update(name: str, body: dict, kind: str = "part", ws: Path | None = None) -> dict:
    """Check the panel values and write <workspace>/presets/parts/<name>.json.

    body: {"values": {key: value}, "material": "PETG" | "", "placement": {...}}.
    A value equal to the default is removed. Keys the panel does not know stay.
    Returns the new file content.
    """
    ws = _ws(ws)
    if not NAME_RE.match(name):
        raise SettingsError("bad name")
    raw = load(name, ws)
    out = {k: v for k, v in raw.items() if k not in FIELD and k not in OWN_KEYS}
    defaults = process_defaults(ws)
    values = body.get("values") or {}
    if not isinstance(values, dict):
        raise SettingsError("values must be an object.")
    unknown = [k for k in values if k not in FIELD]
    if unknown:
        raise SettingsError(f"Unknown setting: {', '.join(unknown)}.")
    for key in FIELD:
        if key in values and values[key] not in (None, ""):
            v = clean_value(key, values[key])
            if not _same(key, v, defaults.get(key)):
                out[key] = v
        elif key in raw and key not in values:
            out[key] = raw[key]  # the panel did not send it: keep it
    material = str(body.get("material", raw.get("material", "")) or "").strip().upper()
    if material:
        if material not in [m["id"] for m in materials(ws)]:
            raise SettingsError(f"Unknown material {material}.")
        if material != default_material(ws):
            out["material"] = material
    if kind == "part":
        pl = clean_placement(body["placement"]) if "placement" in body else placement(name, ws, raw)
        if is_turned(pl) or pl["auto_orient"]:
            out["placement"] = pl
    path = ws / "presets" / "parts" / f"{name}.json"
    if out:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=1) + "\n")
    elif path.exists():
        path.unlink()
    return out


def effective(name: str, kind: str = "part", ws: Path | None = None) -> dict:
    """Defaults, overrides and the values the slicer uses, for the settings panel."""
    ws = _ws(ws)
    raw = load(name, ws)
    defaults = process_defaults(ws)
    sf = settings_file(name, ws)
    mats = materials(ws)
    mat_default = default_material(ws)
    return {
        "name": name,
        "kind": kind,
        "file": str(sf.relative_to(ws)) if sf and ws in sf.parents else (str(sf) if sf else None),
        "fields": FIELDS,
        "defaults": {k: str(defaults.get(k, "")) for k in FIELD},
        "overrides": {k: str(raw[k]) for k in FIELD if k in raw},
        "values": {k: str(raw.get(k, defaults.get(k, ""))) for k in FIELD},
        "extra": {k: v for k, v in raw.items() if k not in FIELD and k not in OWN_KEYS and not k.startswith("_")},
        "material": {"value": str(raw.get("material") or mat_default).upper(), "default": mat_default,
                     "options": [{k: m[k] for k in ("id", "type", "name", "nozzle", "nozzle_first", "bed", "bed_first",
                                                    "source")} for m in mats]},
        "placement": placement(name, ws, raw) if kind == "part" else None,
        "updated": sf.stat().st_mtime if sf else None,
    }


def slicer_files(name: str, outdir: Path, ws: Path | None = None) -> dict:
    """Process and filament files for one OrcaSlicer run, and the orient flag.

    When the settings change the process, the merged process.json goes into outdir.
    """
    ws = _ws(ws)
    raw = load(name, ws)
    process = workspace.presets_dir(ws) / "process.json"
    orca_keys = {k: v for k, v in raw.items() if k not in OWN_KEYS and not k.startswith("_")}
    if orca_keys:
        merged = {**_read_json(process), **orca_keys}
        process = outdir / "process.json"
        process.write_text(json.dumps(merged, indent=1))
    return {"PROCESS": str(process), "FILAMENT": str(filament_file(raw.get("material"), ws)),
            "ORIENT": "1" if placement(name, ws, raw)["auto_orient"] else "0"}


def prepare(name: str, stl: Path, outdir: Path, ws: Path | None = None) -> dict:
    """slicer_files() plus the STL to load: the part's own STL, turned when it has a placement."""
    ws = _ws(ws)
    files = slicer_files(name, outdir, ws)
    pl = placement(name, ws)
    if stl.resolve() == (ws / "STL" / f"{name}.stl").resolve():
        stl = placed_stl(name, ws)
    elif is_turned(pl):
        stl_out = outdir / f"{name}.stl"
        transform_stl(stl, stl_out, pl["rotate"])
        stl = stl_out
    return {**files, "STL": str(stl)}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 1
    try:
        if argv[0] == "prepare" and len(argv) == 4:
            for k, v in prepare(argv[1], Path(argv[2]), Path(argv[3])).items():
                print(f"{k}={shlex.quote(v)}")
        elif argv[0] == "show" and len(argv) == 2:
            print(json.dumps(effective(argv[1]), indent=1))
        elif argv[0] == "materials":
            print(json.dumps(materials(), indent=1))
        else:
            print(__doc__)
            return 1
    except SettingsError as e:
        print(f"part settings: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
