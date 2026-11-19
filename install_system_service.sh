#!/bin/bash
# Install Litter Robot Monitor as a system LaunchDaemon (runs at boot)

PLIST_NAME="com.litterrobot.monitor.plist"
PLIST_SRC="/Users/foxyclaw/openclaw_projects/watchdog_services/$PLIST_NAME"
PLIST_DEST="/Library/LaunchDaemons/$PLIST_NAME"

echo "Installing Litter Robot Monitor as system service..."
echo "This will run at boot (before login)"
echo ""

# Stop and unload user service if it exists
launchctl unload ~/Library/LaunchAgents/$PLIST_NAME 2>/dev/null || true
rm -f ~/Library/LaunchAgents/$PLIST_NAME 2>/dev/null || true

# Copy to system LaunchDaemons (requires sudo)
echo "Copying plist to /Library/LaunchDaemons/ (requires sudo)..."
sudo cp "$PLIST_SRC" "$PLIST_DEST"
sudo chmod 644 "$PLIST_DEST"
sudo chown root:wheel "$PLIST_DEST"

# Unload if already loaded
sudo launchctl unload "$PLIST_DEST" 2>/dev/null || true

# Load the service
sudo launchctl load "$PLIST_DEST"

# Start it
sudo launchctl start com.litterrobot.monitor

echo ""
echo "✅ System service installed and started!"
echo ""
echo "Status:"
sudo launchctl list | grep litterrobot
echo ""
echo "Commands:"
echo "  Check status: sudo launchctl list | grep litterrobot"
echo "  View logs:    tail -f /Users/foxyclaw/openclaw_projects/watchdog_services/daemon.log"
echo "  Stop:         sudo launchctl stop com.litterrobot.monitor"
echo "  Restart:      sudo launchctl kickstart -k system/com.litterrobot.monitor"
echo "  Uninstall:    sudo launchctl unload $PLIST_DEST && sudo rm $PLIST_DEST"
echo ""
echo "The service will now start automatically at boot time (before login)."
