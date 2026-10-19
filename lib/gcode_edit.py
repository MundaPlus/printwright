"""Edit an imported G-code file: temperatures, fan, speed, flow, pauses.

A G-code part (src/<part>.py with "# printwright: gcode") loads the imported file
from imports/, applies its edits and saves gcode/<part>.gcode. The imported file
never changes, so each edit is one line in the part file and is easy to undo:

    g = GCode.load("../imports/<part>.gcode", __file__)
    g.nozzle_temp(215, from_layer=2)
    g.pause_at_layer(12, "Change filament")
    g.save("../gcode/<part>.gcode", __file__)

Layers count from 1 (the first layer). They come from the slicer's layer marks:
";LAYER_CHANGE" (OrcaSlicer, PrusaSlicer, Bambu Studio) or ";LAYER:" (Cura).
An edit "from layer N" goes in at the start of layer N and also changes the
slicer's own commands of the same kind after that point. Commands before it stay,
so the start G-code (heat up, probe, purge) keeps its values.

save() also writes gcode/<part>.meta.json: size of the printed part, slicer,
printer, layers, the edits and warnings (outside the bed, other nozzle).
Stdlib only: the system Python runs it (tools/make.sh).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .workspace import find_workspace, load_config  # a part file imports this as lib.gcode_edit

PAUSE = "PAUSE"  # Klipper's pause macro ([pause_resume]); RESUME in the printer UI goes on
_CMD = re.compile(r"^(M10[49]|M1[49]0|M106)\b([^;]*)", re.I)
_S = re.compile(r"\bS(\d+(?:\.\d+)?)", re.I)
_AXES = re.compile(r"([XYZE])(-?\d*\.?\d+)", re.I)


def _path(rel: str, here: str) -> Path:
    return (Path(here).parent / rel).resolve()


class GCode:
    def __init__(self, lines: list[str], name: str):
        self.lines = lines
        self.name = name
        self.inserts: dict[int, list[str]] = {}  # line index -> lines to put before it
        self.edits: list[str] = []
        self.layers = self._find_layers()

    @classmethod
    def load(cls, rel: str, here: str) -> "GCode":
        path = _path(rel, here)
        return cls(path.read_text(errors="replace").splitlines(), path.name)

    # --- layers ---------------------------------------------------------------

    def _find_layers(self) -> list[int]:
        """Index of the first line of each layer (after its mark and the comments below it)."""
        starts = []
        for i, line in enumerate(self.lines):
            if line.startswith(";LAYER_CHANGE") or line.startswith(";LAYER:"):
                j = i + 1
                while j < len(self.lines) and self.lines[j].startswith(";") and \
                        not self.lines[j].startswith((";LAYER_CHANGE", ";LAYER:", ";TYPE:")):
                    j += 1
                starts.append(j)
        return starts

    def _start(self, layer: int) -> int:
        if not self.layers:
            raise ValueError(f"{self.name} has no layer marks (;LAYER_CHANGE or ;LAYER:). Layer edits need them.")
        if not 1 <= layer <= len(self.layers):
            raise ValueError(f"Layer {layer} does not exist. {self.name} has layers 1 to {len(self.layers)}.")
        return self.layers[layer - 1]

    def _insert(self, layer: int, lines: list[str], note: str) -> int:
        at = self._start(layer)
        self.inserts.setdefault(at, []).extend(f"{ln} ; printwright: {note}" for ln in lines)
        self.edits.append(note)
        return at

    def _replace_after(self, at: int, codes: tuple[str, ...], value: float):
        """Set S of the slicer's own codes after line `at` to value. S0 (off) stays."""
        for i in range(at, len(self.lines)):
            m = _CMD.match(self.lines[i])
            if not m or m.group(1).upper() not in codes:
                continue
            s = _S.search(m.group(2))
            if s and float(s.group(1)) > 0:
                self.lines[i] = self.lines[i][:m.start(2)] + _S.sub(f"S{value:g}", m.group(2), count=1) + \
                    self.lines[i][m.end(2):]

    # --- edits ----------------------------------------------------------------

    def nozzle_temp(self, temp: float, from_layer: int = 1):
        """Nozzle temperature in °C from this layer on. Layer 1 waits until it is hot."""
        code = "M109" if from_layer == 1 else "M104"
        at = self._insert(from_layer, [f"{code} S{temp:g}"], f"nozzle {temp:g} °C from layer {from_layer}")
        self._replace_after(at, ("M104", "M109"), temp)
        return self

    def bed_temp(self, temp: float, from_layer: int = 1):
        """Bed temperature in °C from this layer on. Layer 1 waits until it is hot."""
        code = "M190" if from_layer == 1 else "M140"
        at = self._insert(from_layer, [f"{code} S{temp:g}"], f"bed {temp:g} °C from layer {from_layer}")
        self._replace_after(at, ("M140", "M190"), temp)
        return self

    def fan(self, percent: float, from_layer: int = 1):
        """Part fan in % from this layer on. The slicer's "fan off" commands stay."""
        s = round(255 * max(0.0, min(100.0, percent)) / 100)
        at = self._insert(from_layer, [f"M106 S{s}"], f"fan {percent:g} % from layer {from_layer}")
        self._replace_after(at, ("M106",), s)
        return self

    def speed(self, percent: float, from_layer: int = 1):
        """Speed factor in % (M220) from this layer on. 100 is the sliced speed."""
        self._insert(from_layer, [f"M220 S{percent:g}"], f"speed {percent:g} % from layer {from_layer}")
        return self

    def flow(self, percent: float, from_layer: int = 1):
        """Extrusion factor in % (M221) from this layer on. 100 is the sliced flow."""
        self._insert(from_layer, [f"M221 S{percent:g}"], f"flow {percent:g} % from layer {from_layer}")
        return self

    def pause_at_layer(self, layer: int, message: str = "Paused"):
        """Pause before this layer starts, for example to change the filament colour."""
        msg = re.sub(r"[;\n\r]", " ", message)[:60]
        self._insert(layer, [f"M117 {msg}", PAUSE], f"pause before layer {layer}: {msg}")
        return self

    def pause_at_height(self, z: float, message: str = "Paused"):
        """Pause before the first layer that starts at or above z mm."""
        return self.pause_at_layer(self.layer_at(z), message)

    def insert_at_layer(self, layer: int, *lines: str):
        """Put your own G-code lines at the start of this layer."""
        self._insert(layer, [ln for ln in lines if ln.strip()], f"custom G-code at layer {layer}")
        return self

    def layer_at(self, z: float) -> int:
        """Number of the first layer whose Z is at or above z mm."""
        for n, at in enumerate(self.layers, 1):
            for line in self.lines[max(0, at - 6):at + 40]:
                m = re.match(r";Z:(\S+)|G[01]\b[^;]*\bZ(-?\d*\.?\d+)", line)
                if m and float(m.group(1) or m.group(2)) >= z - 1e-6:
                    return n
                if m:
                    break
        raise ValueError(f"No layer at or above Z={z:g} mm in {self.name}.")

    # --- output ---------------------------------------------------------------

    def text(self) -> str:
        out = []
        for i, line in enumerate(self.lines):
            out.extend(self.inserts.get(i, ()))
            out.append(line)
        return "\n".join(out) + "\n"

    def save(self, rel: str, here: str):
        path = _path(rel, here)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.text())
        tmp.replace(path)
        meta = self.meta(here)
        path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=1) + "\n")
        print(f"wrote G-code: {path.name} ({len(self.layers)} layers, {len(self.edits)} edits)")
        for w in meta["warnings"]:
            print(f"warning: {w}")

    def meta(self, here: str) -> dict:
        """Size of the printed part, slicer facts, the edits and warnings."""
        head = {}
        for line in self.lines[:400] + self.lines[-1500:]:
            for key, pat in (("slicer", r"^; generated by (.+?)(?: on \d{4}.*)?$|^;Generated with (.+)$"),
                             ("printer_model", r"^; printer_model = (.+)$|^;TARGET_MACHINE\.NAME:(.+)$"),
                             ("nozzle", r"^; nozzle_diameter = ([\d.]+)"),
                             ("material", r"^; filament_type = (.+)$")):
                m = re.match(pat, line)
                if m and key not in head:
                    head[key] = next(g for g in m.groups() if g).strip()
        lo, hi = [float("inf")] * 3, [float("-inf")] * 3
        pos = {"X": 0.0, "Y": 0.0, "Z": 0.0, "E": 0.0}
        rel_xyz, rel_e = False, False
        first = self.layers[0] if self.layers else 0  # the start G-code (purge line) does not count
        for i, line in enumerate(self.lines):
            code = line.split(";", 1)[0].strip().upper()
            if not code:
                continue
            cmd = code.split()[0]
            if cmd in ("G0", "G1", "G2", "G3"):
                new = dict(pos)
                for ax, val in _AXES.findall(code):
                    v = float(val)
                    if ax == "E":
                        new["E"] = pos["E"] + v if rel_e else v
                    else:
                        new[ax] = pos[ax] + v if rel_xyz else v
                if i >= first and new["E"] > pos["E"] + 1e-5 and (new["X"], new["Y"]) != (pos["X"], pos["Y"]):
                    for k, ax in enumerate("XYZ"):
                        lo[k] = min(lo[k], pos[ax], new[ax])
                        hi[k] = max(hi[k], pos[ax], new[ax])
                pos = new
            elif cmd == "G90":
                rel_xyz = False
            elif cmd == "G91":
                rel_xyz = True
            elif cmd == "M82":
                rel_e = False
            elif cmd == "M83":
                rel_e = True
            elif cmd == "G92":
                for ax, val in _AXES.findall(code):
                    pos[ax] = float(val)
        size = [round(hi[k] - lo[k], 2) for k in range(2)] + [round(hi[2], 2)] if lo[0] != float("inf") else None
        warnings = []
        try:
            ws = find_workspace(hints=[Path(here).resolve().parent.parent])
            cfg = load_config(ws)
        except SystemExit:
            cfg = None
        if cfg and size:
            bx, by, bz = cfg["bed"]
            if lo[0] < -1 or lo[1] < -1 or hi[0] > bx + 1 or hi[1] > by + 1:
                warnings.append(f"The print goes outside the bed: X {lo[0]:.0f} to {hi[0]:.0f}, "
                                f"Y {lo[1]:.0f} to {hi[1]:.0f} mm, bed {bx:g} x {by:g} mm.")
            if hi[2] > bz:
                warnings.append(f"The print is {hi[2]:.0f} mm high. The printer takes {bz:g} mm.")
        if cfg and head.get("nozzle"):
            try:
                if abs(float(head["nozzle"]) - cfg["nozzle"]) > 0.01:
                    warnings.append(f"Sliced for a {head['nozzle']} mm nozzle. The printer has {cfg['nozzle']:g} mm.")
            except ValueError:
                pass
        if not self.layers:
            warnings.append("No layer marks found: the layer view and layer edits do not work.")
        return {"source": self.name, **head, "layers": len(self.layers), "size": size,
                "min": [round(v, 2) for v in lo] if size else None, "edits": self.edits, "warnings": warnings}

