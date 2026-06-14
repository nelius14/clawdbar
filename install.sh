#!/usr/bin/env bash
# Clawdbar install — local, single-machine menu bar build.
# Builds a uv venv with pyobjc + pillow, regenerates the icon, substitutes the
# real username into both LaunchAgents, and loads them.
#
# Run it from your checkout on the Mac you want the menu bar on:
#
#   bash install.sh
#
# It stages the project into ~/.clawdbar (runtime dir, kept separate from your
# source checkout) and is safe to re-run after edits to restart everything.

set -euo pipefail

INSTALL_DIR="$HOME/.clawdbar"
LA_DIR="$HOME/Library/LaunchAgents"
USER_NAME="$(whoami)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DAEMON_LABEL="io.clawdbar"
MENUBAR_LABEL="io.clawdbar.menubar"

echo "=== Clawdbar install (local menu bar build) ==="
echo "User:    $USER_NAME"
echo "Source:  $SRC_DIR"
echo "Install: $INSTALL_DIR"

# 0. Require uv (global rule: uv, not pip; no global pip installs)
if ! command -v uv >/dev/null 2>&1; then
  echo "ERROR: 'uv' is not installed." >&2
  echo "Install it first, then re-run:" >&2
  echo "  brew install uv" >&2
  echo "  # or: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

# 1. Stage files into INSTALL_DIR (skip when already running in place post-rsync)
if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
  echo "[1/6] Copying project into $INSTALL_DIR..."
  mkdir -p "$INSTALL_DIR"
  rsync -a --delete \
    --exclude '.venv' --exclude 'venv' --exclude '.git' \
    --exclude '__pycache__' --exclude '.DS_Store' --exclude 'CLAUDE.local.md' \
    "$SRC_DIR"/ "$INSTALL_DIR"/
else
  echo "[1/6] Running in place ($INSTALL_DIR) — skipping copy."
fi

cd "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/venv"  # remove any legacy pip venv from older installs

# 2. uv venv + dependencies
echo "[2/6] Creating uv venv + installing pyobjc + pillow (can take a minute)..."
uv venv --clear "$INSTALL_DIR/.venv"
uv pip install --python "$INSTALL_DIR/.venv/bin/python" \
  pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-WebKit pyobjc-framework-Quartz pillow

# 3. Regenerate the icon (in case PIL version differs)
echo "[3/6] Generating menu bar icon..."
( cd "$INSTALL_DIR/static" && "$INSTALL_DIR/.venv/bin/python" ../tools/make_icon.py )

# 4. Install LaunchAgents with the real username substituted
echo "[4/6] Installing LaunchAgents..."
mkdir -p "$LA_DIR"
for label in "$DAEMON_LABEL" "$MENUBAR_LABEL"; do
  sed "s/REPLACE_USER/$USER_NAME/g" "$INSTALL_DIR/$label.plist" > "$LA_DIR/$label.plist"
done

# 5. (Re)load daemon
echo "[5/6] Loading daemon..."
launchctl unload "$LA_DIR/$DAEMON_LABEL.plist" 2>/dev/null || true
launchctl load -w "$LA_DIR/$DAEMON_LABEL.plist"

# Give the daemon a moment to bind before the menu bar app polls it
sleep 2

# 6. (Re)load menu bar app
echo "[6/6] Loading menu bar app..."
launchctl unload "$LA_DIR/$MENUBAR_LABEL.plist" 2>/dev/null || true
launchctl load -w "$LA_DIR/$MENUBAR_LABEL.plist"

echo ""
echo "Done. Look for the Clawd icon in your menu bar."
echo "Daemon:   http://127.0.0.1:7337  (loopback only)"
echo ""
echo "Commands:"
echo "  launchctl list | grep clawdbar"
echo "  curl -s http://127.0.0.1:7337/usage | python3 -m json.tool"
echo "  tail -F ~/Library/Logs/clawdbar.out.log"
echo "  tail -F ~/Library/Logs/clawdbar-menubar.err.log"
echo "  bash $INSTALL_DIR/install.sh   # restart everything after edits"
echo ""
echo "Uninstall:"
echo "  launchctl unload $LA_DIR/$DAEMON_LABEL.plist"
echo "  launchctl unload $LA_DIR/$MENUBAR_LABEL.plist"
echo "  rm -rf $INSTALL_DIR $LA_DIR/$DAEMON_LABEL.plist $LA_DIR/$MENUBAR_LABEL.plist"
