"""Print settings, materials and re-slice for the page (routes under /api/slicing).

The settings of a part or a plate live in <workspace>/presets/parts/<name>.json.
lib/part_settings.py reads, checks and writes them; tools/slice.sh and tools/plate.py
apply them. A plate is named plate_<project>, like its G-code.

Re-slice runs tools/slice.sh for one part (no CAD rebuild: it uses the STL as it is),
then rebuilds the bed view with cadgen, because the label and the placement come
from the new files. One job runs at a time. The page polls the job state.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

APP = Path(__file__).resolve().parents[1]
for _p in (APP / "lib", APP / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
import part_settings  # noqa: E402  (lib/part_settings.py)
import workspace  # noqa: E402  (lib/workspace.py)

WS = workspace.find_workspace()
NAME_RE = re.compile(r"^[a-z0-9_]+$")
CADGEN_PY = ["uvx", "--no-config", "--managed-python", "--python", "3.13", "--from", "cadgen==0.7.24", "python"]
JOB_TIMEOUT = 900  # seconds per step
# G-code config keys the page shows. Orca writes them at the end of the file.
GCODE_KEYS = {"filament_type", "filament_settings_id", "nozzle_temperature", "nozzle_temperature_initial_layer",
              "curr_bed_type", "hot_plate_temp", "hot_plate_temp_initial_layer", "textured_plate_temp",
              "cool_plate_temp", "eng_plate_temp", *part_settings.FIELD}
BED_TEMP_KEY = {"Cool Plate": "cool_plate_temp", "Engineering Plate": "eng_plate_temp",
                "High Temp Plate": "hot_plate_temp", "Textured PEI Plate": "textured_plate_temp"}

router = APIRouter(prefix="/api/slicing")
JOBS: dict[str, dict] = {}
LOCK = asyncio.Lock()


def placed_stl_path(name: str) -> Path:
    """The STL as it lies on the bed (turned by its placement). Falls back to the plain STL."""
    try:
        return part_settings.placed_stl(name, WS)
    except (part_settings.SettingsError, OSError):
        return WS / "STL" / f"{name}.stl"


def stl_xy(path: Path) -> list[float] | None:
    """X and Y size of an STL in mm."""
    try:
        tris = part_settings._read_stl(path)
    except OSError:
        return None
    if not tris:
        return None
    return [round(max(v[i] for t in tris for v in t) - min(v[i] for t in tris for v in t), 2) for i in (0, 1)]


def target(name: str) -> str:
    """'part' or 'plate'. 404 when the part or the plate's project does not exist."""
    if not NAME_RE.match(name or ""):
        raise HTTPException(404, "no such part")
    if (WS / "src" / f"{name}.py").is_file():
        return "part"
    if name.startswith("plate_") and (WS / "projects" / name[len("plate_"):] / "project.json").is_file():
        return "plate"
    raise HTTPException(404, "no such part")


def gcode_header(name: str) -> dict | None:
    """Settings that OrcaSlicer wrote into gcode/<name>.gcode (its config block at the end)."""
    path = WS / "gcode" / f"{name}.gcode"
    if not path.is_file():
        return None
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(64_000).decode(errors="replace")
        f.seek(max(0, size - 400_000))
        tail = f.read().decode(errors="replace")
    # Footprint of the first object (Klipper's object list in the header), to check the turn.
    footprint = None
    m = re.search(r"^EXCLUDE_OBJECT_DEFINE NAME=\S+ CENTER=\S+ POLYGON=(\[.*\])\s*$", head, re.M)
    if m:
        try:
            poly = json.loads(m.group(1))
            footprint = [round(max(p[i] for p in poly) - min(p[i] for p in poly), 2) for i in (0, 1)]
        except (ValueError, TypeError, IndexError):
            pass
    found = {}
    for line in tail.splitlines():
        if line.startswith("; ") and " = " in line:
            key, value = line[2:].split(" = ", 1)
            if key in GCODE_KEYS:
                found[key] = value.strip().strip('"')
    bed_key = BED_TEMP_KEY.get(found.get("curr_bed_type", ""), "hot_plate_temp")
    return {"values": found, "material": found.get("filament_type", "?"),
            "nozzle": found.get("nozzle_temperature"), "nozzle_first": found.get("nozzle_temperature_initial_layer"),
            "bed": found.get(bed_key), "footprint": footprint, "updated": path.stat().st_mtime}


def stale_keys(eff: dict, head: dict | None, size: list[float] | None = None) -> list[str]:
    """Settings that differ between the settings file and the last G-code.

    size: the part's X and Y as it lies on the bed. A footprint that differs means
    the turn changed after slicing (a turn by 180 degrees does not show).
    """
    if not head:
        return []
    out = [k for k, v in eff["values"].items()
           if k in head["values"] and not part_settings._same(k, v, head["values"][k])]
    if eff["material"]["value"] != head["material"].upper():
        mat = next((m for m in eff["material"]["options"] if m["id"] == eff["material"]["value"]), None)
        if not mat or mat["type"].upper() != head["material"].upper():
            out.append("material")
    pl = eff.get("placement")
    fp = head.get("footprint")
    if pl and not pl["auto_orient"] and size and fp and \
            (abs(fp[0] - size[0]) > 0.6 or abs(fp[1] - size[1]) > 0.6):
        out.append("placement")
    return out


