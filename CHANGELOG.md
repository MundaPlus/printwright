# Changelog

## 2026-10-19
- Use cadgen 0.7.24, was 0.7.21 (`web/app.py`, `web/slicing.py`, `tools/make.sh`) — the text-to-cad plugin moved to 0.7.24. The pin must match the plugin, so the app and the plugin share one cadgen install and build daemon. New in 0.7.22–0.7.24: Quick Edit sends several refs on one line, approximate measurements on meshes, new mesh snapshot options.

## 2026-10-15
- Plate layout choices: auto (OrcaSlicer packs tight, optional turn) or grid (rows with spacing, centre or a corner, optional 90° turn); stored as `arrange` in the plate's settings file, set in the plate settings panel, marked stale when it changed (`lib/part_settings.py`, `tools/plate.py`, `web/slicing.py`, `web/static/slicing.js`, `FEATURES.md`, `templates/CLAUDE.workspace.md`) — users want to choose how the copies sit on the plate. OrcaSlicer's command line has no spacing option, so the grid places the copies itself and slices with `--arrange 0`.
- First layer gets a 5 mm auto brim by default (`presets/process.json`: `brim_type` auto_brim, `brim_width` 5, was 0) — parts and plates had only the skirt, which gives little bed grip.

## 2026-10-13
- First public release, Printwright 1.0, with all the changes below (`web/pyproject.toml`) — the app is ready for other users.
- Short README, full feature list in FEATURES.md, screenshots, and credits for text-to-cad, OrcaSlicer and three.js (`README.md`, `FEATURES.md`, `docs/screenshots/`, `CLAUDE.md`) — new users need a short start page and a complete, correct feature list.
- Setup check for Claude Code, the text-to-cad plugin, uv, OrcaSlicer, Python and git; `deploy/install.sh` stops when one is missing, `tools/init-workspace.sh` warns; nothing is installed for you (`tools/check-setup.sh`, `deploy/install.sh`, `tools/init-workspace.sh`) — a missing plugin or slicer failed late and without a clear message.
- Manage parts and projects in the page: rename, move, restore an older version and delete parts; rename and delete projects; a first-run setup screen and a ⚙ Settings dialog for `printwright.json`; phone layout fixes (`web/manage.py`, `web/static/manage.js`, `web/static/manage.css`, `web/static/style.css`) — users could only change parts through the chat, and a new workspace needed a hand-edited config.
- Print settings per part and per plate: material (PLA, PETG, TPU), layer height, infill, walls, shells, supports, brim and the turn on the bed, with re-slice; the print dialog reads the material from the G-code (`lib/part_settings.py`, `web/slicing.py`, `web/static/slicing.js`, `tools/slice.sh`, `tools/plate.py`, `tools/make_filaments.py`, `presets/filaments/`) — users can change how a part prints without asking Claude or editing JSON.
- G-code layer view with a layer slider, feature colours and a legend; live print progress with time left and "Follow print" (`web/layers.py`, `web/static/layers.js`, `tools/printer.py`) — users can check the toolpaths before a print and see where a running print is.
- The app is split from the user's parts: parts, projects, config and generated files live in a workspace that links `tools` and `src/lib` to the app (`lib/workspace.py`, `tools/init-workspace.sh`, `templates/`, `examples/`) — the app can be public while each user keeps their parts private.
- Printer, bed size, owner name, file prefix, material and presets come from `printwright.json` (`lib/workspace.py`, `web/app.py`, `examples/printwright.example.json`) — every user has a different printer.
- No built-in login; the README explains how to run the app safely (`web/`, `deploy/`, `README.md`) — a reverse proxy does auth better, and a LAN-only tool needs no password.
