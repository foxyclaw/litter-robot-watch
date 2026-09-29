#!/bin/bash
echo "Stopping service..."
# Kill all python processes running main.py
pkill -9 -f "watchdog_services/main.py" 2>/dev/null || true
launchctl stop com.litterrobot.monitor 2>/dev/null || true
sleep 3
# Verify no processes remain
remaining=$(pgrep -f "watchdog_services/main.py" 2>/dev/null | wc -l)
if [ "$remaining" -gt 0 ]; then
    echo "Force killing remaining processes..."
    pgrep -f "watchdog_services/main.py" 2>/dev/null | xargs -I {} kill -9 {} 2>/dev/null || true
    sleep 2
fi
echo "Starting service..."
launchctl start com.litterrobot.monitor 2>/dev/null || sudo launchctl start com.litterrobot.monitor 2>/dev/null || true
sleep 3
echo "Service status:"
ps aux | grep "watchdog_services/main.py" | grep -v grep || echo "No processes found"
