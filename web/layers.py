"""G-code layer view and live print progress.

The page shows the sliced G-code of a part or a plate layer by layer. This module
parses the G-code once into a compact per-layer format and keeps it next to the file:

  gcode/<name>.layers.json   index: layers, feature types, time marks
  gcode/<name>.layers.bin    gzip data: segments per type, byte-offset marks

A new parse runs when the G-code changes (size or mtime). The format:

- Each segment is one straight move: x1, y1, x2, y2 as int16 in units of "scale" mm,
  plus its sequence number (uint32, file order over all segments).
- Segments are grouped by feature type (OrcaSlicer ";TYPE:" comments, plus "Travel").
  Inside a type they keep the file order, so they are sorted by layer.
- Each layer has its Z, its height, the first sequence number and the byte offset
  of its ";LAYER_CHANGE" line.
- Marks map byte offsets in the file to sequence numbers. The printer reports its
  byte position (virtual_sdcard.file_position); the marks turn it into a layer and a move.

Command line (timing tests): python3 web/layers.py FILE.gcode
"""
from __future__ import annotations

import bisect
import gzip
import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
from array import array
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "lib"))
sys.path.insert(0, str(APP / "tools"))
import printer  # noqa: E402  (tools/printer.py)
import workspace  # noqa: E402  (lib/workspace.py)

VERSION = 1
NAME_RE = re.compile(r"^[a-z0-9_]+$")
MARK_EVERY = 64  # segments between two byte-offset marks
TRAVEL = "Travel"
ARC_STEP = 1.0  # mm per line piece when an arc (G2/G3) is split


# --- parser -------------------------------------------------------------------

class _Overflow(Exception):
    """A coordinate does not fit in int16 at this scale."""


