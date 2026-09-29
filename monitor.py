"""Core monitoring logic for Litter Robot."""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Callable
from pylitterbot import Account
from pylitterbot.robot import Robot

from database import LitterRobotDatabase
from notifier import TelegramNotifier


# Status code descriptions
STATUS_CODES = {
    "RDY": ("Ready", "normal"),
    "READY": ("Ready", "normal"),  # Alternate format from API
    "CCP": ("Clean cycle in progress", "activity"),
    "CCC": ("Clean cycle complete", "activity"),
    "CSF": ("Cat sensor fault", "error"),
    "DF1": ("Drawer full - 2 cycles remaining", "warning"),
    "DF2": ("Drawer full - 1 cycle remaining", "warning"),
    "DFS": ("Drawer full - cycling stopped", "error"),
    "CST": ("Cat sensor triggered", "activity"),
    "CAT_SENSOR_TIMING": ("Cat sensor timing", "activity"),  # LR4 variant of CST
    "CAT_SENSOR_INTERRUPTED": ("Cat sensor interrupted", "warning"),  # LR4 variant of CSI
    "CSI": ("Cat sensor interrupted", "warning"),
    "BR": ("Bonnet removed", "error"),
    "P": ("Paused", "warning"),
    "OFF": ("Off", "inactive"),
    "SDF": ("Startup drawer full", "error"),
    "DRAWER_FULL": ("Drawer full", "error"),
}


