"""Import an STL or a G-code file as a part, so Claude can edit it in the chat.

The page sends the file (Import button in the parts panel). This module:

1. saves it as imports/<part>.stl or imports/<part>.gcode (in git, never changed),
2. writes src/<part>.py from a template, by kind:
   - "cad": an STL with up to CAD_MAX_TRIANGLES triangles loads as one build123d solid.
     Edits use build123d. The part has STEP, face selection and the usual bed view.
   - "mesh": a bigger STL loads as a manifold3d mesh. Edits are fast mesh booleans.
     When a "cad" build fails (an open or broken mesh), the part becomes "mesh".
   - "gcode": a G-code keeps its toolpaths. Edits change the text (lib/gcode_edit.py):
     temperatures, fan, speed, flow, pauses. It needs no slicing.
3. adds the part to the project and builds it in the background (web/manage.py build).

app.py includes this router after manage.py.
"""
from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

import app as core  # noqa: E402  (web/app.py, already loaded)
import manage  # noqa: E402  (web/manage.py: build(), check_idle(), commit())

router = APIRouter(prefix="/api/import")
WS = core.WS
IMPORTS = WS / "imports"
MAX_IMPORT = 100 * 1024 * 1024
CAD_MAX_TRIANGLES = 20_000  # about 10 to 20 s as a CAD solid; more is slow, so it becomes a mesh part
EXT = {".stl": "stl", ".gcode": "gcode", ".gco": "gcode", ".g": "gcode"}

CAD_TEMPLATE = '''"""Imported STL: {name}, from {orig} ({tris} triangles).

The mesh loads as one CAD solid (lib/mesh.py load_solid), so build123d edits and
face selection work. imports/{name}.stl is the imported file: never change it.
Put the edits in {name}() below. Sizes in mm.
"""
from cadgen import build123d as bd
from cadgen import glb, step, stl
from lib.bed import on_bed
from lib.mesh import load_solid

SOURCE = "../imports/{name}.stl"  # the imported file


@step(out="../STEP/{name}.step")
@stl(out="../STL/{name}.stl")
def {name}():
    body = load_solid(SOURCE, __file__)
    # Edits go here, for example a 5 mm hole through the middle:
    # c = body.bounding_box().center()
    # body -= bd.Pos(c.X, c.Y, c.Z) * bd.Cylinder(2.5, 1000)
    body.label = "{name}"
    return body


@step(out="../STEP/{name}_on_bed.step")
@glb(out="../GLB/{name}_on_bed.glb")
def {name}_on_bed():
    return on_bed({name}(), "{name}")


if __name__ == "__main__":
    {name}()
    {name}_on_bed()
'''

MESH_TEMPLATE = '''"""Imported STL: {name}, from {orig} ({tris} triangles).

A big mesh: edits are manifold3d mesh booleans (lib/mesh.py), which stay fast.
There is no CAD solid: no STEP of the part and no face selection.
imports/{name}.stl is the imported file: never change it. Put the edits in {name}().
Sizes in mm. Review with: cadgen stl snapshot STL/{name}.stl tmp/{name}.png
"""
# printwright: mesh
import manifold3d as m3  # Manifold.cube, .cylinder, .sphere; + union, - difference, ^ intersection
from cadgen import glb, step
from lib.bed import on_bed_mesh
from lib.mesh import load, save_stl

SOURCE = "../imports/{name}.stl"  # the imported file
OUT = "../STL/{name}.stl"


def {name}():
    mesh = load(SOURCE, __file__)
    # Edits go here, for example a 5 mm hole through the middle:
    # x0, y0, z0, x1, y1, z1 = mesh.bounding_box()
    # hole = m3.Manifold.cylinder(z1 - z0 + 2, 2.5, 2.5, 64).translate([(x0 + x1) / 2, (y0 + y1) / 2, z0 - 1])
    # mesh = mesh - hole
    return mesh


@step(out="../STEP/{name}_on_bed.step")
@glb(out="../GLB/{name}_on_bed.glb")
def {name}_on_bed():
    return on_bed_mesh(save_stl({name}(), OUT, __file__), "{name}", __file__)


if __name__ == "__main__":
    {name}_on_bed()
'''

GCODE_TEMPLATE = '''"""Imported G-code: {name}, from {orig}.

The toolpaths stay as sliced. Edits change the G-code text (lib/gcode_edit.py):
temperatures, fan, speed, flow and pauses. There is no 3D model: to change the
shape, import the STL instead. imports/{name}.gcode is the imported file: never
change it. gcode/{name}.meta.json has the slicer, printer, layers and warnings.
Put the edits in edit() below, one line each. Layers count from 1.
"""
# printwright: gcode
from lib.gcode_edit import GCode

SOURCE = "../imports/{name}.gcode"  # the imported file
OUT = "../gcode/{name}.gcode"


def edit(g):
    # g.nozzle_temp(215, from_layer=2)         # degrees C from this layer on
    # g.bed_temp(65)                           # degrees C from layer 1
    # g.fan(50, from_layer=3)                  # part fan, %
    # g.speed(80)                              # % of the sliced speed
    # g.flow(105)                              # % extrusion
    # g.pause_at_layer(12, "Change filament")  # Klipper PAUSE, then RESUME
    # g.pause_at_height(10.0, "Insert magnets")
    # g.insert_at_layer(5, "M117 Hello")
    return g


if __name__ == "__main__":
    edit(GCode.load(SOURCE, __file__)).save(OUT, __file__)
'''


