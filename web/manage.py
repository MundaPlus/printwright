"""Manage parts and projects, and edit the app settings (printwright.json).

Parts: delete, rename, move to another project, restore an older version from git.
Projects: rename, delete. Settings: the first-run setup screen and the gear button.

app.py includes this router at its end, so `import app` here gets the loaded module.
Every action refuses to run while a chat run works (409), to avoid races with Claude.
Every path stays inside the workspace. Source files are in the workspace git repo,
so a deleted part stays in its history. Generated files are not in git.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

import app as core  # noqa: E402  (web/app.py, already loaded: it imports this module at its end)

router = APIRouter(prefix="/api/manage")
WS: Path = core.WS
NAME_RE = core.NAME_RE
GEN_DIRS = ["STEP", "STL", "STL/placed", "GLB", "3MF", "gcode", "tmp"]  # generated, not in git
PLACEHOLDER_URL = "http://printer.local"  # the URL in examples/printwright.example.json
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
LOCK = asyncio.Lock()
BUILDS: dict[str, dict] = {}  # part -> background tools/make.sh run


# --- guards and helpers -------------------------------------------------------

def check_idle(part: str | None = None):
    """Refuse while Claude, a plate slice, another action or a build for this part runs."""
    if core.CURRENT and not core.CURRENT.done:
        raise HTTPException(409, "Claude is working. Wait until the reply ends.")
    if core.PLATE_LOCK.locked():
        raise HTTPException(409, "A plate is being sliced. Wait a moment.")
    if LOCK.locked():
        raise HTTPException(409, "Another change runs. Wait a moment.")
    if part and BUILDS.get(part, {}).get("state") == "running":
        raise HTTPException(409, f"{part} is being built. Wait a moment.")


def inside(path: Path) -> Path:
    """Return path when it is a real file or folder inside the workspace. Never follow links out."""
    if path.is_symlink() or not path.resolve().is_relative_to(WS.resolve()):
        raise HTTPException(400, f"Refused: {path} is outside the workspace.")
    return path


def part_src(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise HTTPException(404, "no such part")
    path = WS / "src" / f"{name}.py"
    if not path.is_file():
        raise HTTPException(404, f"No part {name}.")
    return inside(path)


def generated_files(name: str) -> list[Path]:
    """Generated files of one part: <name>.* and <name>_on_bed.* in STEP, STL, GLB, gcode, tmp."""
    stems = {name, f"{name}_on_bed"} - ({f"{name}_on_bed"} if (WS / "src" / f"{name}_on_bed.py").exists() else set())
    found = []
    for d in GEN_DIRS:
        folder = WS / d
        if not folder.is_dir():
            continue
        for f in folder.iterdir():
            if f.is_file() and not f.is_symlink() and f.name.split(".")[0] in stems:
                found.append(inside(f))
    return found


def part_presets(name: str) -> Path:
    """Per-part Orca overrides in the workspace (tracked in git)."""
    return WS / "presets" / "parts" / f"{name}.json"


def projects_with(name: str) -> list[dict]:
    return [p for p in core.all_projects() if name in p.get("parts", []) or name in (p.get("plate") or {})]


def drop_plate_files(pid: str):
    """The plate of this project names a changed part: remove it, the user arranges again."""
    for suffix in (".gcode", ".json", ".layout.json"):
        f = WS / "gcode" / f"plate_{pid}{suffix}"
        if f.is_file():
            inside(f).unlink()


def plate_names(pid: str, name: str) -> bool:
    lay = WS / "gcode" / f"plate_{pid}.layout.json"
    try:
        return name in json.loads(lay.read_text()).get("items", {})
    except (OSError, ValueError):
        return False


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=WS, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise HTTPException(500, f"git {args[0]} failed: {r.stderr.strip()[-200:]}")
    return r.stdout


def has_git() -> bool:
    return (WS / ".git").exists() and shutil.which("git") is not None


def commit(message: str, paths: list[str] | None = None):
    core.commit_changes(message, prefix="", paths=paths)


# --- rename: the part file pattern (examples/src/demo_box.py) -----------------

def _decorator_outs(fn: ast.FunctionDef) -> list[str]:
    outs = []
    for d in fn.decorator_list:
        if isinstance(d, ast.Call):
            for kw in d.keywords:
                if kw.arg == "out" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    outs.append(kw.value.value)
    return outs


def check_pattern(text: str, name: str) -> str | None:
    """None when the file follows the part pattern for this name, else the reason."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return f"the file has a syntax error (line {e.lineno})"
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    if name not in funcs:
        return f"there is no top-level function {name}()"
    if f"{name}_on_bed" not in funcs:
        return f"there is no top-level function {name}_on_bed()"
    if not any(o.endswith(f"/{name}.stl") for o in _decorator_outs(funcs[name])):
        return f'{name}() has no @stl(out="../STL/{name}.stl")'
    if not any(o.endswith(f"/{name}_on_bed.step") or o.endswith(f"/{name}_on_bed.glb")
               for o in _decorator_outs(funcs[f"{name}_on_bed"])):
        return f'{name}_on_bed() has no @step or @glb out= path for {name}_on_bed'
    if not re.search(rf"""on_bed\(\s*{name}\(\)\s*,\s*["']{name}["']\s*\)""", text):
        return f'there is no on_bed({name}(), "{name}") call'
    return None


