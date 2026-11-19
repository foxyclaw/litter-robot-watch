#!/bin/bash
# Install Litter Robot Monitor as a user LaunchAgent (runs at login)

LAUNCHD_DIR="$HOME/Library/LaunchAgents"
PLIST_NAME="com.litterrobot.monitor.plist"
PLIST_SRC="/Users/foxyclaw/openclaw_projects/watchdog_services/$PLIST_NAME"
PLIST_DEST="$LAUNCHD_DIR/$PLIST_NAME"

echo "Installing Litter Robot Monitor service (user-level)..."
echo "This will run automatically when you log in."
echo ""

# Remove system service if it exists (avoid conflicts)
sudo launchctl unload /Library/LaunchDaemons/$PLIST_NAME 2>/dev/null || true
sudo rm -f /Library/LaunchDaemons/$PLIST_NAME 2>/dev/null || true

# Create LaunchAgents directory if needed
mkdir -p "$LAUNCHD_DIR"

# Copy plist file
cp "$PLIST_SRC" "$PLIST_DEST"

# Unload if already loaded (ignore errors if not loaded)
launchctl unload "$PLIST_DEST" 2>/dev/null || true

# Load the service
launchctl load "$PLIST_DEST"

# Enable to start at login
launchctl enable "gui/$(id - u)/com.litterrobot.monitor" 2>/dev/null || true

# Start it now
launchctl start com.litterrobot.monitor

echo "✅ User service installed and started!"
echo ""
echo "This service will start automatically when you log in to the Mac."
echo ""
echo "Commands:"
echo "  Check status: launchctl list | grep litterrobot"
echo "  View logs:    tail -f /Users/foxyclaw/openclaw_projects/watchdog_services/daemon.log"
echo "  Stop:         launchctl stop com.litterrobot.monitor"
echo "  Restart:      launchctl kickstart -k gui/$(id -u)/com.litterrobot.monitor"
echo "  Uninstall:    launchctl unload $PLIST_DEST && rm $PLIST_DEST"