def _parse(path: Path, scale: float) -> tuple[dict, bytes]:
    inv = 1.0 / scale
    types: dict[str, int] = {}
    coords: list[array] = []
    seqs: list[array] = []
    layers: list[list] = [[None, None, 0, 0]]  # z, height, first seq, byte offset
    first_marker = True
    marks = array("I")
    times: list[list] = []
    x = y = z = 0.0
    abs_e = False
    rel_xyz = False
    e_pos = 0.0
    cur = "Custom"
    tid = None
    seq = 0
    off = 0

    def type_id(name: str) -> int:
        i = types.get(name)
        if i is None:
            i = types[name] = len(coords)
            coords.append(array("h"))
            seqs.append(array("I"))
        return i

    def emit(t: int, x1: float, y1: float, x2: float, y2: float):
        nonlocal seq
        if seq % MARK_EVERY == 0:
            marks.append(off)
            marks.append(seq)
        try:
            coords[t].extend((round(x1 * inv), round(y1 * inv), round(x2 * inv), round(y2 * inv)))
        except OverflowError:
            raise _Overflow
        seqs[t].append(seq)
        seq += 1
        if layers[-1][0] is None and t != travel:
            layers[-1][0] = z  # no ";Z:" comment: use the Z of the first extrusion

    travel = type_id(TRAVEL)
    tid = type_id(cur)
    with path.open("rb") as f:
        for line in f:
            n = len(line)
            c = line[:1]
            if c == b";":
                if line.startswith(b";TYPE:"):
                    cur = line[6:].strip().decode(errors="replace") or "Custom"
                    tid = type_id(cur)
                elif line.startswith(b";LAYER_CHANGE") or line.startswith(b";LAYER:"):
                    if first_marker:
                        first_marker = False  # moves before the first layer belong to it
                    else:
                        layers.append([None, None, seq, off])
                elif line.startswith(b";Z:"):
                    layers[-1][0] = float(line[3:])
                elif line.startswith(b";HEIGHT:"):
                    layers[-1][1] = float(line[8:])
                off += n
                continue
            if c == b"G":
                code = line.split(b";", 1)[0].split()
                cmd = code[0] if code else b""
                if cmd in (b"G1", b"G0", b"G2", b"G3"):
                    nx, ny, nz, de = x, y, z, 0.0
                    i = j = 0.0
                    for p in code[1:]:
                        k = p[:1]
                        if k == b"X":
                            nx = float(p[1:]) + (x if rel_xyz else 0.0)
                        elif k == b"Y":
                            ny = float(p[1:]) + (y if rel_xyz else 0.0)
                        elif k == b"Z":
                            nz = float(p[1:]) + (z if rel_xyz else 0.0)
                        elif k == b"E":
                            v = float(p[1:])
                            if abs_e:
                                de, e_pos = v - e_pos, v
                            else:
                                de = v
                        elif k == b"I":
                            i = float(p[1:])
                        elif k == b"J":
                            j = float(p[1:])
                    z = nz
                    if nx != x or ny != y:
                        t = tid if de > 0 else travel
                        if cmd in (b"G2", b"G3") and (i or j):
                            cx, cy = x + i, y + j
                            r = math.hypot(i, j)
                            a0 = math.atan2(y - cy, x - cx)
                            a1 = math.atan2(ny - cy, nx - cx)
                            if cmd == b"G2":  # clockwise
                                while a1 >= a0:
                                    a1 -= 2 * math.pi
                            else:
                                while a1 <= a0:
                                    a1 += 2 * math.pi
                            steps = max(2, int(abs(a1 - a0) * r / ARC_STEP))
                            px, py = x, y
                            for s in range(1, steps + 1):
                                a = a0 + (a1 - a0) * s / steps
                                qx, qy = (cx + r * math.cos(a), cy + r * math.sin(a)) if s < steps else (nx, ny)
                                emit(t, px, py, qx, qy)
                                px, py = qx, qy
                        else:
                            emit(t, x, y, nx, ny)
                        x, y = nx, ny
                elif cmd == b"G92":
                    for p in code[1:]:
                        k = p[:1]
                        if k == b"E":
                            e_pos = float(p[1:])
                        elif k == b"X":
                            x = float(p[1:])
                        elif k == b"Y":
                            y = float(p[1:])
                        elif k == b"Z":
                            z = float(p[1:])
                elif cmd == b"G90":
                    rel_xyz = False
                elif cmd == b"G91":
                    rel_xyz = True
            elif c == b"M":
                if line.startswith(b"M73 "):
                    m = re.search(rb"\bR(\d+(?:\.\d+)?)", line)
                    if m:
                        times.append([off, float(m.group(1))])
                elif line.startswith(b"M82"):
                    abs_e = True
                elif line.startswith(b"M83"):
                    abs_e = False
            off += n

    # Fill missing Z and heights; drop empty trailing layers.
    prev = 0.0
    for lay in layers:
        if lay[0] is None:
            lay[0] = prev
        if lay[1] is None:
            lay[1] = round(lay[0] - prev, 4) or 0.2
        prev = lay[0]
    while len(layers) > 1 and layers[-1][2] >= seq:
        layers.pop()

    parts, table, pos = [], [], 0
    big = sys.byteorder == "big"
    for name, t in types.items():
        if not seqs[t]:
            continue
        for arr in (coords[t], seqs[t]):
            if big:
                arr.byteswap()
            parts.append(arr.tobytes())
        nbytes = len(coords[t]) * 2
        table.append({"name": name, "count": len(seqs[t]), "coords": pos, "seq": pos + nbytes})
        pos += nbytes + len(seqs[t]) * 4
    if big:
        marks.byteswap()
    parts.append(marks.tobytes())
    index = {
        "version": VERSION, "scale": scale, "segments": seq,
        "layers": [[round(a, 4), round(b, 4), s, o] for a, b, s, o in layers],
        "types": table, "marks": {"offset": pos, "count": len(marks) // 2},
        "times": times, "bytes": off,
    }
    return index, b"".join(parts)


def parse_gcode(path: Path) -> tuple[dict, bytes]:
    """Parse one G-code file. Returns the index and the raw (not compressed) data."""
    for scale in (0.01, 0.02, 0.05, 0.1):
        try:
            return _parse(path, scale)
        except _Overflow:
            continue
    raise ValueError("coordinates out of range")


# --- cache ----------------------------------------------------------------------

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _ws() -> Path:
    return workspace.find_workspace()


def gcode_path(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise HTTPException(404, "no such G-code")
    return _ws() / "gcode" / f"{name}.gcode"


def load_index(name: str) -> dict:
    """The layer index of gcode/<name>.gcode. Parses and caches it when it is missing or old."""
    gc = gcode_path(name)
    if not gc.is_file():
        raise HTTPException(404, "Not sliced yet.")
    idx_p = gc.with_suffix(".layers.json")
    bin_p = gc.with_suffix(".layers.bin")
    with _locks_guard:
        lock = _locks.setdefault(name, threading.Lock())
    with lock:
        st = gc.stat()
        key = {"gcode_mtime": st.st_mtime_ns, "gcode_size": st.st_size}
        if idx_p.is_file() and bin_p.is_file():
            try:
                index = json.loads(idx_p.read_text())
                if index.get("version") == VERSION and all(index.get(k) == v for k, v in key.items()):
                    return index
            except ValueError:
                pass
        t0 = time.perf_counter()
        index, data = parse_gcode(gc)
        t1 = time.perf_counter()
        packed = gzip.compress(data, compresslevel=5, mtime=0)
        index.update(key, name=name, parse_ms=round((t1 - t0) * 1000),
                     pack_ms=round((time.perf_counter() - t1) * 1000), data_bytes=len(packed))
        tmp = bin_p.with_suffix(".tmp")
        tmp.write_bytes(packed)
        tmp.replace(bin_p)
        tmp = idx_p.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, separators=(",", ":")))
        tmp.replace(idx_p)
        return index


