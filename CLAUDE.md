# Printwright — notes for contributors

This repo is the public Printwright app. It holds no user parts.
User parts live in a separate workspace (see FEATURES.md, "Projects & files").

## Rules

- Keep the app free of personal data: no names, LAN addresses, hostnames, emails or user files.
  Check with `git grep -n -i -E "192\.168|@gmail|password"` before you commit.
- The app and the workspace are separate folders. Never find the workspace with `Path(__file__).resolve()`:
  that points into the app. Use `lib/workspace.py` (`find_workspace()`, `load_config()`, `presets_dir()`).
- Tools find the app from their own real location (`realpath "$0"` in bash, `Path(__file__).resolve()` in Python),
  because a workspace reaches them through the `tools` symlink.
- Tools that the system Python runs (`tools/*.py`, `lib/workspace.py`) use the stdlib only and must work on Python 3.10.
  `lib/bed.py` runs under cadgen's Python 3.13.
- Every value that depends on the printer comes from `printwright.json`. Add new keys to `DEFAULTS` in
  `lib/workspace.py`, to `examples/printwright.example.json` and to the table in FEATURES.md ("Setup").
- The chat agent must never print. Keep both guards: the deny list in `web/app.py` and the
  `PRINT_WEB_AGENT` check in `tools/printer.py`.
- Keep `SYSTEM` in `web/app.py` in line with `templates/CLAUDE.workspace.md`.
- Never install the text-to-cad plugin or other programs from a script. `tools/check-setup.sh` only checks and prints
  the install commands. Keep the credit to text-to-cad (earthtojake), OrcaSlicer and three.js in README.md.
- Keep README.md short. New features go into FEATURES.md, grouped like the sections there.
- Write docs, comments and commit messages in short, simple sentences (ASD-STE100), active voice.
- Add a `CHANGELOG.md` entry for every change: `## YYYY-MM-DD`, then `- what (path) — why`.

## Test a change

1. `tools/init-workspace.sh --examples /tmp/pw-ws` (safe to run again).
2. In `/tmp/pw-ws`: `tools/make.sh demo_box`, then `tools/plate.py plate_tests demo_box:2`.
3. Web app: `PRINTWRIGHT_WORKSPACE=/tmp/pw-ws PRINT_WEB_NO_GIT=1 uv run --project web uvicorn app:app --app-dir web --port 8431`.
   Set `moonraker_url` to a dummy URL so nothing reaches a real printer.
4. Syntax: `python3 -m py_compile web/*.py tools/*.py lib/*.py`, `node --check` on each `web/static/*.js`, `bash -n tools/*.sh deploy/install.sh`.

## Layout

- `web/app.py` FastAPI: parts, projects, files, plate, printer, chat (`claude -p` stream), Ideas
- `web/static/` page: `index.html`, `app.js` (three.js viewer, chat, Ideas, plate), `style.css`, vendored three.js
- `lib/workspace.py` workspace and config; `lib/bed.py` bed view for part models; `lib/part_settings.py` print settings per part (stdlib, also imported by `bed.py`)
- `tools/` build, slice, plate, printer, presets, Docker wrapper, workspace init, setup check (`check-setup.sh`)
- `templates/`, `examples/` files for new workspaces
- `presets/` default OrcaSlicer presets (Neptune 4 Pro, PLA, 0.20 mm)
- `deploy/` systemd user unit and installer

## Branches

`master` = releases, `develop` = day-to-day work.
