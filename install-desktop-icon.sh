#!/usr/bin/env bash
# Installs the SSD Sync desktop icon / app launcher.
# Usage:  ./install-desktop-icon.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$APP_DIR/ssd-sync.desktop"
DEST_DIR="$HOME/.local/share/applications"
DEST="$DEST_DIR/ssd-sync.desktop"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found. Install it first:  sudo apt install python3 python3-tk" >&2
  exit 1
fi
if ! python3 -c "import tkinter" 2>/dev/null; then
  echo "ERROR: python3-tk is missing. Install it first:  sudo apt install python3-tk" >&2
  exit 1
fi
if [ ! -f "$SRC" ]; then
  echo "ERROR: $SRC not found (run this script from the Syncer folder)." >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
# Rewrite Exec/Path with the real location of this checkout.
# (Exec arg is quoted per spec; Path must be a bare absolute path.)
sed -e "s|^Exec=.*|Exec=python3 \"$APP_DIR/sync_app.py\"|" \
    -e "s|^Path=.*|Path=$APP_DIR|" \
    "$SRC" > "$DEST"
chmod +x "$DEST"

# Optional copy onto the actual Desktop folder.
if [ -d "$HOME/Desktop" ]; then
  cp "$DEST" "$HOME/Desktop/ssd-sync.desktop"
  chmod +x "$HOME/Desktop/ssd-sync.desktop"
  # Mark trusted so GNOME shows the icon instead of a warning (needs gio).
  if command -v gio >/dev/null 2>&1; then
    gio set "$HOME/Desktop/ssd-sync.desktop" metadata::trusted true 2>/dev/null || true
  fi
  echo "Desktop copy: $HOME/Desktop/ssd-sync.desktop"
  echo "Right-click it -> 'Allow Launching' if Ubuntu asks."
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DEST_DIR" >/dev/null 2>&1 || true
fi

echo "Installed: $DEST"
echo "Press Super, type 'SSD Sync' to launch. Right-click -> 'Add to Favorites' to pin it."