class LitterRobotMonitor:
    """Monitors Litter Robot state and alerts on issues."""

    def __init__(self, config: Dict[str, Any], db: LitterRobotDatabase):
        self.config = config
        self.db = db
        self.logger = logging.getLogger(__name__)

        self.account: Optional[Account] = None
        self.robots: Dict[str, Robot] = {}
        self._running = False
        self._alert_cooldown: Dict[str, datetime] = {}
        self._alert_handlers: list[Callable[[str, str, str], None]] = []

        # Initialize Telegram notifier
        self.telegram: Optional[TelegramNotifier] = None
        notif_config = config.get("notifications", {})
        tg_config = notif_config.get("telegram", {})
        if tg_config.get("enabled") and tg_config.get("bot_token") and tg_config.get("chat_id"):
            self.telegram = TelegramNotifier(
                tg_config["bot_token"],
                tg_config["chat_id"]
            )
            self.logger.info("Telegram notifications enabled")

    def add_alert_handler(self, handler: Callable[[str, str, str], None]) -> None:
        """Add a handler to be called when alerts are triggered."""
        self._alert_handlers.append(handler)

    async def connect(self, email: str, password: str) -> bool:
        """Connect to Whisker cloud API."""
        try:
            self.account = Account()
            await self.account.connect(
                username=email,
                password=password,
                load_robots=True,
                load_pets=True
            )

            self.robots = {robot.id: robot for robot in self.account.robots}
            self.logger.info(f"Connected. Found {len(self.robots)} robot(s), {len(self.account.pets)} pet(s):")
            for pet in self.account.pets:
                self.logger.info(f"  - Pet: {pet.name} (id={pet.id})")
            if not self.account.pets:
                self.logger.warning("No pet profiles loaded - weight history fallback unavailable")
            for robot_id, robot in self.robots.items():
                self.logger.info(f"  - {robot.name} ({robot_id})")

            # Initial sync of pet weight history on startup
            try:
                synced = await self.sync_pet_weight_history()
                if synced > 0:
                    self.logger.info(f"Startup pet weight history sync: {synced} new entries")
            except Exception as e:
                self.logger.warning(f"Startup pet weight history sync failed: {e}")

            return True

        except Exception as e:
            self.logger.error(f"Failed to connect: {e}")
            return False

    async def disconnect(self) -> None:
        """Disconnect from Whisker cloud API."""
        if self.account:
            await self.account.disconnect()
            self.account = None
            self.robots = {}

    async def refresh_robots(self) -> None:
        """Refresh robot data from API."""
        if not self.account:
            return

        await self.account.refresh_robots()
        self.robots = {robot.id: robot for robot in self.account.robots}

    def _get_state_dict(self, robot: Robot) -> Dict[str, Any]:
        """Extract state from robot object."""
        status_code = str(robot.status)
        # Handle enum values like "LitterBoxStatus.DRAWER_FULL"
        if "." in status_code:
            status_code = status_code.split(".")[-1]
        status_desc, status_type = STATUS_CODES.get(status_code, (status_code.replace("_", " ").title(), "unknown"))

        # Get waste drawer level (LR4 uses waste_drawer_level)
        waste_level = getattr(robot, "waste_drawer_level", None)

        # Get cat weight (LR4 uses pet_weight in lbs)
        cat_weight_lbs = getattr(robot, "pet_weight", None)
        # Convert lbs to kg if needed for display
        cat_weight_kg = round(cat_weight_lbs * 0.453592, 2) if cat_weight_lbs else None

        # Get last seen time
        last_seen = getattr(robot, "last_seen", None)

        # Get cycle count
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
            "litter_level_state": str(getattr(robot, "litter_level_state", "")).split(".")[-1] if hasattr(robot, "litter_level_state") else None,
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
            "last_seen": last_seen,
        }

    async def check_robot(self, robot_id: str) -> Dict[str, Any]:
        """Check a single robot and store state."""
        robot = self.robots.get(robot_id)
        if not robot:
            self.logger.warning(f"Robot {robot_id} not found")
            return {}

        # Get current state
        state = self._get_state_dict(robot)

        # Store in database
        await self.db.store_state(robot_id, state)

        # Check for alerts
        await self._check_alerts(robot_id, state)

        return state

    async def sync_pet_weight_history(self) -> int:
        """Sync pet weight history from Whisker profiles into cat_visits DB.

        Returns number of new entries inserted.
        """
        if not self.account or not self.account.pets:
            self.logger.debug("sync_pet_weight_history: no pets available")
            return 0

        # Use first robot ID (we have one robot)
        if not self.robots:
            self.logger.debug("sync_pet_weight_history: no robots")
            return 0
        robot_id = next(iter(self.robots))

        total_inserted = 0

        for pet in self.account.pets:
            cat_name = self._normalize_pet_name(pet.name.upper())
            try:
                history = await pet.fetch_weight_history(limit=100)
            except Exception as e:
                self.logger.warning(f"sync_pet_weight_history: failed to fetch history for {pet.name}: {e}")
                continue

            if not history:
                self.logger.debug(f"sync_pet_weight_history: {pet.name} has no weight history")
                continue

            self.logger.debug(f"sync_pet_weight_history: {pet.name} ({cat_name}) has {len(history)} entries")
            inserted = 0

            for measurement in history:
                ts = measurement.timestamp
                weight_lbs = measurement.weight

                # Normalize timestamp to UTC-aware datetime
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                # Format as UTC string for DB storage (strip timezone offset)
                ts_utc = ts.astimezone(timezone.utc)
                ts_str = ts_utc.strftime("%Y-%m-%d %H:%M:%S")

                # Check if already recorded (within 60 seconds of this timestamp)
                existing = await self.db._connection.execute(
                    """SELECT COUNT(*) FROM cat_visits
                       WHERE cat_name = ?
                       AND ABS(strftime('%s', timestamp) - strftime('%s', ?)) < 60""",
                    (cat_name, ts_str)
                )
                row = await existing.fetchone()
                if row and row[0] > 0:
                    continue  # already in DB

                # Insert new entry
                weight_kg = round(weight_lbs * 0.453592, 2)
                await self.db.store_cat_visit(
                    robot_id=robot_id,
                    cat_name=cat_name,
                    weight_lbs=weight_lbs,
                    weight_kg=weight_kg,
                    timestamp_override=ts_str
                )
                inserted += 1

            if inserted > 0:
                self.logger.info(f"✅ Synced {inserted} new {cat_name} visits from pet profile")
            else:
                self.logger.debug(f"sync_pet_weight_history: {cat_name} - no new entries to sync")
            total_inserted += inserted

        return total_inserted

    def _normalize_pet_name(self, whisker_name: str) -> str:
        """Normalize Whisker pet names to internal DB names."""
        name = whisker_name.upper().strip()
        # Map known name variants
        name_map = {
            "CHICA": "CHICKA",
            "CHICKA": "CHICKA",
            "PIXEL": "PIXEL",
        }
        return name_map.get(name, name)

    async def _check_alerts(self, robot_id: str, state: Dict[str, Any]) -> None:
        """Check if alerts should be triggered."""
        status = state.get("status", "")
        robot_name = state.get("name", robot_id)

        alert_config = self.config.get("alerts", {})
        waste_levels = alert_config.get("waste_levels", [])
        error_statuses = alert_config.get("error_statuses", [])

        # Check waste drawer level
        if status in waste_levels:
            await self._trigger_alert(
                robot_id,
                "waste_drawer",
                f"🗑️ {robot_name}: {state.get('status_description', status)}"
            )

        # Check waste drawer percentage threshold
        waste_level = state.get("waste_drawer_level")
        if waste_level is not None:
            threshold = alert_config.get("waste_threshold", 95)
            if waste_level >= threshold:
                # Check cooldown for waste percentage alerts
                cooldown_key = f"{robot_id}:waste_pct"
                now = datetime.now()
                if cooldown_key in self._alert_cooldown:
                    last_alert = self._alert_cooldown[cooldown_key]
                    if (now - last_alert).total_seconds() >= 3600:  # 1 hour cooldown
                        # Reset after 1 hour to allow re-alerting
                        del self._alert_cooldown[cooldown_key]

                await self._trigger_alert(
                    robot_id,
                    "waste_pct",
                    f"🗑️ {robot_name}: Waste drawer at {waste_level}%!",
                    telegram_only=True,
                    waste_level=waste_level
                )

        # Check error statuses
        if status in error_statuses:
            await self._trigger_alert(
                robot_id,
                "error",
                f"⚠️ {robot_name}: {state.get('status_description', status)}"
            )

        # Special case: ready after being in an error state
        prev_state = await self.db.get_latest_state(robot_id)
        if prev_state:
            prev_status = prev_state.get("status", "")
            if status == "RDY" and prev_status in waste_levels + error_statuses:
                await self._trigger_alert(
                    robot_id,
                    "resolved",
                    f"✅ {robot_name}: Issue resolved - now ready"
                )

    async def _trigger_alert(self, robot_id: str, alert_type: str, message: str,
                            telegram_only: bool = False, waste_level: int = None) -> None:
        """Trigger an alert with cooldown."""
        # Cooldown key
        cooldown_key = f"{robot_id}:{alert_type}"
        now = datetime.now()

        # Check cooldown (5 minutes for regular, 1 hour for waste %)
        cooldown_seconds = 300 if not telegram_only else 3600
        if cooldown_key in self._alert_cooldown:
            last_alert = self._alert_cooldown[cooldown_key]
            if (now - last_alert).total_seconds() < cooldown_seconds:
                return

        # Update cooldown
        self._alert_cooldown[cooldown_key] = now

        # Store in database
        if not telegram_only:
            await self.db.store_alert(robot_id, alert_type, message, "active")

        # Log
        self.logger.warning(message)

        # Send Telegram notification for waste alerts
        if self.telegram and telegram_only and waste_level is not None:
            try:
                robot = self.robots.get(robot_id)
                robot_name = robot.name if robot else "Litter Robot"
                await self.telegram.send_waste_alert(robot_name, waste_level)
            except Exception as e:
                self.logger.error(f"Telegram notification failed: {e}")

        # Send Telegram for error alerts too
        if self.telegram and alert_type == "error":
            try:
                robot = self.robots.get(robot_id)
                robot_name = robot.name if robot else "Litter Robot"
                await self.telegram.send_error_alert(
                    robot_name,
                    alert_type,
                    message.replace(f"⚠️ {robot_name}: ", "")
                )
            except Exception as e:
                self.logger.error(f"Telegram error notification failed: {e}")

        # Call handlers
        for handler in self._alert_handlers:
            try:
                handler(robot_id, alert_type, message)
            except Exception as e:
                self.logger.error(f"Alert handler failed: {e}")

    async def run(self) -> None:
        """Main monitoring loop."""
        self._running = True
        last_pet_sync = 0.0
        poll_interval = self.config.get("monitor", {}).get("poll_interval", 30)

        self.logger.info(f"Starting monitor loop (interval: {poll_interval}s)")

        while self._running:
            try:
                # Refresh robot data
                await self.refresh_robots()

                # Sync pet weight history every 30 minutes
                now_ts = time.time()
                if now_ts - last_pet_sync > 1800:  # 30 minutes
                    synced = await self.sync_pet_weight_history()
                    if synced > 0:
                        self.logger.info(f"Pet weight history sync: {synced} new entries")
                    last_pet_sync = now_ts

                # Check each robot
                for robot_id in self.robots:
                    await self.check_robot(robot_id)

            except Exception as e:
                self.logger.error(f"Error in monitor loop: {e}")

            # Wait for next poll
            await asyncio.sleep(poll_interval)

    def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        self.logger.info("Monitor stopped")

    async def get_current_status(self) -> Dict[str, Dict[str, Any]]:
        """Get current status of all robots."""
        await self.refresh_robots()
        return {
            robot_id: self._get_state_dict(robot)
            for robot_id, robot in self.robots.items()
        }
