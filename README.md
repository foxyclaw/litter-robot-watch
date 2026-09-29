# Litter Robot Monitor

A standalone Python service for monitoring Whisker Litter Robot devices. Tracks waste drawer levels, operational status, and alerts on issues — no Home Assistant required.

## Features

- 📊 **Real-time monitoring** of all connected Litter Robots
- 🗑️ **Waste drawer tracking** with visual level indicators
- 🚨 **Smart alerts** for issues (drawer full, sensor faults, etc.)
- 📝 **SQLite history** for trend analysis
- 🔧 **Standalone operation** — runs anywhere Python runs
- 🐳 **Lightweight** — minimal dependencies

## Quick Start

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure your credentials:**
   ```bash
   cp config.yaml config.yaml.local
   # Edit config.yaml.local with your Whisker account
   ```

3. **Check current status:**
   ```bash
   python main.py status
   ```

4. **Run the monitoring daemon:**
   ```bash
   python main.py daemon
   ```

## Configuration

Edit `config.yaml`:

```yaml
whisker:
  email: "your@email.com"
  password: "your-password"

monitor:
  poll_interval: 30        # seconds between checks
  database: "litter_robot.db"
  log_file: "litter_monitor.log"
  log_level: "INFO"

alerts:
  waste_levels:           # Alert when drawer is:
    - "DF1"              # Full, 2 cycles left
    - "DF2"              # Full, 1 cycle left  
    - "DFS"              # Full, stopped
  
  error_statuses:         # Alert on these issues:
    - "CSF"              # Cat sensor fault
    - "BR"               # Bonnet removed
    - "CSI"              # Cat sensor interrupted
```

## Commands

### Status Check
```bash
python main.py status
```
Shows current state of all robots:
```
📊 Litter Robot Status
==================================================

Living Room LR (a1b2c3d4...)
  Status: ✅ RDY - Ready
  Waste:  [████████░░] 85%
  Power:  AC
  Light:  On
  Lock:   Off
```

### History
```bash
python main.py history -r <robot_id> -H 24 -l 20
```
Shows state history with timestamps and waste levels.

### Alerts
```bash
python main.py alerts              # Show all alerts
python main.py alerts -u           # Show unacknowledged only
python main.py alerts -a 123         # Acknowledge alert #123
```

### Daemon Mode
```bash
python main.py daemon
```
Continuously monitors and logs alerts. Use `systemd`, `launchd`, or `supervisor` to run as a service.

## Status Codes

| Code | Meaning | Type |
|------|---------|------|
| RDY | Ready | Normal |
| CCP | Clean cycle in progress | Activity |
| CCC | Clean cycle complete | Activity |
| CST | Cat sensor triggered | Activity |
| DF1 | Drawer full, 2 cycles left | Warning |
| DF2 | Drawer full, 1 cycle left | Warning |
| DFS | Drawer full, stopped | Error |
| CSF | Cat sensor fault | Error |
| CSI | Cat sensor interrupted | Warning |
| BR | Bonnet removed | Error |
| P | Paused | Warning |
| OFF | Unit off | Inactive |

## Running as a Service

### macOS (launchd)
Create `~/Library/LaunchAgents/com.litterrobot.monitor.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" 
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.litterrobot.monitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/litter_monitor/main.py</string>
        <string>daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/litter_monitor</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/path/to/litter_monitor/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/litter_monitor/daemon.error.log</string>
</dict>
</plist>
```

Load it:
```bash
launchctl load ~/Library/LaunchAgents/com.litterrobot.monitor.plist
```

### Linux (systemd)
Create `~/.config/systemd/user/litter-robot.service`:
```ini
[Unit]
Description=Litter Robot Monitor

[Service]
Type=simple
WorkingDirectory=/path/to/litter_monitor
ExecStart=/usr/bin/python3 main.py daemon
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Enable and start:
```bash
systemctl --user enable litter-robot.service
systemctl --user start litter-robot.service
```

## How It Works

1. Connects to Whisker cloud API using your account credentials
2. Polls robot status every N seconds (configurable)
3. Stores state snapshots in SQLite database
4. Detects status changes and triggers alerts
5. Alert cooldown prevents spam (5 minutes between same alert type)

## Local vs Cloud

Litter Robots communicate via cloud API — there's no local HTTP API. This service:
- Uses the same official API as the Whisker app
- Stores data locally for your own analysis
- Runs standalone without Home Assistant dependencies

For true local-only operation, you'd need to MITM the robot's UDP packets to `dispatch.iothings.site:2000` — this requires router-level packet interception.

## Extending

Add custom alert handlers in `monitor.py`:
```python
def on_alert(robot_id: str, alert_type: str, message: str):
    # Send to Slack, Telegram, etc.
    pass

monitor.add_alert_handler(on_alert)
```

## License

MIT — Do what you want with it.