def rename_source(text: str, old: str, new: str) -> str:
    """Rename the part inside its file. Raise HTTPException when the file does not follow the pattern."""
    why = check_pattern(text, old)
    if why:
        raise HTTPException(422, f"Cannot rename {old} safely: {why}. Ask Claude in the chat to rename it.")
    o = re.escape(old)
    subs = [
        (rf"\bdef {o}(_on_bed)?\(", rf"def {new}\1("),                                 # definitions
        (rf"(?<![\w.]){o}(_on_bed)?\(\)", rf"{new}\1()"),                               # calls without arguments
        (rf"""(["'])(\.\./[A-Za-z0-9]+/){o}((?:_on_bed)?\.[A-Za-z0-9]+)\1""", rf"\1\2{new}\3\1"),  # out= paths
        (rf"""(["']){o}\1""", rf"\1{new}\1"),                                            # labels, on_bed(..., "<name>")
    ]
    for pat, rep in subs:
        text = re.sub(pat, rep, text)
    why = check_pattern(text, new)
    leftover = _leftover_names(text, old)
    if why or leftover:
        raise HTTPException(422, f"Cannot rename {old} safely: "
                            + (why or f"the file still uses {old} in: {', '.join(leftover)}")
                            + ". Ask Claude in the chat to rename it.")
    return text


