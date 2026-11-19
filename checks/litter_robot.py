"""Litter Robot / Whisker monitoring check.

Each poll (and each websocket push, when enabled):
  1. the full raw ``robot.to_dict()`` is stored in SQLite (``raw_snapshots``)
     and appended to a daily JSONL file — our own forensic record;
  2. every change in an important field is written to the log (old→new);
  3. the detector (``detection.py``) looks for litter dumps, motor / pinch /
     USB faults, error statuses and offline periods and sends Telegram alerts
     that repeat until resolved;
  4. the waste-drawer alert fires on the percentage alone (>= waste_threshold),
     repeating every ``waste_repeat_seconds``.

Critical incidents (litter dump, globe motor / retract fault, pinch, hardware
error status, offline / cloud unreachable) are additionally sent as a DM to
``telegram.alert_dm_user_ids`` and documented in ``data/incidents/*.json``.
"""
import asyncio
import copy
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pylitterbot import Account
from pylitterbot.robot import Robot

from checks.base import BaseCheck
from database import LitterRobotDatabase
from detection import (
    Alert,
    DetectionConfig,
    Detector,
    _enum_str,
    diff_fields,
    extract_fields,
    fast_poll_reason,
    format_changes,
)
from forensics import SnapshotLog
from incidents import IncidentStore, utc_iso
from notifier import TelegramNotifier
from status_codes import STATUS_CODES, canonical_set, canonical_status, describe_status  # noqa: F401
from timeutil import DEFAULT_TZ, fmt_local, parse_utc

BASE_DIR = Path(__file__).resolve().parent.parent

INCIDENT_SNAPSHOT_WINDOW_SECONDS = 1800   # raw snapshots kept in an incident file
INCIDENT_ACTIVITY_ENTRIES = 50            # stored activity entries kept in an incident file

WS_STALE_SECONDS = 180          # AppSync sends a keep-alive frame every 60 s
WS_RESUBSCRIBE_MIN_SECONDS = 300
WS_REPORT_SECONDS = 3600        # websocket health line in the log

ACTIVITY_QUERY = """
query GetLR4Activity($serial: String!, $limit: Int, $consumer: String) {
  getLitterRobot4Activity(serial: $serial, limit: $limit, consumer: $consumer) {
    serial measure timestamp value actionValue originalHex valueString
    stateString consumer commandSource
  }
}
"""


def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else BASE_DIR / p


