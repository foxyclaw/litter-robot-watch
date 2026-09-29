"""Litter Robot / Whisker monitoring check."""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pylitterbot import Account
from pylitterbot.robot import Robot

from checks.base import BaseCheck
from database import LitterRobotDatabase
from notifier import TelegramNotifier

STATUS_CODES = {
    "RDY": ("Ready", "normal"),
    "READY": ("Ready", "normal"),
    "CCP": ("Clean cycle in progress", "activity"),
    "CCC": ("Clean cycle complete", "activity"),
    "CSF": ("Cat sensor fault", "error"),
    "DF1": ("Drawer full - 2 cycles remaining", "warning"),
    "DF2": ("Drawer full - 1 cycle remaining", "warning"),
    "DFS": ("Drawer full - cycling stopped", "error"),
    "CST": ("Cat sensor triggered", "activity"),
    "CAT_SENSOR_TIMING": ("Cat sensor timing", "activity"),
    "CAT_SENSOR_INTERRUPTED": ("Cat sensor interrupted", "warning"),
    "CSI": ("Cat sensor interrupted", "warning"),
    "BR": ("Bonnet removed", "error"),
    "P": ("Paused", "warning"),
    "OFF": ("Off", "inactive"),
    "SDF": ("Startup drawer full", "error"),
    "DRAWER_FULL": ("Drawer full", "error"),
}