# --- print job --------------------------------------------------------------------

_size_cache: dict[str, int | None] = {}


def remote_size(filename: str) -> int | None:
    """Size of a file on the printer, from Moonraker's metadata. Cached per file name."""
    if filename not in _size_cache:
        try:
            meta = printer.api("/server/files/metadata?filename=" + urllib.parse.quote(filename), timeout=5)
            _size_cache[filename] = int(meta.get("size") or 0) or None
        except (OSError, ValueError, KeyError, TypeError):
            return None  # try again next time
    return _size_cache[filename]


def local_name(filename: str) -> str | None:
    """Workspace G-code name for a printer file name: <prefix><name>.gcode -> <name>."""
    base = (filename or "").rsplit("/", 1)[-1]
    prefix = printer.prefix()
    if not prefix or not base.startswith(prefix) or not base.endswith(".gcode"):
        return None
    name = base[len(prefix):-len(".gcode")]
    return name if NAME_RE.match(name) and (_ws() / "gcode" / f"{name}.gcode").is_file() else None


def job_position(index: dict, pos: int, current_layer: int | None = None) -> dict:
    """Layer and move for a byte position in the file."""
    layers = index["layers"]
    li = max(0, bisect.bisect_right([lay[3] for lay in layers], pos) - 1)
    if current_layer and 1 <= current_layer <= len(layers):
        li = current_layer - 1  # the printer counts from 1 (SET_PRINT_STATS_INFO)
    return {"layer": li, "z": layers[li][0]}


def time_left(index: dict, pos: int, factor: float) -> float | None:
    """Minutes left from the slicer's M73 R marks at this byte position, times the learned factor."""
    times = index.get("times") or []
    i = bisect.bisect_right([t[0] for t in times], pos) - 1
    return round(times[i][1] * factor, 1) if i >= 0 else None


router = APIRouter()


@router.get("/api/printjob")
async def printjob():
    """What the printer prints now. When it is a file from this workspace, also its layer."""
    try:
        st = await run_in_threadpool(printer.status)
    except (OSError, ValueError, KeyError, TypeError, SystemExit) as e:
        return {"state": "offline", "message": str(e)[:200]}
    out = {k: st.get(k) for k in ("state", "file", "progress", "print_duration", "file_position",
                                  "current_layer", "total_layer")}
    busy = st["state"] in ("printing", "paused")
    progress = (st.get("progress") or 0) / 100
    dur = st.get("print_duration") or 0
    if busy and progress > 0.02 and dur > 0:
        out["time_left_min"] = round(dur / progress * (1 - progress) / 60, 1)
        out["time_source"] = "printer"
    name = local_name(st.get("file") or "")
    if not name or not busy:
        return out
    try:
        index = await run_in_threadpool(load_index, name)
    except (HTTPException, OSError, ValueError):
        return out
    pos = int(st.get("file_position") or 0)
    size = await run_in_threadpool(remote_size, st["file"])
    out["exact"] = size == index["gcode_size"]
    if size and not out["exact"]:
        pos = int(pos * index["gcode_size"] / size)  # the file changed after the upload: estimate
    stats_p = _ws() / "gcode" / f"{name}.json"
    try:
        factor = float(json.loads(stats_p.read_text()).get("factor", 1.0))
    except (OSError, ValueError, AttributeError):
        factor = 1.0
    left = time_left(index, pos, factor)
    if left is not None:
        out["time_left_min"] = left
        out["time_source"] = "slicer"
    out.update(name=name, pos=pos, gcode_mtime=index["gcode_mtime"],
               **job_position(index, pos, st.get("current_layer")))
    return out


@router.get("/api/layers/{name}")
async def layers_index(name: str):
    return await run_in_threadpool(load_index, name)


@router.get("/api/layers/{name}/data")
async def layers_data(name: str):
    """The segment data. It is stored gzip-compressed; the browser unpacks it."""
    await run_in_threadpool(load_index, name)
    data = gcode_path(name).with_suffix(".layers.bin").read_bytes()
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Encoding": "gzip", "Cache-Control": "no-cache"})


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        t0 = time.perf_counter()
        idx, raw = parse_gcode(Path(arg))
        t1 = time.perf_counter()
        packed = gzip.compress(raw, compresslevel=5, mtime=0)
        t2 = time.perf_counter()
        print(f"{arg}: {os.path.getsize(arg) / 1e6:.1f} MB, {len(idx['layers'])} layers, "
              f"{idx['segments']} segments, parse {t1 - t0:.2f} s, gzip {t2 - t1:.2f} s, "
              f"data {len(raw) / 1e6:.1f} MB -> {len(packed) / 1e6:.1f} MB")
        print("  types:", ", ".join(f"{t['name']} {t['count']}" for t in idx["types"]))
