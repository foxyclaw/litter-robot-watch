#!/bin/bash
cd /Users/foxyclaw/openclaw_projects/litter_monitor
source /Users/foxyclaw/openclaw_projects/watchdog_services/.venv/bin/activate
python main.py daemon 2>&1 &
echo "Started litter monitor daemon"
sleep 2
ps aux | grep "watchdog_services/main.py" | grep -v grep