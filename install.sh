#!/usr/bin/env bash
# Installs Routine Panel for this user:
#   1. checks for Python 3.10 or newer and for Google Chrome,
#   2. adds the launcher and the icon to the app menu (~/.local/share),
#   3. with --phone only: adds the service for the phone app (~/.config/systemd/user/routine-panel.service), which
#      starts with your login and serves the panel to paired phones over Tailscale and the home network. It only
#      reads, like the window; without a phone looking at it, it does next to nothing.
# Nothing else: no Python packages (Routine Panel uses only Python's standard library), no system packages. The
# window's Chrome profile is made on first use, in ~/.local/share/routine-panel. Undo with ./uninstall.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
APP_ID="store.themechanic.RoutinePanel"
# The name Chrome gives the window, from its address (routine_panel.HOST_NAME; measured 2026-11-16 on GNOME Wayland).
# GNOME shows the window under this launcher's icon only when StartupWMClass is exactly this; without it the window
# is a nameless "window:NNNN" with Chrome's icon.
WM_CLASS="chrome-routine-panel.localhost__-Default"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS="$DATA/applications"
ICONS="$DATA/icons/hicolor"
PY="$(command -v python3 || true)"
PHONE=no
for arg in "$@"; do
  case "$arg" in
    --phone) PHONE=yes ;;
    *) echo "Unknown option: $arg (the only one is --phone)" >&2; exit 2 ;;
  esac
done

if [ -z "$PY" ] || ! "$PY" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer was not found (python3). Install it, then run ./install.sh again." >&2
  exit 1
fi
if ! command -v google-chrome >/dev/null && ! command -v google-chrome-stable >/dev/null \
   && [ ! -x /opt/google/chrome/chrome ] && ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  echo "Note: neither Google Chrome nor Chromium was found. Routine Panel will open in a tab of your default browser."
  echo "      With Google Chrome (google.com/chrome) or Chromium it gets a window of its own, with its icon."
fi

escape() { printf '%s' "$1" | sed 's/[&|\\]/\\&/g'; }
mkdir -p "$APPS" "$ICONS/scalable/apps" "$ICONS/256x256/apps"
sed -e "s|@ROOT@|$(escape "$ROOT")|g" -e "s|@PYTHON@|$(escape "$PY")|g" -e "s|@WMCLASS@|$(escape "$WM_CLASS")|g" \
  "$ROOT/packaging/$APP_ID.desktop.in" > "$APPS/$APP_ID.desktop"
cp "$ROOT/routine_panel/web/icon.svg" "$ICONS/scalable/apps/$APP_ID.svg"
cp "$ROOT/packaging/icons/routine-panel.png" "$ICONS/256x256/apps/$APP_ID.png"
chmod +x "$ROOT/bin/routine-panel" "$ROOT/uninstall.sh"

command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -f -t "$ICONS" || true
command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS" || true

# --- 3. the service for phones -------------------------------------------------------------------------------------
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
if [ "$PHONE" = yes ] || [ -e "$UNITS/routine-panel.service" ]; then  # an installed service is kept up to date
  mkdir -p "$UNITS"
  sed -e "s|@ROOT@|$(escape "$ROOT")|g" -e "s|@PYTHON@|$(escape "$PY")|g" \
    "$ROOT/packaging/routine-panel.service.in" > "$UNITS/routine-panel.service"
  systemctl --user daemon-reload
  systemctl --user enable routine-panel.service >/dev/null 2>&1
  systemctl --user restart routine-panel.service
  echo "The service for phones runs, and starts with your login (systemctl --user status routine-panel)."
fi
echo "Installed. Open \"Routine Panel\" from the app menu."
