#!/usr/bin/env bash
# Create a Printwright workspace, or repair an existing one.
# Usage: tools/init-workspace.sh [--examples] <dir>
#   --examples   also copy the example parts (demo_box, test_coaster) and the "tests" project
#
# Safe to run again: it only adds what is missing. It never overwrites your files.
# The links tools -> <app>/tools and src/lib -> <app>/lib are re-pointed when the app moved.
# At the end it runs tools/check-setup.sh and warns when a program is missing.
set -euo pipefail
APP="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"

EXAMPLES=0
DIR=""
for arg in "$@"; do
  case "$arg" in
    --examples) EXAMPLES=1 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 1 ;;
    *) [ -z "$DIR" ] || { echo "give one directory" >&2; exit 1; }; DIR="$arg" ;;
  esac
done
[ -n "$DIR" ] || { echo "usage: tools/init-workspace.sh [--examples] <dir>" >&2; exit 1; }

mkdir -p "$DIR"
WS="$(cd "$DIR" && pwd -P)"
case "$WS/" in "$APP/"*) echo "The workspace must be outside the app folder ($APP)." >&2; exit 1 ;; esac

say() { echo "  $*"; }
# copy_new SRC DEST: copy only when DEST does not exist.
copy_new() {
  if [ -e "$2" ] || [ -L "$2" ]; then return 0; fi
  mkdir -p "$(dirname "$2")"
  cp "$1" "$2"
  say "added ${2#"$WS"/}"
}
# link_app TARGET LINK: make LINK point at TARGET. Never replaces a real file or folder.
link_app() {
  local target="$1" link="$2"
  if [ -L "$link" ]; then
    [ "$(readlink "$link")" = "$target" ] && return 0
    rm "$link"
    ln -s "$target" "$link"
    say "re-pointed ${link#"$WS"/} -> $target"
  elif [ -e "$link" ]; then
    echo "  WARNING: ${link#"$WS"/} is a real file or folder, not a link. Move it away and run this again." >&2
  else
    ln -s "$target" "$link"
    say "linked ${link#"$WS"/} -> $target"
  fi
}

echo "Workspace: $WS"
mkdir -p "$WS"/{src,projects,STEP,STL,GLB,gcode,tmp,.printwright}
copy_new "$APP/examples/printwright.example.json" "$WS/printwright.json"
copy_new "$APP/templates/CLAUDE.workspace.md" "$WS/CLAUDE.md"
copy_new "$APP/templates/claude-settings.json" "$WS/.claude/settings.json"
copy_new "$APP/templates/gitignore" "$WS/.gitignore"
link_app "$APP/tools" "$WS/tools"
link_app "$APP/lib" "$WS/src/lib"

if [ "$EXAMPLES" = 1 ]; then
  for f in "$APP"/examples/src/*.py; do copy_new "$f" "$WS/src/$(basename "$f")"; done
  copy_new "$APP/examples/projects/tests/project.json" "$WS/projects/tests/project.json"
fi

if [ ! -e "$WS/.git" ]; then
  git -C "$WS" init -q
  say "git repo created (no commit yet)"
fi

# Check the programs, but only warn: you can create a workspace before you install them.
if [ -z "${PRINTWRIGHT_SKIP_CHECK:-}" ] && ! "$APP/tools/check-setup.sh"; then
  echo "WARNING: some programs are missing (see above). Builds, slicing or the chat fail until you install them." >&2
fi

echo "Done. Next:"
echo "  1. Edit $WS/printwright.json (owner, printer URL, bed size)."
echo "  2. cd $WS && tools/make.sh demo_box   (needs --examples, or your own part)"
