#!/usr/bin/env bash
# Removes what ./install.sh added: the launcher, the icon and (if it was added) the service for phones.
#   ./uninstall.sh         keeps the settings (~/.config/routine-panel) and the window's profile (~/.local/share/routine-panel)
#   ./uninstall.sh --all   removes those too
set -euo pipefail
APP_ID="store.themechanic.RoutinePanel"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
rm -f "$DATA/applications/$APP_ID.desktop" "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg" \
      "$DATA/icons/hicolor/256x256/apps/$APP_ID.png"
UNITS="$CONFIG/systemd/user"
if [ -e "$UNITS/routine-panel.service" ]; then
  systemctl --user disable --now routine-panel.service >/dev/null 2>&1 || true
  rm -f "$UNITS/routine-panel.service"
  systemctl --user daemon-reload
  echo "The service for phones is stopped and removed."
fi
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -f -t "$DATA/icons/hicolor" || true
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
if [ "${1:-}" = "--all" ]; then
  rm -rf "$DATA/routine-panel" "$CONFIG/routine-panel"
  echo "Removed, with its settings and the window's profile."
else
  echo "Removed. Settings and the window's profile are kept (./uninstall.sh --all removes them)."
fi