def _leftover_names(text: str, old: str) -> list[str]:
    """Code that still names the old part after the rename (comments do not count)."""
    hits = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Name) and node.id in (old, f"{old}_on_bed"):
            hits.add(f"line {node.lineno}")
        elif isinstance(node, ast.FunctionDef) and node.name in (old, f"{old}_on_bed"):
            hits.add(f"line {node.lineno}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and \
                re.search(rf"(^|/){re.escape(old)}(_on_bed)?(\.\w+)?$", node.value):
            hits.add(f"line {node.lineno}")
    return sorted(hits)


# --- background build ---------------------------------------------------------

async def build(name: str):
    """Run tools/make.sh <name> in the background. The page polls /api/manage/builds."""
    BUILDS[name] = {"state": "running", "started": time.time(), "log": ""}
    try:
        proc = await asyncio.create_subprocess_exec(
            str(core.APP / "tools" / "make.sh"), name, cwd=WS,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=1200)
        BUILDS[name].update(state="done" if proc.returncode == 0 else "failed",
                            log=out.decode(errors="replace").strip()[-600:])
    except Exception as ex:  # report in the page, never crash the app
        BUILDS[name].update(state="failed", log=f"{type(ex).__name__}: {ex}")
    BUILDS[name]["finished"] = time.time()


@router.get("/builds")
async def builds():
    return BUILDS


# --- info ---------------------------------------------------------------------

@router.get("/info")
async def info():
    """What the page needs to show or hide the manage features."""
    return {"git": has_git(), "autocommit": has_git() and not os.environ.get("PRINT_WEB_NO_GIT"),
            "needs_setup": setup_problem(raw_config()) is not None}


# --- parts --------------------------------------------------------------------

@router.delete("/parts/{name}")
async def delete_part(name: str):
    check_idle(name)
    src = part_src(name)
    async with LOCK:
        await run_in_threadpool(commit, f"Save part {name} before delete")  # the last version goes into git
        removed = [str(src.relative_to(WS))]
        for f in generated_files(name):
            f.unlink()
            removed.append(str(f.relative_to(WS)))
        pre = part_presets(name)
        if pre.is_file():
            inside(pre).unlink()
            removed.append(str(pre.relative_to(WS)))
        for proj in projects_with(name):
            proj["parts"] = [p for p in proj.get("parts", []) if p != name]
            if name in (proj.get("plate") or {}):
                proj["plate"] = {p: n for p, n in proj["plate"].items() if p != name}
            core.save_project(proj)
        for proj in core.all_projects():
            if plate_names(proj["id"], name):
                drop_plate_files(proj["id"])
        src.unlink()
        BUILDS.pop(name, None)
        await run_in_threadpool(commit, f"Delete part {name}")
    return {"ok": True, "removed": removed}


@router.post("/parts/{name}/rename")
async def rename_part(name: str, request: Request):
    check_idle(name)
    new = str((await request.json()).get("to", "")).strip().lower()
    src = part_src(name)
    if not NAME_RE.match(new) or not new[0].isalpha() or len(new) > 60:
        raise HTTPException(400, "Use lowercase letters, digits and _ only. Start with a letter.")
    if new == name:
        raise HTTPException(400, "The new name is the same as the old name.")
    if new.endswith("_on_bed") or new in ("lib", "plate") or new.startswith("plate_"):
        raise HTTPException(400, f"{new} is a reserved name. Choose another name.")
    dest = WS / "src" / f"{new}.py"
    if dest.exists() or (WS / "STL" / f"{new}.stl").exists() or part_presets(new).exists():
        raise HTTPException(409, f"A part {new} exists already. Choose another name.")
    text = rename_source(src.read_text(), name, new)  # refuses before anything changes
    async with LOCK:
        inside(dest.parent)
        dest.write_text(text)
        src.unlink()
        for f in generated_files(name):
            f.rename(f.with_name(new + f.name[len(name):]))
        pre = part_presets(name)
        if pre.is_file():
            inside(pre).rename(part_presets(new))
        for proj in projects_with(name):
            proj["parts"] = sorted({new if p == name else p for p in proj.get("parts", [])})
            if name in (proj.get("plate") or {}):
                proj["plate"] = {(new if p == name else p): n for p, n in proj["plate"].items()}
            core.save_project(proj)
        for proj in core.all_projects():
            if plate_names(proj["id"], name):
                drop_plate_files(proj["id"])
        BUILDS.pop(name, None)
        await run_in_threadpool(commit, f"Rename part {name} to {new}")
    asyncio.create_task(build(new))  # new label in the bed view, new names in the G-code
    return {"ok": True, "name": new}


@router.post("/parts/{name}/move")
async def move_part(name: str, request: Request):
    """Move a part to another project. "unsorted" (Not in a project) works as source and target."""
    check_idle(name)
    part_src(name)
    body = await request.json()
    src_pid, dst_pid = str(body.get("from", "")), str(body.get("to", ""))
    if dst_pid == src_pid:
        raise HTTPException(400, "The part is in this project already.")
    target = None
    if dst_pid != core.UNSORTED:
        target = core.load_project(dst_pid)  # 404 for unknown ids
    async with LOCK:
        if src_pid and src_pid != core.UNSORTED:
            proj = core.load_project(src_pid)
            proj["parts"] = [p for p in proj.get("parts", []) if p != name]
            if name in (proj.get("plate") or {}):
                proj["plate"] = {p: n for p, n in proj["plate"].items() if p != name}
            core.save_project(proj)
            if plate_names(src_pid, name):
                drop_plate_files(src_pid)
        if target is None:  # to "Not in a project": no project may list it
            for proj in projects_with(name):
                proj["parts"] = [p for p in proj.get("parts", []) if p != name]
                core.save_project(proj)
            label = "Not in a project"
        else:
            target = core.load_project(dst_pid)  # read again: it may be the same file as above
            target["parts"] = sorted(set(target.get("parts", [])) | {name})
            core.save_project(target)
            label = target["name"]
        await run_in_threadpool(commit, f"Move part {name} to {label}")
    return {"ok": True}


@router.get("/parts/{name}/history")
async def part_history(name: str):
    """Commits that changed src/<name>.py, newest first. Follows renames."""
    src = part_src(name)
    if not has_git():
        raise HTTPException(404, "The workspace is not a git repo.")
    out = await run_in_threadpool(git, "log", "--follow", "-n", "60", "--format=%x00%H%x09%ct%x09%s",
                                  "--name-only", "--", f"src/{name}.py", check=False)
    items = []
    for chunk in out.split("\x00")[1:]:
        lines = [x for x in chunk.strip().splitlines() if x.strip()]
        if not lines:
            continue
        sha, ts, subject = (lines[0].split("\t", 2) + ["", ""])[:3]
        path = lines[1] if len(lines) > 1 else f"src/{name}.py"
        items.append({"sha": sha, "date": int(ts or 0), "subject": subject, "path": path,
                      "name": Path(path).stem})
    items = await run_in_threadpool(cut_copies, items)
    current = src.read_text()
    head = await run_in_threadpool(git, "show", f"HEAD:src/{name}.py", check=False)
    for it in items:
        it["current"] = False
    if items and head == current and items[0]["path"] == f"src/{name}.py":
        items[0]["current"] = True
    return {"items": items, "dirty": head != current,
            "autocommit": not os.environ.get("PRINT_WEB_NO_GIT")}


def cut_copies(items: list[dict]) -> list[dict]:
    """git --follow also follows copies: a new part made from another part. Keep only real renames."""
    for k in range(len(items) - 1):
        old, new = items[k + 1]["path"], items[k]["path"]
        if old == new:
            continue
        status = git("show", "-M", "--name-status", "--format=", items[k]["sha"], check=False)
        if not any(ln.startswith(("R", "D")) and f"\t{old}" in ln for ln in status.splitlines()):
            return items[:k + 1]
    return items


async def version_text(name: str, sha: str) -> tuple[str, str]:
    """(source, old part name) of one commit in the part's history."""
    if not SHA_RE.match(sha or ""):
        raise HTTPException(400, "bad version id")
    hist = {it["sha"]: it for it in (await part_history(name))["items"]}
    it = next((v for k, v in hist.items() if k.startswith(sha)), None)
    if not it:
        raise HTTPException(404, "This version is not in the history of the part.")
    text = await run_in_threadpool(git, "show", f"{it['sha']}:{it['path']}", check=False)
    if not text:
        raise HTTPException(404, "This version has no file (it was deleted in that commit).")
    return text, it["name"]


@router.get("/parts/{name}/history/{sha}")
async def part_version(name: str, sha: str):
    text, old = await version_text(name, sha)
    return {"source": text, "name": old}


@router.post("/parts/{name}/restore")
async def restore_part(name: str, request: Request):
    """Write an older version as a new change (no history rewrite), then rebuild in the background."""
    check_idle(name)
    src = part_src(name)
    sha = str((await request.json()).get("sha", ""))
    text, old = await version_text(name, sha)
    if old != name:  # the version is from before a rename: rename it again
        text = rename_source(text, old, name)
    async with LOCK:
        if src.read_text() != text:
            await run_in_threadpool(commit, f"Save part {name} before restore")
            src.write_text(text)
        date = time.strftime("%Y-%m-%d %H:%M", time.localtime(
            int(await run_in_threadpool(git, "show", "-s", "--format=%ct", sha) or 0)))
        await run_in_threadpool(commit, f"Restore part {name} to the version of {date} ({sha[:7]})")
    asyncio.create_task(build(name))
    return {"ok": True}


# --- projects -----------------------------------------------------------------

@router.post("/projects/{pid}/rename")
async def rename_project(pid: str, request: Request):
    """Change the shown name. The id (the folder name) stays, so chats keep their project."""
    check_idle()
    proj = core.load_project(pid)
    name = str((await request.json()).get("name", "")).strip()[:80]
    if not name:
        raise HTTPException(400, "Give the project a name.")
    old = proj["name"]
    async with LOCK:
        proj["name"] = name
        core.save_project(proj)
        await run_in_threadpool(commit, f"Rename project {old} to {name}")
    return {"ok": True}


@router.delete("/projects/{pid}")
async def delete_project(pid: str, parts: str = "", files: str = ""):
    """Delete a project. parts=unsort lets its parts go to "Not in a project".
    files=delete also deletes projects/<id>/files/. The part models stay in src/."""
    check_idle()
    proj = core.load_project(pid)
    d = inside(core.project_dir(pid))
    names = proj.get("parts", [])
    stored = [f for f in (d / "files").iterdir()] if (d / "files").is_dir() else []
    if names and parts != "unsort":
        raise HTTPException(409, f"The project has parts ({len(names)}). Move them first, or confirm.")
    if stored and files != "delete":
        raise HTTPException(409, f"The project has files ({len(stored)}). Confirm to delete them.")
    async with LOCK:
        await run_in_threadpool(commit, f"Save project {proj['name']} before delete")  # files go into git first
        shutil.rmtree(d)
        drop_plate_files(pid)
        await run_in_threadpool(commit, f"Delete project {proj['name']}")
    return {"ok": True}


# --- settings: printwright.json -------------------------------------------------

SETTING_KEYS = ["owner", "printer_name", "moonraker_url", "bed", "nozzle", "material"]
MATERIALS = ["PLA", "PETG", "ABS", "ASA", "TPU", "PLA+", "PA", "PC"]


def config_path() -> Path:
    return WS / "printwright.json"


def raw_config() -> dict:
    try:
        data = json.loads(config_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def valid_bed(bed) -> bool:
    return isinstance(bed, list) and len(bed) == 3 and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and 20 <= v <= 2000 for v in bed)


def setup_problem(raw: dict) -> str | None:
    """Why the first-run setup screen must show, or None."""
    url = str(raw.get("moonraker_url", "")).strip().rstrip("/")
    if not url or url == PLACEHOLDER_URL:
        return "The printer URL is the example placeholder."
    if not valid_bed(raw.get("bed")):
        return "The bed size is missing or not valid."
    return None


def clean_url(value) -> str:
    url = str(value or "").strip().rstrip("/")
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "The printer URL must start with http:// or https://, then the host.")
    if p.username or p.password or p.query or p.fragment:
        raise HTTPException(400, "Give the plain printer URL: no user name, password, ? or #.")
    if len(url) > 200:
        raise HTTPException(400, "The printer URL is too long.")
    try:
        p.port  # noqa: B018  (raises for a bad port)
    except ValueError:
        raise HTTPException(400, "The port in the printer URL is not valid.")
    return url