def stl_triangles(data: bytes) -> int:
    """Triangle count of a binary or ASCII STL. 0 when it is not an STL."""
    if len(data) >= 84:
        count = int.from_bytes(data[80:84], "little")
        if len(data) == 84 + 50 * count:
            return count
    head = data[:512].lstrip().lower()
    if head.startswith(b"solid") and b"facet" in data[:4096].lower():
        return len(re.findall(rb"facet\s+normal", data))
    return 0


def is_gcode(data: bytes) -> bool:
    text = data[:200_000].decode("utf-8", errors="replace")
    return "\0" not in text and len(re.findall(r"^G[01]\s", text, re.M)) >= 5


def template(kind: str, name: str, orig: str, tris: int = 0) -> str:
    t = {"cad": CAD_TEMPLATE, "mesh": MESH_TEMPLATE, "gcode": GCODE_TEMPLATE}[kind]
    orig = re.sub(r"[^\w .()+-]", "_", orig)[:80] or "the upload"
    return t.format(name=name, orig=orig, tris=f"{tris:,}".replace(",", " "))


def check_new_name(name: str):
    if not core.NAME_RE.match(name) or not name[0].isalpha() or len(name) > 60:
        raise HTTPException(400, "Use lowercase letters, digits and _ only. Start with a letter.")
    if name.endswith("_on_bed") or name in ("lib", "plate") or name.startswith("plate_"):
        raise HTTPException(400, f"{name} is a reserved name. Choose another name.")
    if (WS / "src" / f"{name}.py").exists() or (WS / "STL" / f"{name}.stl").exists() \
            or manage.part_presets(name).exists() or any(IMPORTS.glob(f"{name}.*")):
        raise HTTPException(409, f"A part {name} exists already. Choose another name.")


async def build_import(name: str, kind: str):
    """Build the new part. A "cad" STL that fails as a solid becomes a "mesh" part."""
    await manage.build(name)
    if kind == "cad" and manage.BUILDS[name]["state"] == "failed":
        src = WS / "src" / f"{name}.py"
        text = src.read_text()
        m = re.search(r"from (.+) \(([\d ]+) triangles\)", text)
        src.write_text(template("mesh", name, m.group(1) if m else "the upload",
                                int(m.group(2).replace(" ", "")) if m else 0))
        await manage.build(name)
        if manage.BUILDS[name]["state"] == "done":
            manage.BUILDS[name]["note"] = "It is not a clean solid, so it became a mesh part."
        await run_in_threadpool(manage.commit, f"Import {name} as a mesh part")


@router.post("")
async def import_file(request: Request, name: str, filename: str, project: str = ""):
    """Raw body upload of one STL or G-code file. Returns the new part and its kind."""
    manage.check_idle()
    name = name.strip().lower()
    check_new_name(name)
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in EXT:
        raise HTTPException(400, "Import an .stl or a .gcode file.")
    if project and project != core.UNSORTED:
        core.load_project(project)  # 404 for unknown ids
    data = await request.body()
    if not data:
        raise HTTPException(400, "The file is empty.")
    if len(data) > MAX_IMPORT:
        raise HTTPException(413, f"The file is larger than {MAX_IMPORT // 2**20} MB.")
    tris = 0
    if EXT[ext] == "stl":
        tris = await run_in_threadpool(stl_triangles, data)
        if not tris:
            raise HTTPException(400, "This is not an STL file, or it holds no triangles.")
        kind = "cad" if tris <= CAD_MAX_TRIANGLES else "mesh"
    else:
        if not await run_in_threadpool(is_gcode, data):
            raise HTTPException(400, "This is not a G-code file (no G0/G1 moves found).")
        kind = "gcode"
    async with manage.LOCK:
        IMPORTS.mkdir(exist_ok=True)
        stored = manage.inside(IMPORTS) / f"{name}.{EXT[ext]}"
        await run_in_threadpool(stored.write_bytes, data)
        (WS / "src" / f"{name}.py").write_text(template(kind, name, filename, tris))
        if project and project != core.UNSORTED:
            proj = core.load_project(project)
            proj["parts"] = sorted(set(proj.get("parts", [])) | {name})
            core.save_project(proj)
        await run_in_threadpool(manage.commit, f"Import {name} from {filename[:60]}")
    asyncio.create_task(build_import(name, kind))
    return {"ok": True, "name": name, "kind": kind, "triangles": tris}