class LitterRobotCheck(BaseCheck):
    """Monitors Litter Robot state and alerts on issues."""

    def __init__(self, config: Dict[str, Any], notifier=None):
        super().__init__(config, notifier)
        self.logger = logging.getLogger("check.litter_robot")

        self.account: Optional[Account] = None
        self.robots: Dict[str, Robot] = {}
        self.db: Optional[LitterRobotDatabase] = None
        self._alert_cooldown: Dict[str, datetime] = {}
        self._last_pet_sync: float = 0.0
        self._connected: bool = False

        # Per-check Telegram (overrides shared notifier if configured)
        tg = config.get("telegram", {})
        if tg.get("bot_token") and tg.get("chat_id"):
            self.telegram = TelegramNotifier(tg["bot_token"], tg["chat_id"])
            self.logger.info("Litter Robot: using dedicated Telegram notifier")
        else:
            self.telegram = notifier  # fall back to shared notifier

    @property
    def name(self) -> str:
        return "litter_robot"

    async def _init_db(self) -> None:
        if self.db is None:
            db_path = self.config.get("database", "litter_robot.db")
            self.db = LitterRobotDatabase(db_path)
            await self.db.connect()
            self.logger.info(f"Database connected: {db_path}")

    async def _connect(self) -> bool:
        whisker = self.config.get("whisker", {})
        email = whisker.get("email")
        password = whisker.get("password")
        if not email or not password:
            self.logger.error("Missing whisker email/password in config")
            return False
        try:
            self.account = Account()
            await self.account.connect(
                username=email,
                password=password,
                load_robots=True,
                load_pets=True,
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

            # Initial pet weight sync (with timeout)
            try:
                synced = await asyncio.wait_for(self._sync_pet_weight_history(), timeout=60)
                if synced > 0:
                    self.logger.info(f"Startup pet weight sync: {synced} new entries")
                else:
                    self.logger.info("Startup pet weight sync: no new entries")
            except asyncio.TimeoutError:
                self.logger.warning("Startup pet weight sync timed out (60s) — will retry next cycle")
            except Exception as e:
                self.logger.warning(f"Startup pet weight sync failed: {e}")

            self._connected = True
            return True
        except Exception as e:
            self.logger.error(f"Whisker connect failed: {e}")
            self._connected = False
            return False

    async def run(self) -> None:
        """One monitoring iteration."""
        await self._init_db()

        if not self._connected:
            ok = await self._connect()
            if not ok:
                return
            # Skip refresh_robots on first run — _connect already loaded them
        else:
            try:
                self.logger.info("Refreshing robots...")
                await asyncio.wait_for(self.account.refresh_robots(), timeout=30)
                self.robots = {robot.id: robot for robot in self.account.robots}
                self.logger.info("Refresh complete")
            except asyncio.TimeoutError:
                self.logger.error("refresh_robots timed out (30s) — skipping this cycle")
                return
            except Exception as e:
                self.logger.error(f"refresh_robots failed: {e} — reconnecting next cycle")
                self._connected = False
                return

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

        for robot_id in self.robots:
            try:
                await self._check_robot(robot_id)
            except Exception as e:
                self.logger.error(f"check_robot({robot_id}) failed: {e}")

    # ── internal helpers (kept identical to original monitor.py logic) ──

    def _get_state_dict(self, robot: Robot) -> Dict[str, Any]:
        status_code = str(robot.status)
        if "." in status_code:
            status_code = status_code.split(".")[-1]
        status_desc, status_type = STATUS_CODES.get(
            status_code, (status_code.replace("_", " ").title(), "unknown")
        )
        waste_level = getattr(robot, "waste_drawer_level", None)
        cat_weight_lbs = getattr(robot, "pet_weight", None)
        cat_weight_kg = round(cat_weight_lbs * 0.453592, 2) if cat_weight_lbs else None
        cycles = getattr(robot, "cycle_count", None) or getattr(robot, "total_cycles", None)
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
                str(getattr(robot, "litter_level_state", "")).split(".")[-1]
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
        }

    async def _check_robot(self, robot_id: str) -> None:
        robot = self.robots.get(robot_id)
        if not robot:
            return
        state = self._get_state_dict(robot)
        self.logger.info(f"Checking {state.get('name')}: status={state.get('status')}, waste={state.get('waste_drawer_level')}%, full={state.get('is_waste_drawer_full')}")
        await self.db.store_state(robot_id, state)
        await self._check_alerts(robot_id, state)
        await self._track_drawer_events(robot_id, state)

    async def _check_alerts(self, robot_id: str, state: Dict[str, Any]) -> None:
        status = state.get("status", "")
        robot_name = state.get("name", robot_id)
        alert_cfg = self.config.get("alerts", {})
        waste_levels = alert_cfg.get("waste_levels", [])
        error_statuses = alert_cfg.get("error_statuses", [])

        # Primary drawer-full detection: use the boolean flag from the robot.
        # This fires even when % is below the threshold (e.g. 93%).
        is_full = state.get("is_waste_drawer_full", False)
        waste_level = state.get("waste_drawer_level")

        if is_full or status in waste_levels:
            pct_str = f" ({waste_level}%)" if waste_level is not None else ""
            await self._trigger_alert(
                robot_id, "waste_drawer",
                f"🗑️ {robot_name}: Drawer full{pct_str}! Empty the waste drawer.",
                telegram_only=True,
                waste_level=waste_level,
            )

        # Secondary: percentage threshold alert (only if not already full)
        if waste_level is not None and not is_full and status not in waste_levels:
            threshold = alert_cfg.get("waste_threshold", 95)
            if waste_level >= threshold:
                await self._trigger_alert(
                    robot_id, "waste_pct",
                    f"🗑️ {robot_name}: Waste drawer at {waste_level}%!",
                    telegram_only=True,
                    waste_level=waste_level,
                )

        if status in error_statuses:
            await self._trigger_alert(
                robot_id, "error",
                f"⚠️ {robot_name}: {state.get('status_description', status)}"
            )

        prev_state = await self.db.get_latest_state(robot_id)
        if prev_state:
            prev_status = prev_state.get("status", "")
            if status == "RDY" and prev_status in waste_levels + error_statuses:
                await self._trigger_alert(
                    robot_id, "resolved",
                    f"✅ {robot_name}: Issue resolved - now ready"
                )

    async def _track_drawer_events(self, robot_id: str, state: Dict[str, Any]) -> None:
        """Track drawer full/emptied transitions."""
        is_full = state.get("is_waste_drawer_full", False)
        status = state.get("status", "")
        waste_level = state.get("waste_drawer_level")
        alert_cfg = self.config.get("alerts", {})
        waste_levels = alert_cfg.get("waste_levels", [])
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

    async def _trigger_alert(
        self, robot_id: str, alert_type: str, message: str,
        telegram_only: bool = False, waste_level: int = None,
    ) -> None:
        cooldown_key = f"{robot_id}:{alert_type}"
        now = datetime.now()
        cooldown_seconds = 3600 if telegram_only else 300
        if cooldown_key in self._alert_cooldown:
            if (now - self._alert_cooldown[cooldown_key]).total_seconds() < cooldown_seconds:
                return
        self._alert_cooldown[cooldown_key] = now

        if not telegram_only:
            await self.db.store_alert(robot_id, alert_type, message, "active")

        self.logger.warning(message)

        if self.telegram and telegram_only:
            try:
                robot = self.robots.get(robot_id)
                rname = robot.name if robot else "Litter Robot"
                level = waste_level if waste_level is not None else 100
                await self.telegram.send_waste_alert(rname, level)
            except Exception as e:
                self.logger.error(f"Telegram waste alert failed: {e}")

        if self.telegram and alert_type == "error":
            try:
                robot = self.robots.get(robot_id)
                rname = robot.name if robot else "Litter Robot"
                await self.telegram.send_error_alert(
                    rname, alert_type,
                    message.replace(f"⚠️ {rname}: ", "")
                )
            except Exception as e:
                self.logger.error(f"Telegram error alert failed: {e}")

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

    async def make_bot_handler(self):
        """Return a TelegramBot wired to this check's DB and robot state, or None."""
        tg = self.config.get("telegram", {})
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

        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from telegram_bot import TelegramBot

        # TelegramBot uses monitor.robots and monitor._get_state_dict() —
        # LitterRobotCheck has both, so pass self as the monitor
        bot = TelegramBot(
            bot_token=bot_token,
            chat_id=chat_id,
            monitor=self,
            db=self.db,
        )
        return bot
