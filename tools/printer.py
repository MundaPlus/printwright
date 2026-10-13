#!/usr/bin/env python3
"""Talk to a Klipper printer through Moonraker.

Moonraker may also show an OctoPrint-style API. That is a compat layer; this
script uses the native Moonraker API. It sends no API key: use it on a trusted LAN.
The printer URL and the file prefix come from printwright.json in the workspace
(PRINTWRIGHT_WORKSPACE, else the current folder). PRINTER_URL overrides the URL.

  printer.py status                 printer state, temperatures, current job
  printer.py upload FILE.gcode      upload only; you start the print yourself
  printer.py print FILE.gcode       upload and start (only when the printer is idle)
  printer.py start NAME             start a file already on the printer
  printer.py cancel                 cancel the current print
  printer.py estimate FILE.gcode    real print time: Orca estimate x learned factor + warm-up;
                                    writes FILE.json next to the G-code for the bed view
"""
import json
import os
import re
import statistics
import sys
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import workspace  # noqa: E402  (lib/workspace.py)


def settings():
    """(printer URL, file prefix) from printwright.json. Read on each call: edits apply at once."""
    cfg = workspace.load_config(workspace.find_workspace())
    host = os.environ.get("PRINTER_URL") or cfg["moonraker_url"]
    return host.rstrip("/"), str(cfg["file_prefix"])


def prefix():
    """Marks the files this tool uploads, next to files you upload yourself."""
    return settings()[1]


def api(path, data=None, headers=None, method=None, timeout=30):
    req = urllib.request.Request(settings()[0] + path, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["result"]


def status():
    q = ("print_stats&heater_bed=temperature,target&extruder=temperature,target"
         "&virtual_sdcard=progress,file_position")
    s = api("/printer/objects/query?" + q)["status"]
    ps = s["print_stats"]
    info = ps.get("info") or {}
    return {
        "state": ps["state"],
        "file": ps["filename"],
        "progress": round(100 * s["virtual_sdcard"]["progress"], 1),
        "nozzle": f'{s["extruder"]["temperature"]:.0f}/{s["extruder"]["target"]:.0f} C',
        "bed": f'{s["heater_bed"]["temperature"]:.0f}/{s["heater_bed"]["target"]:.0f} C',
        "message": ps["message"],
        # Added for the layer view; older callers ignore them.
        "print_duration": round(ps.get("print_duration") or 0, 1),  # seconds
        "file_position": s["virtual_sdcard"].get("file_position") or 0,  # bytes into the file
        "current_layer": info.get("current_layer"),  # from SET_PRINT_STATS_INFO, else None
        "total_layer": info.get("total_layer"),
    }


def upload(path, start=False):
    host, pre = settings()
    name = pre + os.path.basename(path)
    boundary = uuid.uuid4().hex
    with open(path, "rb") as f:
        payload = f.read()
    parts = []
    for key, value in (("root", "gcodes"), ("print", "true" if start else "false")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n".encode() + payload + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    req = urllib.request.Request(host + "/server/files/upload", data=b"".join(parts), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


WARMUP_MIN = 3.0  # heat, home, bed mesh before the first layer; median of past prints


def gcode_info(path):
    """Read Orca's summary comments from a sliced file."""
    info = {}
    with open(path, errors="replace") as f:
        for line in f:
            if not line.startswith(";"):
                continue
            m = re.match(r"; estimated printing time \(normal mode\) = (.+)", line)
            if m:
                units = {"d": 1440, "h": 60, "m": 1, "s": 1 / 60}
                info["orca_min"] = sum(float(n) * units[u] for n, u in re.findall(r"(\d+)([dhms])", m.group(1)))
            for key in ("total filament used [g]", "filament_type", "layer_height"):
                if line.startswith(f"; {key} = "):
                    info[key] = line.split(" = ", 1)[1].strip()
    return info


def calibration():
    """Factor (real / Orca) from completed prints with our prefix, and warm-up from all prints."""
    pre = prefix()
    try:
        jobs = api("/server/history/list?limit=200&order=desc", timeout=10)["jobs"]
    except (OSError, ValueError, KeyError):
        return 1.0, 0, WARMUP_MIN
    done = [j for j in jobs if j["status"] == "completed" and j["print_duration"] > 0]
    ratios = [j["print_duration"] / j["metadata"]["estimated_time"] for j in done
              if j["filename"].startswith(pre) and (j.get("metadata") or {}).get("estimated_time")]
    warmups = [(j["total_duration"] - j["print_duration"]) / 60 for j in done[:30]]
    factor = statistics.median(ratios) if ratios else 1.0
    warmup = statistics.median(warmups) if warmups else WARMUP_MIN
    return factor, len(ratios), warmup


def estimate(path):
    info = gcode_info(path)
    factor, n, warmup = calibration()
    total = info["orca_min"] * factor + warmup
    result = {
        "total_min": round(total, 1),
        "orca_min": round(info["orca_min"], 1),
        "factor": round(factor, 3),
        "factor_from_prints": n,
        "warmup_min": round(warmup, 1),
        "grams": float(info.get("total filament used [g]", 0)),
        "material": info.get("filament_type", "?"),
        "layer_height": float(info.get("layer_height", 0)),
    }
    with open(os.path.splitext(path)[0] + ".json", "w") as f:
        json.dump(result, f, indent=1)
    return result


def fmt_min(minutes):
    h, m = divmod(round(minutes), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def require_idle():
    st = status()
    if st["state"] in ("printing", "paused"):
        sys.exit(f"Printer is busy ({st['state']}: {st['file']}). Nothing started.")


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, args = argv[0], argv[1:]
    # The web app runs Claude with PRINT_WEB_AGENT=1. Only the Print button may move the printer.
    if os.environ.get("PRINT_WEB_AGENT") and cmd in ("upload", "print", "start", "cancel"):
        sys.exit(f"'{cmd}' is blocked for the chat agent. The user prints with the Print button in the web app.")
    if cmd == "status":
        print(json.dumps(status(), indent=1))
    elif cmd == "upload":
        print(json.dumps(upload(args[0]), indent=1))
    elif cmd == "print":
        require_idle()
        print(json.dumps(upload(args[0], start=True), indent=1))
    elif cmd == "start":
        require_idle()
        api("/printer/print/start?filename=" + urllib.parse.quote(args[0]), data=b"", method="POST")
        print("started", args[0])
    elif cmd == "estimate":
        r = estimate(args[0])
        basis = f"factor {r['factor']} from {r['factor_from_prints']} prints" if r["factor_from_prints"] else "not calibrated yet"
        print(f"print time: ~{fmt_min(r['total_min'])} "
              f"(Orca {fmt_min(r['orca_min'])}, {basis}, + {r['warmup_min']} min warm-up) | "
              f"{r['grams']} g {r['material']} | {r['layer_height']} mm layers")
    elif cmd == "cancel":
        api("/printer/print/cancel", data=b"", method="POST")
        print("cancelled")
    else:
        sys.exit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main(sys.argv[1:])
