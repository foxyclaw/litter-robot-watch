"""Telegram notifications with retry.

``send_message``  — immediate delivery with a few quick retries; returns bool.
``send_reliable`` — same, but if delivery still fails the message is queued and
retried in the background with exponential backoff, so an alert is not lost
during a network blip. The queue is optionally persisted to a JSON file so it
also survives a restart.
``send_critical`` — ``send_reliable`` to the group AND to every user in
``alert_dm_user_ids`` as a direct message (same retry queue). A user who never
started the bot answers 403: that is logged as a WARNING once per user per day
and never queued.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

RETRY_BASE_SECONDS = 5.0
RETRY_MAX_SECONDS = 300.0
MAX_QUEUE_AGE_SECONDS = 24 * 3600
MAX_QUEUE_LENGTH = 200


def backoff_delay(attempt: int, base: float = RETRY_BASE_SECONDS,
                  cap: float = RETRY_MAX_SECONDS) -> float:
    """Exponential backoff: base, 2*base, 4*base ... capped."""
    return min(cap, base * (2 ** max(0, attempt - 1)))


class TelegramNotifier:
    """Send Telegram notifications for alerts."""

    def __init__(self, bot_token: str, chat_id: str, outbox_path: Optional[str] = None):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.logger = logging.getLogger(__name__)
        self.authorized_user_ids: List[str] = []  # used by the command bot
        self.alert_dm_user_ids: List[str] = []    # critical alerts are also DMed to these
        self.last_status: Optional[int] = None    # HTTP status of the last attempt (None = network error)
        self._forbidden_logged: Dict[str, str] = {}  # user id -> day the 403 warning was logged
        self._outbox_path = Path(outbox_path) if outbox_path else None
        self._queue: List[Dict[str, Any]] = self._load_outbox()
        self._retry_task: Optional[asyncio.Task] = None

    # ── low level ────────────────────────────────────────────────────────────

    async def _post(self, message: str, chat_id: Optional[str] = None) -> Tuple[bool, bool, float]:
        """One delivery attempt. Returns (ok, retryable, retry_after_seconds)."""
        payload = {
            "chat_id": str(chat_id or self.chat_id),
            "text": message,
            "parse_mode": "HTML",
            "disable_notification": False,
        }
        self.last_status = None
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.post(f"{self.base_url}/sendMessage", json=payload) as response:
                    self.last_status = response.status
                    if response.status == 200:
                        return True, False, 0.0
                    text = await response.text()
                    retry_after = 0.0
                    if response.status == 429:
                        try:
                            retry_after = float(json.loads(text)["parameters"]["retry_after"])
                        except Exception:
                            retry_after = 5.0
                    retryable = response.status == 429 or response.status >= 500
                    if not self._is_forbidden_dm(payload["chat_id"]):  # that one is logged by the caller
                        self.logger.error(f"Telegram API error: {response.status} - {text[:300]}")
                    return False, retryable, retry_after
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Never log the exception repr of the URL (it contains the bot token).
            self.logger.warning(f"Telegram send failed: {type(e).__name__}: {self._scrub(str(e))}")
            return False, True, 0.0

    def _scrub(self, text: str) -> str:
        return text.replace(self.bot_token, "<token>")

    def _is_forbidden_dm(self, chat_id: Optional[str]) -> bool:
        """Last attempt was a DM answered with 403 (user never started / blocked the bot)."""
        return self.last_status == 403 and chat_id is not None and str(chat_id) != self.chat_id

    def _log_rejected(self, chat_id: Optional[str], what: str) -> None:
        """Log a non-retryable rejection; a 403 on a DM only once per user per day."""
        if not self._is_forbidden_dm(chat_id):
            self.logger.error(f"{what} rejected (not retryable) — dropped")
            return
        user, today = str(chat_id), time.strftime("%Y-%m-%d")
        if self._forbidden_logged.get(user) != today:
            self._forbidden_logged[user] = today
            self.logger.warning(
                f"Telegram DM to user {user} failed with 403 — they must open the bot and "
                f"press Start before it can message them. Not retried (logged once per day)."
            )

    async def send_message(self, message: str, chat_id: Optional[str] = None,
                           attempts: int = 3) -> bool:
        """Send now, with a few quick retries. Returns True if delivered."""
        for attempt in range(1, max(1, attempts) + 1):
            ok, retryable, retry_after = await self._post(message, chat_id)
            if ok:
                self.logger.info(f"Telegram message sent to {chat_id or self.chat_id}")
                return True
            if not retryable:
                self._log_rejected(chat_id, "Telegram message")
                return False
            if attempt == attempts:
                return False
            await asyncio.sleep(max(retry_after, float(attempt)))
        return False

    # ── reliable delivery ────────────────────────────────────────────────────

    async def send_reliable(self, message: str, chat_id: Optional[str] = None) -> bool:
        """Send now; on failure queue for background retry.

        Returns True if delivered immediately, False if queued (or dropped as
        non-retryable).
        """
        ok, retryable, _ = await self._post(message, chat_id)
        if ok:
            self.logger.info(f"Telegram message sent to {chat_id or self.chat_id}")
            return True
        if not retryable:
            self._log_rejected(chat_id, "Telegram message")
            return False
        self._enqueue(message, chat_id)
        return False

    async def send_critical(self, message: str) -> bool:
        """Critical alert: group chat plus a DM to every ``alert_dm_user_ids`` user.

        Every copy goes through ``send_reliable`` (queued + retried on network
        errors). Returns the result of the group send.
        """
        ok = await self.send_reliable(message)
        for user_id in dict.fromkeys(str(u) for u in self.alert_dm_user_ids):
            if user_id == self.chat_id:
                continue
            try:
                await self.send_reliable(message, chat_id=user_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a broken DM must never block the others
                self.logger.error(f"Telegram DM to {user_id} failed: {type(e).__name__}")
        return ok

    @property
    def pending(self) -> int:
        return len(self._queue)

    def _enqueue(self, message: str, chat_id: Optional[str]) -> None:
        self._queue.append({"text": message, "chat_id": str(chat_id or self.chat_id),
                            "queued_at": time.time(), "attempts": 1})
        if len(self._queue) > MAX_QUEUE_LENGTH:
            dropped = len(self._queue) - MAX_QUEUE_LENGTH
            del self._queue[:dropped]
            self.logger.error(f"Telegram outbox full — dropped {dropped} oldest message(s)")
        self._save_outbox()
        self.logger.warning(f"Telegram delivery failed — queued for retry ({len(self._queue)} pending)")
        self.ensure_retry_task()

    def ensure_retry_task(self) -> None:
        """Start the background retry loop if there is anything queued."""
        if not self._queue:
            return
        if self._retry_task is None or self._retry_task.done():
            try:
                self._retry_task = asyncio.get_running_loop().create_task(self._retry_loop())
            except RuntimeError:
                pass  # no loop yet; next send will start it

    async def flush(self) -> int:
        """Try to deliver everything queued, once. Returns messages still pending."""
        remaining: List[Dict[str, Any]] = []
        now = time.time()
        for i, item in enumerate(self._queue):
            age = now - item["queued_at"]
            if age > MAX_QUEUE_AGE_SECONDS:
                self.logger.error(f"Telegram message undeliverable for {age / 3600:.0f}h — dropped")
                continue
            text = item["text"]
            if age > 120:
                text = f"<i>(delayed {int(age // 60)} min — Telegram was unreachable)</i>\n{text}"
            ok, retryable, _ = await self._post(text, item.get("chat_id"))
            if ok:
                self.logger.info(f"Queued Telegram message delivered after {int(age)}s")
                continue
            if not retryable:
                self._log_rejected(item.get("chat_id"), "Queued Telegram message")
                continue
            item["attempts"] = item.get("attempts", 1) + 1
            remaining.append(item)
            remaining.extend(self._queue[i + 1:])  # still down: keep order, stop hammering
            break
        self._queue[:] = remaining
        self._save_outbox()
        return len(self._queue)

    async def _retry_loop(self) -> None:
        attempt = 1
        while self._queue:
            await asyncio.sleep(backoff_delay(attempt))
            before = len(self._queue)
            try:
                left = await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.error(f"Telegram retry loop error: {type(e).__name__}")
                left = len(self._queue)
            attempt = 1 if left < before else attempt + 1

    def _load_outbox(self) -> List[Dict[str, Any]]:
        if not self._outbox_path or not self._outbox_path.exists():
            return []
        try:
            data = json.loads(self._outbox_path.read_text())
            return [d for d in data if isinstance(d, dict) and d.get("text")]
        except Exception:
            return []

    def _save_outbox(self) -> None:
        if not self._outbox_path:
            return
        try:
            self._outbox_path.parent.mkdir(parents=True, exist_ok=True)
            self._outbox_path.write_text(json.dumps(self._queue))
        except Exception as e:
            self.logger.warning(f"Could not persist Telegram outbox: {e}")

    # ── message helpers ──────────────────────────────────────────────────────

    async def send_waste_alert(self, robot_name: str, level: int) -> bool:
        """Send waste drawer level alert."""
        message = (
            f"🗑️ <b>{robot_name}</b>\n"
            f"Waste drawer at <b>{level}%</b> — time to empty!\n"
            f"\n<i>LR will stop cycling soon if not emptied.</i>"
        )
        return await self.send_reliable(message)

    async def send_error_alert(self, robot_name: str, status: str, description: str) -> bool:
        """Send error status alert."""
        message = (
            f"⚠️ <b>{robot_name} Issue</b>\n"
            f"Status: <code>{status}</code>\n"
            f"{description}"
        )
        return await self.send_reliable(message)

    async def send_resolved(self, robot_name: str, status: str) -> bool:
        """Send issue resolved notification."""
        message = f"✅ <b>{robot_name}</b> — Issue resolved! Now {status}"
        return await self.send_reliable(message)
