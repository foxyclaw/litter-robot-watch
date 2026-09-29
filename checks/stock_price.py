"""Stock price monitoring check using Alpaca Markets API."""
import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

import aiohttp

from checks.base import BaseCheck
from notifier import TelegramNotifier


class StockPriceCheck(BaseCheck):
    """Checks stock prices via Alpaca and sends alerts on threshold crosses."""

    ALPACA_SNAPSHOT_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/snapshot"

    def __init__(self, config: Dict[str, Any], notifier=None):
        super().__init__(config, notifier)
        self.logger = logging.getLogger("check.stock_price")

        # Dedicated Telegram notifier for this check (separate bot + chat)
        tg = config.get("telegram", {})
        if tg.get("bot_token") and tg.get("chat_id"):
            self.telegram = TelegramNotifier(tg["bot_token"], tg["chat_id"])
            self.logger.info("StockPrice: using dedicated Telegram notifier")
        else:
            self.telegram = notifier  # fallback to shared

        self._state: Dict[str, Any] = self._load_state()

    @property
    def name(self) -> str:
        return "stock_price"

    # ── state persistence ──────────────────────────────────────────────────

    def _state_file(self) -> str:
        return self.config.get(
            "state_file",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "stock_price_state.json"),
        )

    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self._state_file()) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            with open(self._state_file(), "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:
            self.logger.warning(f"Could not save state: {e}")

    # ── Alpaca API ─────────────────────────────────────────────────────────

    async def _fetch_price(self, symbol: str) -> Optional[float]:
        api_key = self.config.get("alpaca_key")
        api_secret = self.config.get("alpaca_secret")
        if not api_key or not api_secret:
            self.logger.error("Missing alpaca_key / alpaca_secret in config")
            return None
        url = self.ALPACA_SNAPSHOT_URL.format(symbol=symbol)
        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        self.logger.error(f"Alpaca API {symbol}: HTTP {resp.status}")
                        return None
                    data = await resp.json()
                    price = data.get("latestTrade", {}).get("p")
                    if price is None:
                        self.logger.warning(f"No latestTrade.p for {symbol}: {data}")
                    return float(price) if price is not None else None
        except Exception as e:
            self.logger.error(f"Alpaca fetch failed for {symbol}: {e}")
            return None

    # ── main run ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        watches = self.config.get("watches", [])
        if not watches:
            return

        for watch in watches:
            symbol = watch.get("symbol")
            threshold = watch.get("threshold")
            direction = watch.get("direction", "above")  # "above" | "below"
            if not symbol or threshold is None:
                continue

            price = await self._fetch_price(symbol)
            if price is None:
                continue

            self.logger.info(f"{symbol}: ${price:.2f} (threshold: {direction} ${threshold})")

            sym_state = self._state.setdefault(symbol, {"alerted": False, "last_price": None})
            sym_state["last_price"] = price

            triggered = (
                price >= threshold if direction == "above" else price <= threshold
            )

            if triggered and not sym_state["alerted"]:
                # Send alert
                emoji = "📈" if direction == "above" else "📉"
                msg = (
                    f"{emoji} <b>{symbol}</b>: ${price:.2f}\n"
                    f"Threshold {direction} ${threshold} reached!"
                )
                self.logger.warning(f"ALERT: {symbol} ${price:.2f} ({direction} ${threshold})")
                if self.telegram:
                    try:
                        await self.telegram.send_message(msg)
                    except Exception as e:
                        self.logger.error(f"Telegram alert failed: {e}")
                sym_state["alerted"] = True

            elif not triggered and sym_state["alerted"]:
                # Reset — price moved away from threshold
                reset_msg = (
                    f"✅ <b>{symbol}</b>: ${price:.2f} — back {'below' if direction == 'above' else 'above'} ${threshold}"
                )
                self.logger.info(f"RESET: {symbol} back to normal")
                if self.telegram:
                    try:
                        await self.telegram.send_message(reset_msg)
                    except Exception as e:
                        self.logger.error(f"Telegram reset msg failed: {e}")
                sym_state["alerted"] = False

        self._save_state()

    async def make_bot_handler(self):
        """Return a TelegramCommandBot pre-wired with /price command, or None."""
        tg = self.config.get("telegram", {})
        bot_token = tg.get("bot_token")
        if not bot_token or bot_token.startswith("REPLACE"):
            return None

        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from telegram_command_bot import TelegramCommandBot

        bot = TelegramCommandBot(bot_token)
        bot.register("/price", self._cmd_price)
        bot.register("price", self._cmd_price)
        bot.register("goog", self._cmd_price)
        bot.register("google", self._cmd_price)
        return bot

    async def _cmd_price(self, chat_id: int, args: list) -> str:
        """Handle /price [SYMBOL] command."""
        watches = self.config.get("watches", [])
        if not watches:
            return "❌ No symbols configured."

        # If specific symbol requested, filter; otherwise show all
        requested = args[0].upper() if args else None
        targets = (
            [w for w in watches if w["symbol"].upper() == requested]
            if requested else watches
        )
        if not targets:
            return f"❌ Symbol <b>{requested}</b> not configured."

        lines = []
        for watch in targets:
            symbol = watch["symbol"]
            threshold = watch.get("threshold")
            direction = watch.get("direction", "above")
            price = await self._fetch_price(symbol)
            if price is None:
                lines.append(f"❓ <b>{symbol}</b>: could not fetch price")
                continue
            arrow = "📈" if direction == "above" else "📉"
            status = (
                f"{arrow} above threshold!"
                if (direction == "above" and price >= threshold)
                else (f"{arrow} below threshold!" if (direction == "below" and price <= threshold) else "✅ normal")
            )
            lines.append(
                f"<b>{symbol}</b>: <b>${price:.2f}</b>\n"
                f"Threshold: {'≥' if direction == 'above' else '≤'} ${threshold} — {status}"
            )
        return "\n\n".join(lines)
