"""Telegram bot command handler — polls for updates and responds to commands."""
import asyncio
import logging
from typing import Any, Callable, Dict, Optional

import aiohttp


class TelegramCommandBot:
    """Long-polls Telegram and dispatches commands to registered handlers."""

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.logger = logging.getLogger("telegram_bot")
        self._offset: int = 0
        # command → async callable(chat_id, args) → str (reply text, HTML)
        self._handlers: Dict[str, Callable] = {}

    def register(self, command: str, handler: Callable) -> None:
        """Register an async handler for a command (e.g. '/price') or keyword (e.g. 'price')."""
        self._handlers[command.lower()] = handler
        self.logger.info(f"Bot: registered trigger '{command}'")

    async def run_forever(self) -> None:
        self.logger.info("Telegram bot polling started")
        failures = 0
        while True:
            try:
                updates = await self._get_updates()
                if failures:
                    self.logger.info(f"Bot polling recovered after {failures} failed attempt(s)")
                    failures = 0
                for update in updates:
                    await self._handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failures += 1
                if failures == 1:  # one warning per outage, not one error per attempt
                    reason = str(e).replace(self.bot_token, "<token>")
                    self.logger.warning(f"Bot polling failed ({type(e).__name__}: {reason}) — retrying with backoff")
                await asyncio.sleep(min(120, 2 * 2 ** min(failures - 1, 6)))

    async def _get_updates(self):
        params = {"offset": self._offset, "timeout": 30, "allowed_updates": ["message"]}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/getUpdates",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=40),
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"getUpdates HTTP {resp.status}")
                    data = await resp.json()
                    updates = data.get("result", [])
                    if updates:
                        self._offset = updates[-1]["update_id"] + 1
                    return updates
        except asyncio.TimeoutError:
            return []

    async def _handle_update(self, update: Dict[str, Any]) -> None:
        msg = update.get("message", {})
        text = (msg.get("text") or "").strip()
        chat_id = msg.get("chat", {}).get("id")
        if not text or not chat_id:
            return

        parts = text.split()
        # First word, strip leading / and @BotName suffix
        first = parts[0].lower().lstrip("/").split("@")[0]
        args = parts[1:]

        # Match exact first-word trigger (slash or keyword)
        handler = self._handlers.get("/" + first) or self._handlers.get(first)
        if handler:
            self.logger.info(f"Bot: '{first}' from chat {chat_id}")
            try:
                reply = await handler(chat_id, args)
                if reply:
                    await self._send(chat_id, reply)
            except Exception as e:
                self.logger.error(f"Handler '{first}' failed: {e}")
                await self._send(chat_id, f"❌ Error: {e}")

    async def _send(self, chat_id: int, text: str) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{self.base_url}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                    timeout=aiohttp.ClientTimeout(total=10),
                )
        except Exception as e:
            self.logger.error(f"send_message failed: {e}")
