# Printwright workspace — describe a part, print it

The owner of this workspace describes an object in words. You design it, show it, slice it and send it to the printer.
Their name, the printer, the bed size and the material are in `printwright.json`. Read it first.

## Printer

- One Klipper printer with Moonraker at `moonraker_url` in `printwright.json`. There is no API key; the LAN is trusted.
- Moonraker may show an "OctoPrint" API. That is a compat layer. Use `tools/printer.py`, which calls the native Moonraker API.
- Bed: `bed` in `printwright.json` is the usable [x, y, z] in mm.
- Default material: `material` in `printwright.json`. The slicer presets set the temperatures and layers.
- Files this workspace uploads start with `file_prefix` (default `AI_`). Do not touch other files on the printer.

## Workflow

1. **Design.** Load the `text-to-cad:cad` skill. Write one model per part in `src/<part>.py` with `@step(out="../STEP/<part>.step")` and `@stl(out="../STL/<part>.stl")`. Add the bed view model `<part>_on_bed` (`@step(out="../STEP/<part>_on_bed.step")` and `@glb(out="../GLB/<part>_on_bed.glb")`, body `return on_bed(<part>(), "<part>")`, import `from lib.bed import on_bed`) and call both under `__main__`. Copy `src/demo_box.py` (or the app's `examples/src/demo_box.py`) as the pattern. Keep all sizes as named constants at the top of the file, in mm. Ask only about sizes that change the result. Use sensible defaults for the rest and state them.
2. **Build, slice, estimate.** `tools/make.sh <part>` builds the model, slices it, estimates the print time and refreshes the bed view label. Orca overrides go after the part name.
3. **Review.** Render `cadgen step snapshot STEP/<part>_on_bed.step tmp/<part>.png --camera top` and look at it yourself. Show the bed view with `cad_show` (absolute path to `STEP/<part>_on_bed.step`). It shows the part centred on the bed (10 mm grid, bed coordinates), X/Y/Z rulers beside the part with a red mark at the exact size, and a label with size, print time and grams. Use `STEP/<part>.step` when the user selects faces for edits.
4. **Printability.** Design for FDM with no supports where possible: flat base on the bed, overhangs ≤ 45°, walls ≥ 1.2 mm, holes for screws +0.2–0.3 mm. Use the `text-to-cad:dfam-check` skill for anything non-trivial.
5. **Adjust.** The user asks for changes in words ("10 mm taller", "add a hole"). Edit the constants or the geometry, then go back to step 2.
6. **Print time.** `tools/make.sh` prints it. `printer.py estimate` multiplies Orca's estimate by a factor learned from completed prints with the file prefix (Moonraker history) and adds the measured warm-up (~3 min: heat, home, bed mesh). Say "not calibrated yet" until the first such print finished.
7. **Print.** Run `python3 tools/printer.py status` first. Then ask the user with `AskUserQuestion`: show the part name, size, print time, grams and material. Only on "yes" run `python3 tools/printer.py print gcode/<part>.gcode`. The script refuses to start when the printer is busy. Other choices: `upload` only, or cancel.

## Projects and plates

- A project groups parts for one goal: `projects/<id>/project.json` lists its parts, ideas and plate.
  Part models stay in `src/`. Pictures and files the user adds are in `projects/<id>/files/`; read them.
- When you create a part outside the web app, add its name to the `parts` list of the right project.
- Several parts on one plate: `tools/plate.py plate_<id> <part>:<count> ...` arranges and slices them
  (OrcaSlicer). It writes `gcode/plate_<id>.gcode`, `.json` (time, grams) and `.layout.json`, and exits 2
  when the copies do not fit. Show the result and ask before printing, like a single part.

## Print settings

- Each part can have `presets/parts/<part>.json`. A plate has `presets/parts/plate_<project>.json`.
  The web app's Settings panel writes these files. Edit them when the user asks, for example
  "use tree supports" or "print it in PETG". Then run `tools/make.sh <part>` (or `tools/slice.sh STL/<part>.stl`).
- OrcaSlicer process keys, values as strings: `layer_height` (0.12, 0.16, 0.2, 0.24, 0.28),
  `sparse_infill_pattern` (grid, gyroid, cubic, honeycomb, lightning, rectilinear, line, adaptivecubic, ...),
  `sparse_infill_density` ("25%"), `wall_loops`, `top_shell_layers`, `bottom_shell_layers`,
  `enable_support` ("0"/"1"), `support_type` ("normal(auto)"/"tree(auto)"), `support_on_build_plate_only`,
  `support_threshold_angle`, `brim_type` ("no_brim"/"auto_brim"/"outer_only"), `brim_width`.
  OrcaSlicer turns an unknown pattern name into another pattern without a warning. Use only these names.
- `"material": "PETG"` picks `presets/filaments/petg.json` (from the app, or your own in `presets/filaments/`).
  Materials: PLA (`filament-1.json`), PETG, TPU. Without it, `material` in `printwright.json` applies.
- `"placement": {"rotate": [x, y, z], "auto_orient": false}` turns the part on the bed: X, then Y, then Z, in degrees.
  X and Y take 0, 90, 180 or 270. Use it to lay a part on another face instead of changing the model.
  The bed view, the size and the slicer use the same turn. `auto_orient` lets OrcaSlicer choose the face.
- A plate uses only its own file. OrcaSlicer cannot mix settings on one plate. Each part keeps its own turn.

## Rules

- Never start a print without the user's "yes" for that exact file in this conversation.
- If the part does not fit the bed, say so and propose a split or a smaller size. Do not scale silently.
- Another material: PLA, PETG and TPU are ready (see "Print settings"). For a new one, write a complete preset with the `text-to-cad:gcode` skill's `orca_presets.py` into `presets/filaments/<material>.json`, as the app's `tools/make_filaments.py` does for PETG and TPU. Check the temperatures before slicing.
- `tools/` and `src/lib/` are links into the Printwright app. Do not edit them here.

## Layout

- `printwright.json` config: owner, printer, Moonraker URL, bed size, nozzle, material, file prefix, presets
- `src/` models (source of truth) · `STEP/`, `STL/`, `GLB/` generated · `gcode/` sliced · `tmp/` snapshots
- `src/lib/bed.py` bed view: bed, grid, rulers, label (from the app)
- `tools/make.sh` build + slice + estimate + bed view in one step
- `tools/slice.sh` OrcaSlicer headless slice (Flatpak `com.orcaslicer.OrcaSlicer`, else Docker)
- `tools/plate.py` several parts on one plate: arrange, slice, layout for the preview
- `tools/printer.py` Moonraker: status, upload, print, start, cancel, estimate
- `tools/make_presets.py` write presets with the printer's real limits into `presets/`
- `projects/` project files (parts list, ideas, plate) and uploaded pictures
- `presets/parts/<part>.json` print settings per part or plate: process keys, material, placement (see "Print settings")
- `presets/filaments/<material>.json` your own filament presets (the app has PETG and TPU)
- `.printwright/` web app data (chats, Ideas runs); not in git
