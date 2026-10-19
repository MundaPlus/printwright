"""Imported STL files: load them for edits, save the result.

Two ways, chosen when the STL is imported (web/importer.py):

- "cad" parts (small meshes): load_solid() turns the triangles into one build123d
  solid. Edits use build123d, and the part has STEP, face selection and the usual
  bed view. It is slow for big meshes (about 10 s for 15 000 triangles).
- "mesh" parts (big meshes): load() gives a manifold3d Manifold. Edits are mesh
  booleans (fast: 0.3 s for 135 000 triangles). save_stl() writes STL/<part>.stl.
  lib/bed.py on_bed_mesh() draws the bed view; the page adds the mesh itself.

Paths are relative to the part file (pass __file__), like the out= paths of cadgen.
manifold3d and trimesh come from uvx --with (MESH_WITH in lib/workspace.py).
"""
from __future__ import annotations

from pathlib import Path


def _path(rel: str, here: str) -> Path:
    return (Path(here).parent / rel).resolve()


def load_solid(rel: str, here: str):
    """The STL as one build123d Solid. Raise ValueError when the mesh is not a closed solid."""
    from cadgen import build123d as bd

    path = _path(rel, here)
    shapes = bd.Mesher().read(str(path))
    solids = [s for s in shapes if isinstance(s, bd.Solid)]
    if len(solids) != 1 or not solids[0].is_valid:
        raise ValueError(f"{path.name} is not one closed solid ({len(shapes)} shapes). "
                         "Import it again: it becomes a mesh part.")
    return solids[0]


def load(rel: str, here: str):
    """The STL as a manifold3d.Manifold (closed mesh), for fast mesh booleans.

    An open mesh (holes, loose triangles) cannot be a Manifold. Then this returns a
    trimesh.Trimesh: it slices and prints, but booleans on it fail.
    """
    import manifold3d as m3
    import trimesh

    tm = trimesh.load(str(_path(rel, here)), force="mesh", process=True)
    if not tm.is_watertight:
        trimesh.repair.fill_holes(tm)
    if not tm.is_watertight:
        print(f"warning: {Path(rel).name} is an open mesh. Edits with booleans do not work on it.")
        return tm
    mf = m3.Manifold(m3.Mesh(tm.vertices.astype("float32"), tm.faces.astype("uint32")))
    if mf.status() != m3.Error.NoError:
        print(f"warning: {Path(rel).name} is not a clean closed mesh ({mf.status()}). Booleans do not work on it.")
        return tm
    return mf


def to_trimesh(mesh):
    """A trimesh.Trimesh from a Manifold or a Trimesh."""
    import trimesh

    if isinstance(mesh, trimesh.Trimesh):
        return mesh
    m = mesh.to_mesh()
    return trimesh.Trimesh(vertices=m.vert_properties[:, :3], faces=m.tri_verts, process=False)


def save_stl(mesh, rel: str, here: str) -> str:
    """Write the mesh as a binary STL when it changed. Return rel, so a call can pass it on."""
    path = _path(rel, here)
    data = to_trimesh(mesh).export(file_type="stl")
    # The same mesh keeps its file and its time: tools/make.sh builds twice, and a newer
    # STL would mark the G-code as out of date.
    if path.exists() and path.read_bytes() == data:
        return rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    print(f"wrote STL: {path.relative_to(Path(here).parent.parent)}")
    return rel