class LitterRobotCheck(BaseCheck):
    """Monitors Litter Robot state and alerts on issues."""

    def __init__(self, config: Dict[str, Any], notifier=None, dry_run: bool = False):
        super().__init__(config, notifier)
        self.logger = logging.getLogger("check.litter_robot")
        self.dry_run = dry_run

        self.account: Optional[Account] = None
        self.robots: Dict[str, Robot] = {}
        self.db: Optional[LitterRobotDatabase] = None
        self._alert_cooldown: Dict[str, float] = {}
        self._last_pet_sync: float = 0.0
        self._connected: bool = False
        self._refresh_failures: int = 0

        forensics = config.get("forensics", {}) or {}
        self.data_dir = _resolve(forensics.get("jsonl_dir", "data"))
        self.retention_days = int(forensics.get("retention_days", 90))
        self.activity_interval = float(forensics.get("activity_interval_seconds", 300))
        self.snapshot_log = SnapshotLog(str(self.data_dir), self.retention_days)
        self.incident_store = IncidentStore(str(self.data_dir / "incidents"))
        self._bg_tasks: set = set()

        realtime = config.get("realtime", {}) or {}
        self.ws_enabled = bool(realtime.get("websocket", True))
        self.fast_interval = float(realtime.get("fast_poll_interval_seconds", 30))
        self.fast_hold = float(realtime.get("fast_poll_hold_seconds", 300))

        self.tz_name = config.get("timezone", DEFAULT_TZ)
        self.det_cfg = DetectionConfig.from_config(config.get("alerts", {}))
        self.detectors: Dict[str, Detector] = {}
        self._api_detector: Optional[Detector] = None
        self._last_fields: Dict[str, Dict[str, Any]] = {}
        self._last_raw_sig: Dict[str, str] = {}
        self._last_activity_sync: Dict[str, float] = {}
        self._last_prune: float = 0.0
        self._fast_until: float = 0.0
        self._fast_on: bool = False
        self._ws_since: Dict[str, float] = {}     # robot id -> subscribed at
        self._ws_stats: Dict[str, int] = {"pushes": 0, "pushes_new_data": 0, "poll_first": 0}
        self._ws_last_report: float = 0.0
        self._ws_last_resubscribe: float = 0.0
        self._refreshing: bool = False
        self._lock = asyncio.Lock()
        self._ws_tasks: set = set()
        self._ws_unsubs: list = []

        # Per-check Telegram (overrides shared notifier if configured)
        tg = config.get("telegram", {}) or {}
        if tg.get("bot_token") and tg.get("chat_id"):
            self.telegram = TelegramNotifier(
                tg["bot_token"], tg["chat_id"],
                outbox_path=None if dry_run else str(self.data_dir / "telegram_outbox_litter.json"),
            )
            self.telegram.authorized_user_ids = [str(u) for u in tg.get("authorized_user_ids") or []]
            self.telegram.alert_dm_user_ids = [str(u) for u in tg.get("alert_dm_user_ids") or []]
            self.logger.info("Litter Robot: using dedicated Telegram notifier")
        else:
            self.telegram = notifier  # fall back to shared notifier

    @property
    def name(self) -> str:
        return "litter_robot"

    @property
    def interval_override(self) -> Optional[float]:
        """Scheduler hint: poll faster while a cycle / fault / incident is in progress."""
        return self.fast_interval if self.fast_poll_reason() else None

    def fast_poll_reason(self, now: Optional[float] = None, hold: bool = True) -> Optional[str]:
        """Why the check must poll fast right now (None = normal interval).

        Derived from the current state on every call, not from a timer, so it
        cannot lapse while the reason persists (2026-11-19: with an open
        incident and FAULT_TIMEOUT latched the old timer ran out and the
        08:00 cycle was polled at 5 min). Fast while:
          * the robot is doing something or is not at rest: active status,
            ``robotCycleStatus`` != CYCLE_IDLE, ``displayCode`` != DC_MODE_IDLE,
            globe motor / retract fault not clear, ``robotCycleState`` FAULT_*;
          * any incident is open, or an alert is waiting for confirmation;
          * for ``fast_poll_hold_seconds`` after the last of the above.
        """
        for robot_id, det in self.detectors.items():
            reason = fast_poll_reason(self._last_fields.get(robot_id))
            if reason:
                return reason
            if det.incidents:
                return "open incident " + ", ".join(sorted(det.incidents))
            if det.has_pending():
                return "alert waiting for confirmation"
        if self._api_detector is not None and self._api_detector.incidents:
            return "open incident " + ", ".join(sorted(self._api_detector.incidents))
        if hold and (time.time() if now is None else now) < self._fast_until:
            return "hold after activity"
        return None

    # ── connection ───────────────────────────────────────────────────────────

    async def _init_db(self) -> None:
        if self.db is None:
            db_path = self.config.get("database", "litter_robot.db")
            self.db = LitterRobotDatabase(db_path)
            await self.db.connect()
            self.logger.info(f"Database connected: {db_path}")

    async def _disconnect(self) -> None:
        for unsub in self._ws_unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._ws_unsubs = []
        self._ws_since = {}
        if self.account:
            try:
                await asyncio.wait_for(self.account.disconnect(), timeout=15)
            except Exception as e:
                self.logger.debug(f"Whisker disconnect: {e}")
        self.account = None
        self._connected = False

    async def _connect(self, pet_sync: bool = True, subscribe: Optional[bool] = None) -> bool:
        whisker = self.config.get("whisker", {})
        email = whisker.get("email")
        password = whisker.get("password")
        if not email or not password:
            self.logger.error("Missing whisker email/password in config")
            return False
        await self._disconnect()
        try:
            self.account = Account()
            await asyncio.wait_for(
                self.account.connect(
                    username=email, password=password, load_robots=True, load_pets=True,
                ),
                timeout=90,
            )
            self.robots = {robot.id: robot for robot in self.account.robots}
            self.logger.info(
                f"Connected to Whisker. Robots: {len(self.robots)}, "
                f"Pets: {len(self.account.pets)}"
            )
            for pet in self.account.pets:
                self.logger.info(f"  Pet: {pet.name} (id={pet.id})")
            for rid, robot in self.robots.items():
                self.logger.info(f"  Robot: {robot.name} ({rid})")
            if not self.robots:
                # connect() swallows some network errors and just yields no robots
                self.logger.error("Whisker connect returned no robots")
                return False

            if pet_sync:
                # Initial pet weight sync (with timeout)
                try:
                    synced = await asyncio.wait_for(self._sync_pet_weight_history(), timeout=60)
                    self.logger.info(
                        f"Startup pet weight sync: {synced} new entries" if synced > 0
                        else "Startup pet weight sync: no new entries"
                    )
                except asyncio.TimeoutError:
                    self.logger.warning("Startup pet weight sync timed out (60s) — will retry next cycle")
                except Exception as e:
                    self.logger.warning(f"Startup pet weight sync failed: {e}")

            if self.ws_enabled if subscribe is None else subscribe:
                await self._subscribe_realtime()

            self._connected = True
            self._refresh_failures = 0
            return True
        except Exception as e:
            self.logger.error(f"Whisker connect failed: {type(e).__name__}: {e}")
            self._connected = False
            return False

    async def _refresh(self) -> bool:
        """Refresh every robot. Unlike Account.refresh_robots() this does NOT
        swallow network errors, so stale data is never mistaken for fresh."""
        self._refreshing = True
        try:
            await asyncio.wait_for(
                asyncio.gather(*(robot.refresh() for robot in self.robots.values())), timeout=30
            )
            self._refresh_failures = 0
            return True
        except Exception as e:
            self._refresh_failures += 1
            reason = "timed out (30s)" if isinstance(e, asyncio.TimeoutError) else f"{type(e).__name__}: {e}"
            if self._refresh_failures >= 3:
                self.logger.error(f"Robot refresh failed {self._refresh_failures}x ({reason}) — reconnecting next cycle")
                self._connected = False
            else:
                self.logger.warning(f"Robot refresh failed ({reason}) — skipping this cycle")
            return False
        finally:
            self._refreshing = False

    # ── realtime (websocket) ─────────────────────────────────────────────────

    async def _subscribe_realtime(self) -> None:
        """Best effort: pylitterbot keeps the socket alive and reconnects with
        backoff; regular polling continues regardless, so a dead socket only
        costs us the extra in-between snapshots.

        What to expect (checked live on 2026-11-19 with pylitterbot 2025.3.2):
        the subscription is acknowledged, AppSync sends a keep-alive every 60 s
        and a ``data`` frame only when Whisker's backend publishes a change.
        ``_check_ws_health`` logs how many pushes arrived versus changes that
        polling saw first, and re-subscribes when the socket goes silent."""
        try:
            from pylitterbot.event import EVENT_UPDATE
        except Exception as e:
            self.logger.warning(f"Websocket updates unavailable: {e}")
            return
        for robot in self.robots.values():
            try:
                self._ws_unsubs.append(robot.on(EVENT_UPDATE, lambda r=robot: self._on_robot_update(r)))
                await asyncio.wait_for(robot.subscribe(), timeout=30)
                self._ws_since[robot.id] = time.time()
                self.logger.info(f"Websocket updates subscribed for {robot.name}")
            except Exception as e:
                self._ws_since.pop(robot.id, None)
                self.logger.warning(f"Websocket subscribe failed for {robot.name} ({type(e).__name__}: {e}) — polling only")

    def _ws_last_frame(self, robot: Robot) -> Optional[float]:
        """Epoch of the last frame (data or keep-alive) on the robot's socket."""
        last = getattr(getattr(robot, "_transport", None), "_last_received", None)
        try:
            return last.timestamp() if last is not None else None
        except Exception:
            return None

    def _ws_summary(self, now: float) -> str:
        st = self._ws_stats
        ages = []
        for rid in self._ws_since:
            robot = self.robots.get(rid)
            last = self._ws_last_frame(robot) if robot else None
            ages.append(f"last frame {now - last:.0f}s ago" if last else "no frame yet")
        return (
            f"{', '.join(ages) or 'not subscribed'}; {st['pushes']} push(es), "
            f"{st['pushes_new_data']} with new data; {st['poll_first']} change(s) seen first by polling"
        )

    async def _check_ws_health(self, now: Optional[float] = None) -> bool:
        """Log websocket health; re-subscribe when the socket has gone silent
        (no data and no keep-alive for WS_STALE_SECONDS: a half-open connection
        that pylitterbot cannot notice by itself). Returns True if re-subscribed."""
        if not self._ws_since:
            return False
        now = time.time() if now is None else now
        stale = []
        for rid, since in self._ws_since.items():
            robot = self.robots.get(rid)
            if robot is None:
                continue
            last = self._ws_last_frame(robot)
            if now - max(last or 0.0, since) > WS_STALE_SECONDS:
                stale.append(robot)
        if stale and now - self._ws_last_resubscribe >= WS_RESUBSCRIBE_MIN_SECONDS:
            self._ws_last_resubscribe = now
            self.logger.warning(
                f"Websocket silent for more than {WS_STALE_SECONDS}s "
                f"({self._ws_summary(now)}) — re-subscribing; polling continues"
            )
            robots = [r for r in self.robots.values() if r.id in self._ws_since]
            for robot in robots:
                try:
                    await asyncio.wait_for(robot.unsubscribe(), timeout=15)
                except Exception as e:
                    self.logger.debug(f"Websocket unsubscribe: {type(e).__name__}: {e}")
            for robot in robots:
                try:
                    await asyncio.wait_for(robot.subscribe(), timeout=30)
                    self._ws_since[robot.id] = now
                except Exception as e:
                    self.logger.warning(
                        f"Websocket re-subscribe failed for {robot.name} "
                        f"({type(e).__name__}: {e}) — polling only")
            return True
        if now - self._ws_last_report >= WS_REPORT_SECONDS:
            self._ws_last_report = now
            self.logger.info(f"Websocket health: {self._ws_summary(now)}")
        return False

    def _on_robot_update(self, robot: Robot) -> None:
        if self._refreshing or not self._connected:
            return  # our own poll; handled by run()
        self._ws_stats["pushes"] += 1
        try:
            task = asyncio.get_running_loop().create_task(self._handle_ws_update(robot))
        except RuntimeError:
            return
        self._ws_tasks.add(task)
        task.add_done_callback(self._ws_tasks.discard)

    async def _handle_ws_update(self, robot: Robot) -> None:
        try:
            raw = copy.deepcopy(robot.to_dict())
            if self._signature(raw) == self._last_raw_sig.get(robot.id):
                self.logger.debug(f"Websocket push for {robot.name}: no new data")
                return
            self._ws_stats["pushes_new_data"] += 1
            await self.process_snapshot(robot.id, self._get_state_dict(robot), raw, source="ws")
        except Exception as e:
            self.logger.error(f"Websocket update handling failed: {type(e).__name__}: {e}")

    @staticmethod
    def _signature(raw: Dict[str, Any]) -> str:
        return json.dumps(raw, sort_keys=True, default=str)

    # ── main iteration ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """One monitoring iteration."""
        await self._init_db()

        if not self._connected:
            ok = await self._connect()
            # _connect already loaded fresh robot data
        else:
            ok = await self._refresh()
        if not ok:
            await self._note_api(reachable=False)
            return
        await self._note_api(reachable=True)

        # Sync pet weight history every 30 minutes (with timeout to avoid blocking)
        now_ts = time.time()
        if now_ts - self._last_pet_sync > 1800:
            try:
                synced = await asyncio.wait_for(self._sync_pet_weight_history(), timeout=60)
                if synced > 0:
                    self.logger.info(f"Pet weight sync: {synced} new entries")
                else:
                    self.logger.info("Pet weight sync: no new entries")
            except asyncio.TimeoutError:
                self.logger.warning("Pet weight sync timed out (60s)")
            except Exception as e:
                self.logger.warning(f"Pet weight sync failed: {e}")
            self._last_pet_sync = time.time()

        for robot_id in list(self.robots):
            try:
                await self._check_robot(robot_id)
            except Exception as e:
                self.logger.error(f"check_robot({robot_id}) failed: {e}", exc_info=True)
            try:
                await self._sync_activity(robot_id)
            except Exception as e:
                self.logger.warning(f"Activity history sync failed: {type(e).__name__}: {e}")

        try:
            await self._check_ws_health()
        except Exception as e:
            self.logger.warning(f"Websocket health check failed: {type(e).__name__}: {e}")
        await self._maintenance()
        if self.telegram and hasattr(self.telegram, "ensure_retry_task"):
            self.telegram.ensure_retry_task()

    async def _note_api(self, reachable: bool) -> None:
        """Track Whisker-cloud reachability so a blind monitor raises an alert."""
        if self._api_detector is None:
            state = await self.db.get_kv("detector:_api") if not self.dry_run else None
            self._api_detector = Detector("Litter-Robot", self.det_cfg, state)
        det = self._api_detector
        before = json.dumps(det.state, sort_keys=True, default=str)
        now = time.time()
        alerts = det.process_reachable(now) if reachable else det.process_unreachable(now)
        await self._dispatch("_api", alerts, now=now)
        if not self.dry_run and json.dumps(det.state, sort_keys=True, default=str) != before:
            await self.db.set_kv("detector:_api", det.state)

    async def _maintenance(self) -> None:
        if self.dry_run or time.time() - self._last_prune < 86400:
            return
        self._last_prune = time.time()
        try:
            deleted = await self.db.prune_raw_snapshots(self.retention_days)
            if deleted:
                self.logger.info(f"Pruned {deleted} raw snapshots older than {self.retention_days} days")
        except Exception as e:
            self.logger.warning(f"Raw snapshot pruning failed: {e}")

    # ── state extraction ─────────────────────────────────────────────────────

    def _get_state_dict(self, robot: Robot) -> Dict[str, Any]:
        status_code = canonical_status(robot.status)
        status_desc, status_type = describe_status(status_code)
        waste_level = getattr(robot, "waste_drawer_level", None)
        cat_weight_lbs = getattr(robot, "pet_weight", None)
        cat_weight_kg = round(cat_weight_lbs * 0.453592, 2) if cat_weight_lbs else None
        cycles = getattr(robot, "cycle_count", None) or getattr(robot, "total_cycles", None)
        raw = robot.to_dict() or {}
        return {
            "id": robot.id,
            "name": robot.name,
            "serial": getattr(robot, "serial", None),
            "model": getattr(robot, "model", "Unknown"),
            "status": status_code,
            "status_description": status_desc,
            "status_type": status_type,
            "waste_drawer_level": waste_level,
            "is_waste_drawer_full": getattr(robot, "is_waste_drawer_full", False),
            "litter_level": getattr(robot, "litter_level", None),
            "litter_level_state": (
                _enum_str(getattr(robot, "litter_level_state", None))
                if hasattr(robot, "litter_level_state") else None
            ),
            "cat_weight_lbs": cat_weight_lbs,
            "cat_weight_kg": cat_weight_kg,
            "power_status": getattr(robot, "power_status", "AC"),
            "is_online": getattr(robot, "is_online", True),
            "nightlight_status": 1 if getattr(robot, "night_light_mode_enabled", False) else 0,
            "panel_locked": 1 if getattr(robot, "panel_lock_enabled", False) else 0,
            "sleep_mode": 1 if getattr(robot, "sleep_mode_enabled", False) else 0,
            "cycle_count": cycles,
            "scoops_saved": getattr(robot, "scoops_saved_count", None),
            "firmware": getattr(robot, "firmware", None),
            "last_seen": getattr(robot, "last_seen", None),
            "empty_cycles": raw.get("odometerEmptyCycles"),
            "globe_motor_fault": _enum_str(getattr(robot, "globe_motor_fault_status", None)),
            "globe_motor_retract_fault": _enum_str(getattr(robot, "globe_motor_retract_fault_status", None)),
            "usb_fault": _enum_str(getattr(robot, "usb_fault_status", None)),
            "pinch_status": raw.get("pinchStatus"),
            "robot_cycle_state": raw.get("robotCycleState"),
            "robot_cycle_status": raw.get("robotCycleStatus"),
        }

    async def _check_robot(self, robot_id: str) -> None:
        robot = self.robots.get(robot_id)
        if not robot:
            return
        state = self._get_state_dict(robot)
        raw = copy.deepcopy(robot.to_dict())
        await self.process_snapshot(robot_id, state, raw, source="poll")

    # ── the pipeline (also used by tests and the smoke command) ──────────────

    async def _get_detector(self, robot_id: str, name: str) -> Detector:
        det = self.detectors.get(robot_id)
        if det is not None:
            det.name = name
            return det
        state = await self.db.get_kv(f"detector:{robot_id}")
        det = Detector(name, self.det_cfg, state)
        if det.state.get("last") is None:
            # First run with this code (or a wiped state): prime the detector
            # with the last stored snapshot so a change across the restart is
            # still seen.
            last = await self.db.get_latest_raw_snapshot(robot_id)
            if last:
                try:
                    ts = parse_utc(last["ts"]).timestamp()
                    det.process(extract_fields(last["data"], last.get("status")), ts)
                    det.incidents.clear()
                    det.state["cond_since"].clear()
                except Exception as e:
                    self.logger.debug(f"Could not prime detector: {e}")
        if det.state.get("last"):
            self._last_fields.setdefault(
                robot_id, {k: v for k, v in det.state["last"].items() if k != "ts"}
            )
        self.detectors[robot_id] = det
        return det

    async def process_snapshot(self, robot_id: str, state: Dict[str, Any], raw: Dict[str, Any],
                               source: str = "poll", now: Optional[float] = None) -> List[Alert]:
        """Store one snapshot, log changes, run detection, send alerts."""
        async with self._lock:
            now = time.time() if now is None else now
            ts = datetime.fromtimestamp(now, tz=timezone.utc)
            name = state.get("name") or robot_id
            status = canonical_status(state.get("status"))
            fields = extract_fields(raw, status)
            det = await self._get_detector(robot_id, name)  # before storing: may prime from DB

            # 1. forensic record — never let a storage error stop detection
            try:
                await self.db.store_raw_snapshot(
                    robot_id, raw, ts.strftime("%Y-%m-%d %H:%M:%S"), source=source, status=status
                )
            except Exception as e:
                self.logger.error(f"Storing raw snapshot failed: {e}")
            try:
                self.snapshot_log.append(
                    {"ts": ts.isoformat(timespec="seconds"), "robot_id": robot_id,
                     "source": source, "status": status, "data": raw},
                    now=ts,
                )
            except Exception as e:
                self.logger.error(f"Writing snapshot JSONL failed: {e}")
            self._last_raw_sig[robot_id] = self._signature(raw)

            # 2. plain-log story: what changed since the previous snapshot
            changes = diff_fields(self._last_fields.get(robot_id), fields)
            if changes:
                self.logger.info(f"CHANGE {name} [{source}]: {format_changes(changes)}")
                if source == "poll" and robot_id in self._ws_since:
                    self._ws_stats["poll_first"] += 1   # no push announced this change
            self._last_fields[robot_id] = fields

            self.logger.info(
                f"Checking {name} [{source}]: status={status}, waste={state.get('waste_drawer_level')}%, "
                f"full={state.get('is_waste_drawer_full')}, litter={fields.get('litter_pct')}% "
                f"{fields.get('litter_state')}, emptyCycles={fields.get('empty_cycles')}, "
                f"cleanCycles={fields.get('clean_cycles')}"
            )

            # 3. existing behaviour: state history, drawer alert (% only), drawer events
            if not self.dry_run:
                await self.db.store_state(robot_id, state)
            await self._check_waste_alert(robot_id, state, now)
            if not self.dry_run:
                await self._track_drawer_events(robot_id, state)

            # 4. detection
            if self.dry_run:
                det = Detector(name, self.det_cfg, copy.deepcopy(det.state))
            alerts = det.process(fields, now)
            await self._dispatch(robot_id, alerts, raw=raw, now=now)
            if not self.dry_run:
                await self.db.set_kv(f"detector:{robot_id}", det.state)

            # 5. poll faster while something is going on: robot not at rest,
            #    fault latched, incident open, alert pending (+ hold afterwards)
            reason = self.fast_poll_reason(now, hold=False)
            if reason:
                if not self._fast_on:
                    self.logger.info(f"Fast polling ({self.fast_interval:.0f}s) on for {name}: {reason}")
                self._fast_on = True
                self._fast_until = now + self.fast_hold
            elif self._fast_on and now >= self._fast_until:
                self._fast_on = False
                self.logger.info(f"Fast polling off for {name}: at rest, no open incident")
            return alerts

    async def _dispatch(self, robot_id: str, alerts: List[Alert],
                        raw: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        for alert in alerts:
            prefix = "DRY-RUN (not sent) " if self.dry_run else ""
            self.logger.warning(f"{prefix}ALERT [{alert.key}/{alert.kind}] {alert.plain}")
            if self.dry_run:
                continue
            try:
                await self.db.store_alert(robot_id, alert.key, alert.plain, alert.kind)
            except Exception as e:
                self.logger.error(f"Storing alert failed: {e}")
            if self.telegram:
                try:
                    # critical: group + DM to telegram.alert_dm_user_ids (first alert,
                    # every reminder and the resolved message)
                    if alert.critical and hasattr(self.telegram, "send_critical"):
                        await self.telegram.send_critical(alert.text)
                    else:
                        await self.telegram.send_reliable(alert.text)
                except Exception as e:
                    self.logger.error(f"Telegram alert failed: {type(e).__name__}: {e}")
            if alert.critical:
                try:
                    await self._record_incident(robot_id, alert, raw, now)
                except Exception as e:  # forensics must never break alerting
                    self.logger.error(f"Incident snapshot failed: {type(e).__name__}: {e}")

    # ── incident files (data/incidents/<YYYYmmdd-HHMMSS>-<kind>.json) ────────

    async def _incident_context(self, robot_id: str, now: float) -> Dict[str, Any]:
        since = datetime.fromtimestamp(now - INCIDENT_SNAPSHOT_WINDOW_SECONDS, tz=timezone.utc)
        rid = None if robot_id == "_api" else robot_id   # cloud unreachable: all robots
        return {
            "raw_snapshots": await self.db.get_raw_snapshots_since(
                rid, since.strftime("%Y-%m-%d %H:%M:%S")),
            "activity_history": await self.db.get_activities(
                rid, limit=INCIDENT_ACTIVITY_ENTRIES, with_raw=True),
        }

    async def _record_incident(self, robot_id: str, alert: Alert,
                               raw: Optional[Dict[str, Any]], now: float) -> None:
        """Open / update / close the forensic file of a critical incident."""
        kv_key = f"incident_file:{robot_id}:{alert.key}"
        event = {"ts": utc_iso(now), "kind": alert.kind, "text": alert.plain}
        det = self._api_detector if robot_id == "_api" else self.detectors.get(robot_id)
        stored = await self.db.get_kv(kv_key)
        path = Path(stored) if stored else None

        if alert.kind == "new":
            doc = {
                "incident": alert.key,
                "robot_id": robot_id,
                "robot_name": det.name if det else robot_id,
                "opened_at": utc_iso(now),
                "opened_at_local": fmt_local(now, "%Y-%m-%d %H:%M:%S %Z", self.tz_name),
                "trigger": {
                    "reason": alert.plain,
                    "details": copy.deepcopy(det.incidents.get(alert.key)) if det else None,
                },
                "current_raw": raw,
                **await self._incident_context(robot_id, now),
                "activity_fresh": None,
                "alerts": [event],
                "resolved": None,
            }
            path = self.incident_store.create(alert.key, now, doc)
            await self.db.set_kv(kv_key, str(path))
            self.logger.warning(f"INCIDENT {alert.key} opened — snapshot written to {path}")
            if robot_id == "_api":   # the cloud is unreachable: nothing to fetch
                self.incident_store.update(path, activity_fresh={"error": "Whisker cloud unreachable"})
                return
            # immediately pull the freshest activity feed (read-only) into the file
            task = asyncio.get_running_loop().create_task(self._add_fresh_activity(robot_id, path))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
            return

        if alert.kind == "resolved":
            final = {
                "ts": utc_iso(now),
                "reason": alert.plain,
                "current_raw": raw,
                **await self._incident_context(robot_id, now),
            }
            if path and self.incident_store.update(path, append={"alerts": event}, resolved=final):
                self.logger.info(f"INCIDENT {alert.key} resolved — final snapshot added to {path}")
            else:  # opened before incident files existed, or the file was removed
                path = self.incident_store.create(
                    alert.key, now, {"incident": alert.key, "robot_id": robot_id, "resolved": final},
                    suffix="-resolved",
                )
                self.logger.info(f"INCIDENT {alert.key} resolved — final snapshot written to {path}")
            await self.db.set_kv(kv_key, None)
            return

        if path:  # reminder / new evidence
            self.incident_store.update(path, append={"alerts": event})

    async def _add_fresh_activity(self, robot_id: str, path: Path) -> None:
        """Fetch the activity feed right now (read-only query) and add it to the incident file."""
        fresh: Dict[str, Any] = {"fetched_at": utc_iso(time.time())}
        try:
            robot = self.robots.get(robot_id)
            if robot is None:
                raise RuntimeError("robot not connected")
            entries = await self._fetch_activity(robot, limit=100)
            fresh["entries"] = entries
            try:
                fresh["new_entries"] = len(await self.db.store_activities(robot_id, entries))
            except Exception as e:
                self.logger.debug(f"Storing fresh activity failed: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            fresh["error"] = f"{type(e).__name__}: {e}"
            self.logger.warning(f"Incident fresh activity fetch failed: {fresh['error']}")
        try:
            self.incident_store.update(path, activity_fresh=fresh)
        except Exception as e:
            self.logger.error(f"Updating incident file failed: {type(e).__name__}: {e}")

    async def wait_background(self) -> None:
        """Wait for pending incident follow-up work (used by tests / shutdown)."""
        while self._bg_tasks:
            await asyncio.gather(*list(self._bg_tasks), return_exceptions=True)

    async def _check_waste_alert(self, robot_id: str, state: Dict[str, Any], now: float) -> None:
        """Waste alert: trust the percentage only (ignore is_waste_drawer_full /
        status codes — the robot's full flag is flaky). Repeats every
        ``waste_repeat_seconds`` (default 3h) while at/above the threshold."""
        alert_cfg = self.config.get("alerts", {})
        waste_level = state.get("waste_drawer_level")
        threshold = alert_cfg.get("waste_threshold", 90)
        if waste_level is None or waste_level < threshold:
            return
        robot_name = state.get("name", robot_id)
        cooldown_key = f"{robot_id}:waste_pct"
        repeat = alert_cfg.get("waste_repeat_seconds", 10800)
        last = self._alert_cooldown.get(cooldown_key)
        if last is not None and now - last < repeat:
            return
        self._alert_cooldown[cooldown_key] = now
        prefix = "DRY-RUN (not sent) " if self.dry_run else ""
        self.logger.warning(f"{prefix}🗑️ {robot_name}: Waste drawer at {waste_level}%!")
        if self.telegram and not self.dry_run:
            try:
                await self.telegram.send_waste_alert(robot_name, waste_level)
            except Exception as e:
                self.logger.error(f"Telegram waste alert failed: {type(e).__name__}: {e}")

    async def _track_drawer_events(self, robot_id: str, state: Dict[str, Any]) -> None:
        """Track drawer full/emptied transitions (history for /shit)."""
        is_full = state.get("is_waste_drawer_full", False)
        status = canonical_status(state.get("status", ""))
        waste_level = state.get("waste_drawer_level")
        alert_cfg = self.config.get("alerts", {})
        waste_levels = canonical_set(alert_cfg.get("waste_levels", []))
        currently_full = is_full or status in waste_levels

        last_event = await self.db.get_last_drawer_event(robot_id)

        if currently_full:
            # Record 'full' event if last event wasn't already 'full'
            if not last_event or last_event["event_type"] != "full":
                await self.db.store_drawer_event(robot_id, "full", waste_level)
                self.logger.info(f"Drawer event: FULL (waste={waste_level}%)")
        else:
            # Record 'emptied' if transitioning from full to not-full
            if last_event and last_event["event_type"] == "full":
                await self.db.store_drawer_event(robot_id, "emptied", waste_level)
                self.logger.info(f"Drawer event: EMPTIED (waste={waste_level}%)")

    # ── activity history (Whisker keeps ~6 days; we keep everything) ─────────

    async def _fetch_activity(self, robot: Robot, limit: int) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        try:
            data = await asyncio.wait_for(
                robot._post(json={
                    "query": ACTIVITY_QUERY,
                    "variables": {"serial": robot.serial, "limit": limit, "consumer": "app"},
                }),
                timeout=30,
            )
            rows = (data or {}).get("data", {}).get("getLitterRobot4Activity")
            if rows is None:
                raise ValueError("no activity data in response")
            for row in rows:
                if not row.get("timestamp"):
                    continue
                entries.append({
                    "timestamp": parse_utc(row["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
                    "action": row.get("value") or row.get("measure") or "unknown",
                    "action_value": row.get("actionValue") or "",
                    "raw": row,
                })
            return entries
        except asyncio.TimeoutError:
            raise
        except Exception as e:
            # Raw query not supported (other model / API change): use the library call.
            self.logger.debug(f"Raw activity query failed ({e}); using get_activity_history")
        history = await asyncio.wait_for(robot.get_activity_history(limit=limit), timeout=30)
        for item in history:
            ts = item.timestamp
            if not isinstance(ts, datetime):
                ts = datetime(ts.year, ts.month, ts.day)
            entries.append({
                "timestamp": parse_utc(ts).strftime("%Y-%m-%d %H:%M:%S"),
                "action": str(_enum_str(item.action)),
                "action_value": "",
                "raw": None,
            })
        return entries

    async def _sync_activity(self, robot_id: str, force: bool = False) -> int:
        """Copy new activity-feed entries into our DB. Returns number of new rows."""
        robot = self.robots.get(robot_id)
        if not robot or not hasattr(robot, "get_activity_history"):
            return 0
        now = time.time()
        interval = min(self.activity_interval, 60) if self.fast_poll_reason(now) else self.activity_interval
        if not force and now - self._last_activity_sync.get(robot_id, 0) < interval:
            return 0
        self._last_activity_sync[robot_id] = now
        first_time = await self.db.count_activities(robot_id) == 0
        entries = await self._fetch_activity(robot, limit=1000 if first_time else 100)
        new = await self.db.store_activities(robot_id, entries)
        if not new:
            return 0
        if len(new) > 15:
            self.logger.info(f"ACTIVITY {robot.name}: stored {len(new)} new history entries")
        else:
            for e in sorted(new, key=lambda x: x["timestamp"]):
                value = f"={e['action_value']}" if e["action_value"] else ""
                source = (e.get("raw") or {}).get("commandSource")
                self.logger.info(
                    f"ACTIVITY {robot.name}: {fmt_local(e['timestamp'], '%Y-%m-%d %H:%M:%S %Z', self.tz_name)} "
                    f"{e['action']}{value}" + (f" (source={source})" if source else "")
                )
        return len(new)

    # ── pet weights ──────────────────────────────────────────────────────────

    async def _sync_pet_weight_history(self) -> int:
        if not self.account or not self.account.pets or not self.robots:
            return 0
        robot_id = next(iter(self.robots))
        total = 0
        for pet in self.account.pets:
            cat_name = self._normalize_pet_name(pet.name.upper())
            try:
                history = await asyncio.wait_for(
                    pet.fetch_weight_history(limit=100), timeout=20
                )
            except asyncio.TimeoutError:
                self.logger.warning(f"Weight history fetch timed out for {pet.name}")
                continue
            except Exception as e:
                self.logger.warning(f"Weight history fetch failed for {pet.name}: {e}")
                continue
            if not history:
                continue
            inserted = 0
            for measurement in history:
                ts = measurement.timestamp
                weight_lbs = measurement.weight
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                ts_str = ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                existing = await self.db._connection.execute(
                    """SELECT COUNT(*) FROM cat_visits
                       WHERE cat_name = ?
                       AND ABS(strftime('%s', timestamp) - strftime('%s', ?)) < 60""",
                    (cat_name, ts_str),
                )
                row = await existing.fetchone()
                if row and row[0] > 0:
                    continue
                weight_kg = round(weight_lbs * 0.453592, 2)
                await self.db.store_cat_visit(
                    robot_id=robot_id,
                    cat_name=cat_name,
                    weight_lbs=weight_lbs,
                    weight_kg=weight_kg,
                    timestamp_override=ts_str,
                )
                inserted += 1
            if inserted > 0:
                self.logger.info(f"✅ Synced {inserted} new {cat_name} visits")
            total += inserted
        return total

    def _normalize_pet_name(self, whisker_name: str) -> str:
        name_map = {"CHICA": "CHICKA", "CHICKA": "CHICKA", "PIXEL": "PIXEL"}
        return name_map.get(whisker_name.strip(), whisker_name.strip())

    # ── used by the Telegram command bot ─────────────────────────────────────

    def active_incidents(self) -> Dict[str, Dict[str, Any]]:
        """{'<robot name>: <incident key>': incident} for everything currently alerting."""
        out: Dict[str, Dict[str, Any]] = {}
        dets = list(self.detectors.values()) + ([self._api_detector] if self._api_detector else [])
        for det in dets:
            for key, inc in det.incidents.items():
                out[f"{det.name}: {key}"] = inc
        return out

    async def acknowledge_incidents(self) -> List[str]:
        """Silence reminder repeats for all active incidents."""
        acked: List[str] = []
        for robot_id, det in self.detectors.items():
            keys = det.acknowledge()
            if keys:
                acked += keys
                await self.db.set_kv(f"detector:{robot_id}", det.state)
        if self._api_detector:
            keys = self._api_detector.acknowledge()
            if keys:
                acked += keys
                await self.db.set_kv("detector:_api", self._api_detector.state)
        if acked:
            self.logger.info(f"Incidents acknowledged: {', '.join(acked)}")
        return acked

    async def make_bot_handler(self):
        """Return a TelegramBot wired to this check's DB and robot state, or None."""
        tg = self.config.get("telegram", {}) or {}
        # Fall back to shared notifier credentials if no per-check telegram block
        if tg.get("bot_token") and tg.get("chat_id"):
            bot_token = tg["bot_token"]
            chat_id = str(tg["chat_id"])
        elif self.notifier:
            bot_token = self.notifier.bot_token
            chat_id = self.notifier.chat_id
        else:
            return None

        # Ensure DB is ready
        await self._init_db()

        from telegram_bot import TelegramBot

        # TelegramBot uses monitor.robots and monitor._get_state_dict() —
        # LitterRobotCheck has both, so pass self as the monitor
        return TelegramBot(
            bot_token=bot_token,
            chat_id=chat_id,
            monitor=self,
            db=self.db,
            authorized_user_ids=getattr(self.telegram, "authorized_user_ids", None),
            tz_name=self.tz_name,
        )

    # ── read-only smoke test ─────────────────────────────────────────────────

    async def snapshot_once(self) -> List[Dict[str, Any]]:
        """Connect, take ONE snapshot per robot, store it (DB + JSONL), sync the
        activity feed and run detection in dry-run mode. Sends nothing, changes
        nothing on the robot."""
        if not self.dry_run:
            raise RuntimeError("snapshot_once() requires dry_run=True")
        await self._init_db()
        results: List[Dict[str, Any]] = []
        try:
            if not await self._connect(pet_sync=False, subscribe=False):
                raise RuntimeError("could not connect to Whisker")
            for robot_id, robot in self.robots.items():
                state = self._get_state_dict(robot)
                raw = copy.deepcopy(robot.to_dict())
                det = await self._get_detector(robot_id, robot.name)
                previous = dict(self._last_fields.get(robot_id) or {})
                prev_ts = (det.state.get("last") or {}).get("ts")
                alerts = await self.process_snapshot(robot_id, state, raw, source="smoke")
                fields = self._last_fields[robot_id]
                new_activity = await self._sync_activity(robot_id, force=True)
                results.append({
                    "name": robot.name,
                    "status": fields["status"],
                    "raw_keys": len(raw),
                    "fields": fields,
                    "previous_ts": prev_ts,
                    "changes": diff_fields(previous, fields),
                    "alerts": alerts,
                    "active_incidents": dict(det.incidents),
                    "new_activity": new_activity,
                    "activity_total": await self.db.count_activities(robot_id),
                    "jsonl": str(self.snapshot_log.path_for(
                        datetime.now(timezone.utc).strftime("%Y-%m-%d"))),
                    "db": str(self.db.db_path),
                })
            return results
        finally:
            await self._disconnect()
            await self.db.disconnect()
            self.db = None
