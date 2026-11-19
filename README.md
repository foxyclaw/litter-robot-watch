# Watchdog Services — Litter-Robot monitor

A small asyncio daemon that watches a Whisker Litter-Robot 4 through the Whisker
cloud API ([pylitterbot](https://github.com/natekspencer/pylitterbot)), keeps its
own forensic record of everything the robot reports, and sends Telegram alerts.
A second check watches a stock price. No LLM at runtime, no Home Assistant.

```
python main.py daemon      # run the monitor (this is what launchd starts)
python main.py snapshot    # read-only smoke test, see below
```

## What it alerts on

All alerts go to the configured Telegram group. Each problem is an *incident*:
alerted once, then repeated until it is resolved (a ✅ message follows) or
silenced with `/ack`.

| Alert | Trigger | Repeat |
|-------|---------|--------|
| 🚨 **Litter likely dumped** | Any of: waste-drawer level steps up by ≥ `dump_waste_jump_points` (25) within `dump_window_seconds` (15 min) and stays there for `dump_confirm_seconds` (5 min) · litter level steps down by ≥ `dump_litter_drop_points` (25) · the empty-cycle odometer increments · an `EMPTY_CYCLE` status is seen · litter-level state becomes `EMPTY` | every `dump_repeat_seconds` (1 h) for up to `dump_max_alert_seconds` (24 h) |
| ⚠️ Litter low | litter-level state in `litter_low_states` (LOW / REFILL / EMPTY) | every `litter_low_repeat_seconds` (24 h); at once when it gets worse |
| 🚨 Globe motor fault / retract fault | status ≠ `FAULT_CLEAR` | every `fault_repeat_seconds` (1 h) |
| 🚨 Pinch detected | pinch status / pinch fault reported | 1 h |
| ⚠️ USB fault | `USBFaultStatus` ≠ `CLEAR` | 1 h |
| 🚨 Error status | any status classified `error` in `status_codes.py` (cat-sensor fault, over-torque, dump/home position fault …) — immediately. Bonnet removed and the statuses listed in `error_statuses` (paused, off …) only after `status_grace_seconds` (10 min), so cleaning the robot does not alert | 1 h |
| 📡 Offline | robot offline, or the monitor cannot reach the Whisker cloud, for more than `offline_minutes` (30) | 1 h |
| 🗑️ Waste drawer full | waste level ≥ `waste_threshold` (90 %). **The percentage only** — the robot's own "drawer full" flag/status is flaky and is ignored | every `waste_repeat_seconds` (3 h) |

Example:

> 🚨 **COPTiP**: litter appears to have been dumped into the waste drawer
> (litter level 90%→44% in 10 min; waste drawer 19%→73% in 5 min). Check the globe has litter!

Notes on the dump detector:

- Only readings taken while the globe is at rest are compared — mid-cycle the
  drawer sensor reports junk (often 0 %).
- A 0 % reading (right after the drawer was emptied) is never used as a baseline.
- The confirmation delay filters one-off sensor blips; set
  `dump_confirm_seconds: 0` to alert on the first reading.
- The waste sensor is noisy, so an occasional false alarm from a large jump is
  possible; raise `dump_waste_jump_points` if it bothers you.

Alert delivery is queued: if Telegram is unreachable the message is retried
with exponential backoff (5 s … 5 min, for up to 24 h) and the queue is kept in
`data/telegram_outbox*.json` so it survives a restart. Late messages are marked
"delayed N min".

## What is logged where

Everything below is local and gitignored.

| Where | What | Retention |
|-------|------|-----------|
| `litter_robot.db` → `raw_snapshots` (ts, robot_id, source, status, json) | the **full** raw `robot.to_dict()` (all ~70 keys) for every poll and every websocket push | `forensics.retention_days` (90) |
| `data/robot_snapshots-YYYY-MM-DD.jsonl` | the same snapshots, one JSON object per line, one file per UTC day; finished days are gzipped | 90 days |
| `litter_robot.db` → `activity_history` | our copy of the Whisker activity feed (clean cycles, cat detections, weights, drawer level …) with the raw entry, de-duplicated on (robot, timestamp, action, value). Whisker itself only keeps about 6 days | forever |
| `litter_robot.db` → `robot_states`, `cat_visits`, `drawer_events`, `alerts` | summary state per poll, cat weights, drawer full/emptied events, incident alerts | forever |
| `litter_robot.db` → `kv_state` | detector state (active incidents, baselines) so a restart does not lose or repeat an alert | — |
| `litter_monitor.log` | one `Checking …` line per snapshot, a `CHANGE …` line whenever an important field changes (old→new), `ACTIVITY …` lines for new feed entries, `ALERT […]` lines | manual |

The plain log tells the story of an incident, e.g.:

```
CHANGE COPTiP [poll]: litter% 90.0→44.0; litterState OPTIMAL→REFILL
CHANGE COPTiP [poll]: status CAT_DETECTED→READY; waste% 19→73
ALERT [litter_dump/new] 🚨 COPTiP: litter appears to have been dumped into the waste drawer (...)
```

Fields tracked for changes: status, waste %, litter % / state / mm, empty / clean /
power cycle odometers, globe motor fault + retract fault, pinch, USB fault,
`robotCycleState` / `robotCycleStatus` / `robotStatus`, display code, online,
bonnet, DFI full flags and mm, cat detect, laser dirty, power, hopper, sleep, wifi.

Useful queries:

```bash
sqlite3 litter_robot.db "SELECT ts, source, status, json_extract(json,'$.DFILevelPercent'),
  json_extract(json,'$.litterLevelPercentage'), json_extract(json,'$.odometerEmptyCycles')
  FROM raw_snapshots ORDER BY id DESC LIMIT 20;"
sqlite3 litter_robot.db "SELECT timestamp, action, action_value FROM activity_history
  ORDER BY timestamp DESC LIMIT 40;"
zcat -f data/robot_snapshots-*.jsonl* | jq -c '[.ts, .status, .data.DFILevelPercent]'
```

### Catching transient states

- **Fast polling** — the normal interval is `interval_seconds` (300). While a cat
  is in the globe, a cycle is running, or an alert is waiting for confirmation,
  the check polls every `fast_poll_interval_seconds` (30) and keeps doing so for
  `fast_poll_hold_seconds` (300) afterwards.
- **Websocket** (`realtime.websocket: true`) — subscribes to Whisker's push
  updates; every push that changes the data is stored and analysed like a poll.
  pylitterbot reconnects on its own with backoff, and polling continues
  regardless, so a dead socket only costs the in-between snapshots.

## Telegram commands

| Command | |
|---------|---|
| `/status` | status, levels, any active alerts |
| `/level` | waste drawer level |
| `/cats` | cat weights, last visits, weight chart |
| `/shit` | drawer-full history |
| `/history` | last 24 h summary |
| `/activity` | last robot events (local copy of the Whisker feed) |
| `/ack` | stop reminder repeats for the active alerts (new evidence alerts again) |
| `/full` | detailed report |

The bot answers in the configured group chat. Direct messages are answered only
for the Telegram user IDs listed in `telegram.authorized_user_ids`; the reply
goes to the chat the command came from. Ignored messages are logged with the
chat and user ID, which is also how you find the ID to put in the list.

All times are shown in `timezone` (default `America/Los_Angeles`) with the
correct PST/PDT label.

## Configuration

Copy `config.example.yaml` to `config.yaml` (gitignored — it holds your
credentials) and edit it. Keys of the `litter_robot` check:

| Key | Default | |
|-----|---------|---|
| `interval_seconds` | 300 | normal poll interval |
| `config.timezone` | `America/Los_Angeles` | display time zone |
| `config.forensics.jsonl_dir` | `data` | where the JSONL files and the Telegram outbox live |
| `config.forensics.retention_days` | 90 | raw snapshot retention (DB + JSONL) |
| `config.forensics.activity_interval_seconds` | 300 | how often the activity feed is copied |
| `config.realtime.websocket` | true | subscribe to push updates |
| `config.realtime.fast_poll_interval_seconds` | 30 | poll interval while something is happening |
| `config.realtime.fast_poll_hold_seconds` | 300 | keep polling fast this long afterwards |
| `config.alerts.waste_threshold` | 90 | waste-drawer alert level (%) |
| `config.alerts.waste_repeat_seconds` | 10800 | waste-drawer alert repeat |
| `config.alerts.dump_waste_jump_points` | 25 | waste step that means "litter dumped" |
| `config.alerts.dump_window_seconds` | 900 | the step may build up over this long |
| `config.alerts.dump_confirm_seconds` | 300 | the step must persist this long |
| `config.alerts.dump_litter_drop_points` | 25 | litter-level drop that means "litter dumped" |
| `config.alerts.dump_max_gap_seconds` | 21600 | readings further apart are not compared |
| `config.alerts.dump_ignore_zero_baseline` | true | ignore 0 % readings as a baseline |
| `config.alerts.dump_repeat_seconds` | 3600 | dump reminder interval |
| `config.alerts.dump_max_alert_seconds` | 86400 | stop dump reminders after this long |
| `config.alerts.litter_low_states` | LOW, REFILL, EMPTY | litter states that alert |
| `config.alerts.litter_low_repeat_seconds` | 86400 | litter-low reminder interval |
| `config.alerts.fault_repeat_seconds` | 3600 | fault / offline reminder interval |
| `config.alerts.offline_minutes` | 30 | offline / cloud-unreachable grace |
| `config.alerts.status_grace_seconds` | 600 | bonnet removed / paused / off grace |
| `config.alerts.error_statuses` | — | extra statuses to alert on (short codes like `BR` or names like `BONNET_REMOVED`) |
| `telegram.authorized_user_ids` | `[]` | user IDs whose DMs the bot answers |

## Smoke test

```bash
venv/bin/python main.py snapshot
```

Connects to Whisker, takes one snapshot, writes it to `raw_snapshots` and the
JSONL file, copies the activity feed, and runs detection in dry-run mode. It
prints what it would alert on. It never sends to Telegram and never changes
robot state, and it is safe to run while the daemon is running.

## Tests

```bash
venv/bin/python -m pip install pytest
venv/bin/python -m pytest
```

Offline unit tests (no network): the 2026-11-10 dump sequence, normal
increments, empty-cycle counter, motor / pinch / USB faults, offline, the
waste-drawer rule, storage and rotation, alert retry queue, bot authorization
and time-zone formatting.

## Running as a service (macOS launchd)

`install_service.sh` installs `com.litterrobot.monitor.plist` as a user
LaunchAgent that runs `venv/bin/python main.py daemon`; `restart_service.sh`
restarts it. After changing code or `config.yaml`, restart the service.

## Layout

| File | |
|------|---|
| `main.py`, `scheduler.py` | entry point, per-check scheduling |
| `checks/litter_robot.py` | the Litter-Robot check (polling, websocket, storage, alert dispatch) |
| `detection.py` | pure detection logic, clock injected, fully unit-tested |
| `status_codes.py` | status names and classification |
| `forensics.py`, `database.py` | JSONL writer, SQLite storage |
| `notifier.py` | Telegram sending with retry queue |
| `telegram_bot.py` | Telegram commands for the litter robot |
| `checks/stock_price.py`, `telegram_command_bot.py` | stock price check and its bot |
| `timeutil.py` | time-zone helpers |
| `monitor.py`, `cli.py` | legacy standalone monitor from before the multi-check daemon; not used by `main.py` |

## License

MIT — Do what you want with it.