def text_value(body: dict, key: str, label: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(body.get(key, ""))).strip()
    if not value or len(value) > limit:
        raise HTTPException(400, f"{label}: give 1 to {limit} characters.")
    return value


@router.get("/settings")
async def get_settings():
    raw = raw_config()
    cfg = core.config()
    return {**{k: cfg.get(k) for k in SETTING_KEYS},
            "moonraker_url": raw.get("moonraker_url", cfg.get("moonraker_url")),
            "bed": raw.get("bed") if valid_bed(raw.get("bed")) else cfg.get("bed"),
            "problem": setup_problem(raw), "materials": MATERIALS,
            "env_url": bool(os.environ.get("PRINTER_URL")), "path": "printwright.json"}


@router.put("/settings")
async def save_settings(request: Request):
    """Write the setup values into printwright.json. Keep every other key as it is."""
    check_idle()
    body = await request.json()
    new = {
        "owner": text_value(body, "owner", "Your name", 60),
        "printer_name": text_value(body, "printer_name", "Printer name", 80),
        "moonraker_url": clean_url(body.get("moonraker_url")),
        "material": text_value(body, "material", "Material", 20),
    }
    try:
        bed = [round(float(v), 1) for v in body.get("bed", [])]
        nozzle = round(float(body.get("nozzle")), 2)
    except (TypeError, ValueError):
        raise HTTPException(400, "Bed size and nozzle must be numbers.")
    if not valid_bed(bed):
        raise HTTPException(400, "Bed size: give X, Y and Z, each 20 to 2000 mm.")
    if not 0.1 <= nozzle <= 2.0:
        raise HTTPException(400, "Nozzle: give 0.1 to 2.0 mm.")
    new["bed"] = [int(v) if v == int(v) else v for v in bed]
    new["nozzle"] = nozzle
    if new["moonraker_url"] == PLACEHOLDER_URL:
        raise HTTPException(400, "Give the real URL of your printer, not the example.")
    async with LOCK:
        raw = raw_config()
        if not raw and config_path().exists() and config_path().read_text().strip():
            raise HTTPException(409, "printwright.json is not valid JSON. Fix it by hand first.")
        raw.update(new)
        tmp = config_path().with_suffix(".json.tmp")
        text = json.dumps(raw, indent=2, ensure_ascii=False)
        text = re.sub(r"\[\s+([^\[\]{}]*?)\s+\]", lambda m: "[" + re.sub(r"\s+", " ", m.group(1)) + "]", text)
        tmp.write_text(text + "\n")  # short lists stay on one line, like "bed": [235, 230, 265]
        tmp.replace(config_path())
        await run_in_threadpool(commit, "Update settings", ["printwright.json"])
    return {"ok": True}


def probe(url: str) -> dict:
    req = urllib.request.Request(url + "/printer/info", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read(200_000))
        res = data.get("result", {}) if isinstance(data, dict) else {}
        if not isinstance(res, dict) or "state" not in res:
            return {"ok": False, "message": "The URL answers, but not like Moonraker."}
        return {"ok": True, "message": f"Connected. Klipper is {res.get('state')}"
                + (f", version {str(res.get('software_version'))[:40]}." if res.get("software_version") else ".")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": f"The printer answered with HTTP {e.code}."}
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return {"ok": False, "message": f"No connection: {str(reason)[:120]}"}
    except ValueError:
        return {"ok": False, "message": "The URL answers, but not with JSON. Is it Moonraker?"}


@router.post("/settings/test")
async def test_printer(request: Request):
    """GET <url>/printer/info with a short timeout. Only reads; sends nothing to the printer."""
    url = clean_url((await request.json()).get("moonraker_url"))
    return await run_in_threadpool(probe, url)
