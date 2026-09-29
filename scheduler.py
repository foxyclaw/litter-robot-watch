"""General-purpose check scheduler — runs checks on independent intervals."""
import asyncio
import logging
import time
from typing import Any, Dict, List


class CheckScheduler:
    def __init__(self):
        self._checks: List[Dict[str, Any]] = []
        self.logger = logging.getLogger("scheduler")

    def add_check(self, check, interval_seconds: int) -> None:
        self._checks.append({
            "check": check,
            "interval": interval_seconds,
            "last_run": 0.0,
        })
        self.logger.info(f"Registered: {check.name} (every {interval_seconds}s)")

    async def run_forever(self) -> None:
        self.logger.info(f"Scheduler started — {len(self._checks)} check(s) active")
        while True:
            now = time.time()
            for entry in self._checks:
                if now - entry["last_run"] >= entry["interval"]:
                    check = entry["check"]
                    try:
                        self.logger.debug(f"→ Running {check.name}")
                        await check.run()
                    except Exception as e:
                        self.logger.error(f"{check.name} raised: {e}", exc_info=True)
                    entry["last_run"] = time.time()
            await asyncio.sleep(5)
