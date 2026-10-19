"""Bed view: place a part on the printer bed, with rulers and a print label.

Use from a part model:

    @step(out="../STEP/<part>_on_bed.step")
    def <part>_on_bed():
        return on_bed(<part>(), "<part>")

Coordinates match the printer: bed front-left corner is (0, 0), bed top is Z=0.
The part sits at the bed centre, where OrcaSlicer's --arrange puts it.
Rulers start at 0 on the part's edges; a red mark and label show the exact size.
The label reads print time and grams from gcode/<part>.json (tools/slice.sh).
The part is turned as "placement" in presets/parts/<part>.json says (lib/part_settings.py),
so the bed view, the size check and the slicer agree.
A mesh part (an imported STL, lib/mesh.py) uses on_bed_mesh(): the same scene without the mesh.

The bed size comes from printwright.json in the workspace. The workspace is
PRINTWRIGHT_WORKSPACE, else the current folder, else the folder above the part
file. This file lives in the app; a workspace links src/lib to the app's lib/.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

from cadgen import build123d as bd
from cadgen import srgb

from .part_settings import is_turned, placement, rotation_matrix
from .workspace import find_workspace, load_config

# Not Path(__file__).resolve(): that points into the app, not the workspace.
# The part file is <workspace>/src/<part>.py, so its parent's parent is a fallback.
WS = find_workspace(hints=[Path(sys.argv[0]).absolute().parent.parent] if sys.argv and sys.argv[0] else None)
BED_X, BED_Y, BED_Z = load_config(WS)["bed"]  # printable area and height, mm
MARGIN = 14.0     # dark frame around the printable area, holds coordinates
PLATE = 3.0       # plate thickness below Z=0
GRID = 10.0       # grid pitch, mm; every 5th line is wider
GAP = 3.0         # space between part and ruler
RULER_W = 10.0    # ruler strip width
RULER_T = 0.8     # ruler strip thickness
INK = 0.25        # height of ticks and text above their surface

COL_PLATE = srgb("#2B2F36")
COL_AREA = srgb("#3A4049")
COL_GRID = srgb("#5C6672")
COL_RULER = srgb("#F2C230")
COL_INK = srgb("#15181C")
COL_EXACT = srgb("#FF5A4F")
COL_TEXT = srgb("#E8ECF1")
COL_PART = srgb("#4C9BE8")


def _rects(rects):
    """Fuse (cx, cy, w, h) rectangles into one face set."""
    faces = [bd.Rectangle(w, h).moved(bd.Location((cx, cy))) for cx, cy, w, h in rects]
    out = faces[0]
    for f in faces[1:]:
        out += f
    return out


def _texts(items, size):
    """Fuse (text, x, y, align) labels on the XY plane into one face set."""
    out = None
    for text, x, y, align in items:
        t = bd.Text(text, font_size=size, align=align).moved(bd.Location((x, y)))
        out = t if out is None else out + t
    return out


def _leaf(shape, color, label):
    shape.color = color
    shape.label = label
    return shape


def _ticks(length):
    """Tick positions and label step for a ruler of this length."""
    span = max(10.0, math.ceil(length / 10 - 1e-6) * 10)
    minor = 1.0 if span <= 80 else 5.0
    label = 10.0 if span <= 120 else 20.0 if span <= 200 else 50.0
    return span, minor, label


def _ruler(length, name, side=-1):
    """Flat ruler along +X from 0, built at the origin; the caller places it.

    The tick edge lies on local Y=0 (put it next to the part). side=-1 puts the
    strip on -Y, side=+1 on +Y. Text faces +Z.
    """
    span, minor, step = _ticks(length)
    strip = bd.Box(span, RULER_W, RULER_T,
                   align=(bd.Align.MIN, bd.Align.MAX if side < 0 else bd.Align.MIN, bd.Align.MIN))
    marks, labels = [], []
    for i in range(int(span / minor) + 1):
        x = i * minor
        major = math.isclose(x % step, 0, abs_tol=1e-6) or math.isclose(x % step, step, abs_tol=1e-6)
        mid = math.isclose(x % 5, 0, abs_tol=1e-6) or math.isclose(x % 5, 5, abs_tol=1e-6)
        h = 4.5 if major else 3.0 if mid else 1.6
        marks.append((x, side * h / 2, 0.35, h))
        if major:
            labels.append((f"{x:g}", x, side * 7.2, (bd.Align.CENTER, bd.Align.CENTER)))
    up = bd.Location((0, 0, RULER_T))
    ticks = bd.extrude(_rects(marks), amount=INK).moved(up)
    nums = bd.extrude(_texts(labels, 2.6), amount=INK).moved(up)
    exact = bd.extrude(_rects([(length, side * RULER_W / 2, 0.6, RULER_W)]), amount=INK * 2).moved(up)
    tag = bd.extrude(_texts([(f"{length:.1f}", span + 2, side * RULER_W / 2, (bd.Align.MIN, bd.Align.CENTER))], 4.0),
                     amount=INK).moved(up)
    return [
        _leaf(strip, COL_RULER, f"{name}_ruler"),
        _leaf(ticks, COL_INK, f"{name}_ticks"),
        _leaf(nums, COL_INK, f"{name}_numbers"),
        _leaf(exact, COL_EXACT, f"{name}_exact"),
        _leaf(tag, COL_EXACT, f"{name}_size"),
    ]


def _bed(label_text, fits):
    plate = bd.Box(BED_X + 2 * MARGIN, BED_Y + 2 * MARGIN, PLATE,
                   align=(bd.Align.MIN, bd.Align.MIN, bd.Align.MAX)).moved(bd.Location((-MARGIN, -MARGIN, -0.02)))
    area = bd.Box(BED_X, BED_Y, 0.02, align=(bd.Align.MIN, bd.Align.MIN, bd.Align.MAX))
    lines = []
    for i in range(int(BED_X // GRID) + 1):
        w = 0.6 if i % 5 == 0 else 0.25
        lines.append((i * GRID, BED_Y / 2, w, BED_Y))
    for j in range(int(BED_Y // GRID) + 1):
        w = 0.6 if j % 5 == 0 else 0.25
        lines.append((BED_X / 2, j * GRID, BED_X, w))
    grid = bd.extrude(_rects(lines), amount=0.03)
    coords = [(f"{x:g}", x, -MARGIN / 2, (bd.Align.CENTER, bd.Align.CENTER)) for x in range(0, int(BED_X) + 1, 50)]
    coords += [(f"{y:g}", -MARGIN / 2, y, (bd.Align.CENTER, bd.Align.CENTER)) for y in range(50, int(BED_Y) + 1, 50)]
    coord_text = bd.extrude(_texts(coords, 4.0), amount=INK).moved(bd.Location((0, 0, -0.02)))
    title = bd.extrude(_texts([(label_text, BED_X / 2, BED_Y + MARGIN / 2, (bd.Align.CENTER, bd.Align.CENTER))], 5.0),
                       amount=INK).moved(bd.Location((0, 0, -0.02)))
    return [
        _leaf(plate, COL_PLATE, "bed_plate"),
        _leaf(area, COL_AREA, "bed_printable_area"),
        _leaf(grid, COL_GRID, "bed_grid_10mm"),
        _leaf(coord_text, COL_TEXT, "bed_coordinates"),
        _leaf(title, COL_TEXT if fits else COL_EXACT, "print_label"),
    ]


def _print_info(name):
    stats = WS / "gcode" / f"{name}.json"
    if not stats.exists():
        return "not sliced yet"
    s = json.loads(stats.read_text())
    h, m = divmod(round(s["total_min"]), 60)
    t = f"{h} h {m:02d} min" if h else f"{m} min"
    return f"~{t} | {s['grams']:g} g {s['material']}"


def on_bed(part, name):
    """Return the bed scene with `part` centred on the bed and rulers beside it.

    The part is first turned around X, then Y, then Z, as its placement says.
    """
    pl = placement(name, WS)
    if is_turned(pl):
        color = part.color
        for axis, angle in zip((bd.Axis.X, bd.Axis.Y, bd.Axis.Z), pl["rotate"]):
            if angle % 360:
                part = part.rotate(axis, angle)
        part.color = color
    return _scene(part, name, pl)


def on_bed_mesh(stl, name, here):
    """Bed scene for a mesh part (lib/mesh.py): bed, rulers and label, but not the mesh.

    stl is the part's binary STL, relative to the part file `here` (pass __file__).
    The size comes from its triangles, turned as the placement says. The page draws
    the mesh itself from the placed STL, because a big mesh as CAD faces is too slow.
    """
    import numpy as np

    pl = placement(name, WS)
    data = (Path(here).parent / stl).read_bytes()
    count = int.from_bytes(data[80:84], "little")
    tris = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]),
                         count=count, offset=84)
    pts = tris["v"].reshape(-1, 3).astype(float) @ np.array(rotation_matrix(*pl["rotate"])).T
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    box = bd.Box(*(hi - lo), align=(bd.Align.MIN,) * 3).moved(bd.Location(tuple(lo)))
    return _scene(box, name, pl, show=False)


def _scene(part, name, pl, show=True):
    """Bed, rulers and label around the (turned) part. show=False leaves the part out."""
    bb = part.bounding_box()
    sx, sy, sz = bb.size.X, bb.size.Y, bb.size.Z
    fits = sx <= BED_X and sy <= BED_Y and sz <= BED_Z
    # Centre on the bed, bottom on Z=0.
    dx = BED_X / 2 - (bb.min.X + bb.max.X) / 2
    dy = BED_Y / 2 - (bb.min.Y + bb.max.Y) / 2
    placed = part.moved(bd.Location((dx, dy, -bb.min.Z)))
    placed.label = name
    if placed.color is None:
        placed.color = COL_PART
    x0, y0 = BED_X / 2 - sx / 2, BED_Y / 2 - sy / 2

    size = f"{sx:.1f} x {sy:.1f} x {sz:.1f} mm"
    label = f"{name} | {size} | {_print_info(name)}" + ("" if fits else " | DOES NOT FIT") + \
        (" | slicer turns it (auto-orient)" if pl["auto_orient"] else "")
    children = _bed(label, fits)

    # Each ruler is built along local +X with its tick edge on local Y=0.
    # Plane y_dir = z_dir x x_dir; `side` puts the strip on the side away from the part.
    rulers = [
        # X: flat, in front of the part. y_dir = +Y points at the part, so strip on -Y.
        ("x", sx, -1, bd.Plane(origin=(x0, y0 - GAP, 0), x_dir=(1, 0, 0), z_dir=(0, 0, 1))),
        # Y: flat, left of the part. y_dir = -X points away from the part, so strip on +Y.
        ("y", sy, +1, bd.Plane(origin=(x0 - GAP, y0, 0), x_dir=(0, 1, 0), z_dir=(0, 0, 1))),
        # Z: upright at the front-left corner, text facing the front. y_dir = -X.
        ("z", sz, +1, bd.Plane(origin=(x0 - GAP, y0 - GAP, 0), x_dir=(0, 0, 1), z_dir=(0, -1, 0))),
    ]
    for axis, length, side, plane in rulers:
        for s in _ruler(length, axis, side):
            children.append(s.moved(bd.Location(plane)))
    if show:
        children.append(placed)
    return bd.Compound(children=children, label=f"{name}_on_bed")
