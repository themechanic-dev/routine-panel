#!/usr/bin/env bash
# Makes the Linux download: routine-panel-linux-<version>.tar.gz with the app, its launcher files and the
# install/uninstall scripts (no tests, no Android or Windows sources). Run from the project's folder.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION=$(python3 -I -c "import sys; sys.path.insert(0, '$ROOT'); import routine_panel; print(routine_panel.__version__)")
OUT="${1:-$ROOT/release-build/linux}"
NAME="routine-panel-$VERSION"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/$NAME/packaging" "$OUT"
cp -a "$ROOT/routine_panel" "$ROOT/bin" "$ROOT/install.sh" "$ROOT/uninstall.sh" "$ROOT/LICENSE" "$T/$NAME/"
[ -f "$ROOT/README.md" ] && cp "$ROOT/README.md" "$T/$NAME/"
cp -a "$ROOT/packaging/icons" "$ROOT/packaging/store.themechanic.RoutinePanel.desktop.in" \
      "$ROOT/packaging/routine-panel.service.in" "$T/$NAME/packaging/"
find "$T/$NAME" -name __pycache__ -prune -exec rm -rf {} +
tar --owner=0 --group=0 --sort=name -czf "$OUT/routine-panel-linux-$VERSION.tar.gz" -C "$T" "$NAME"
echo "$OUT/routine-panel-linux-$VERSION.tar.gz"