def job_state(name: str) -> dict:
    return JOBS.get(name) or {"name": name, "state": "idle"}


def settings_view(name: str, kind: str) -> dict:
    try:
        eff = part_settings.effective(name, kind, WS)
    except part_settings.SettingsError as e:
        raise HTTPException(400, str(e))
    head = gcode_header(name)
    size = None
    if kind == "part" and head and (WS / "STL" / f"{name}.stl").is_file():
        size = stl_xy(placed_stl_path(name))
    stale = stale_keys(eff, head, size)
    if kind == "plate" and head and plate_arrange(name) not in (None, eff["arrange"]):
        stale.append("arrange")
    return {**eff, "sliced": head, "stale": stale, "job": job_state(name)}


def plate_arrange(name: str) -> dict | None:
    """The layout settings of the last arranged plate. Plates from before this setting used the default."""
    try:
        layout = json.loads((WS / "gcode" / f"{name}.layout.json").read_text())
    except (OSError, ValueError):
        return None
    return layout.get("arrange") or part_settings.DEFAULT_ARRANGE


@router.get("/materials")
async def materials():
    try:
        return await run_in_threadpool(part_settings.materials, WS)
    except part_settings.SettingsError as e:
        raise HTTPException(500, str(e))


@router.get("/settings/{name}")
async def get_settings(name: str):
    """Defaults from the process preset, the overrides of this part or plate, and the result."""
    return await run_in_threadpool(settings_view, name, target(name))


@router.put("/settings/{name}")
async def put_settings(name: str, request: Request):
    """Check and save the panel values. Nothing is sliced here."""
    kind = target(name)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Send JSON.")
    if not isinstance(body, dict):
        raise HTTPException(400, "Send a JSON object.")
    if kind == "plate":
        body.pop("placement", None)
    try:
        await run_in_threadpool(part_settings.update, name, body, kind, WS)
    except part_settings.SettingsError as e:
        raise HTTPException(400, str(e))
    return await run_in_threadpool(settings_view, name, kind)


@router.get("/gcode/{name}")
async def gcode_info(name: str):
    """Material, temperatures and settings of the G-code as sliced (for the print dialog)."""
    target(name)
    head = await run_in_threadpool(gcode_header, name)
    if not head:
        raise HTTPException(404, "not sliced yet")
    return head


@router.get("/placed-stl/{name}")
async def placed_stl(name: str):
    """The STL as the slicer gets it (turned by its placement), for the plate preview."""
    if not NAME_RE.match(name) or not (WS / "STL" / f"{name}.stl").is_file():
        raise HTTPException(404)
    path = await run_in_threadpool(placed_stl_path, name)
    return FileResponse(path, media_type="model/stl", headers={"Cache-Control": "no-cache"})


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=WS, env={**os.environ, "PRINTWRIGHT_WORKSPACE": str(WS)},
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=JOB_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, out.decode(errors="replace")


async def run_job(name: str):
    job = JOBS[name]
    async with LOCK:
        try:
            job.update(step="slice", text="Slicing…")
            rc, out = await _run([str(APP / "tools" / "slice.sh"), f"STL/{name}.stl"])
            if rc != 0:
                raise RuntimeError("Slicing failed: " + " | ".join(out.strip().splitlines()[-4:]))
            job.update(step="view", text="Updating the bed view…", sliced=time.time())
            if (WS / "src" / f"{name}.py").is_file():
                rc, out = await _run(CADGEN_PY + [f"src/{name}.py", "--force"])
                errors = [ln for ln in out.splitlines() if re.search(r"error|Error", ln)]
                if rc != 0 or errors:
                    job["warning"] = "The bed view was not updated: " + " | ".join((errors or out.splitlines())[-2:])
            job.update(state="done", step="done", text="Done")
        except Exception as e:  # report to the page, never hang the job
            job.update(state="error", step="error", error=str(e)[-500:])
        finally:
            job["finished"] = time.time()


@router.post("/reslice/{name}")
async def reslice(name: str):
    """Slice one part again with its settings. The CAD model is not rebuilt."""
    if target(name) != "part":
        raise HTTPException(400, "Use Arrange on plate to slice a plate again.")
    if not (WS / "STL" / f"{name}.stl").is_file():
        raise HTTPException(409, f"{name} is not built yet.")
    running = next((j for j in JOBS.values() if j["state"] == "running"), None)
    if running or LOCK.locked():
        raise HTTPException(409, f"{running['name'] if running else 'A part'} is being sliced. Wait a moment.")
    JOBS[name] = {"name": name, "state": "running", "step": "queued", "text": "Starting…",
                  "started": time.time(), "error": None, "warning": None}
    asyncio.create_task(run_job(name))
    return JOBS[name]


@router.get("/jobs/{name}")
async def get_job(name: str):
    if not NAME_RE.match(name):
        raise HTTPException(404)
    return job_state(name)
