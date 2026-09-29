"""Notification handlers for Litter Robot Monitor."""
import logging
import aiohttp
from typing import Optional


class TelegramNotifier:
    """Send Telegram notifications for alerts."""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.logger = logging.getLogger(__name__)
        
    async def send_message(self, message: str) -> bool:
        """Send a message via Telegram bot."""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/sendMessage"
                payload = {
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_notification": False
                }
                
                async with session.post(url, json=payload) as response:
                    if response.status == 200:
                        self.logger.info(f"Telegram message sent to {self.chat_id}")
                        return True
                    else:
                        text = await response.text()
                        self.logger.error(f"Telegram API error: {response.status} - {text}")
                        return False
                        
        except Exception as e:
            self.logger.error(f"Failed to send Telegram message: {e}")
            return False
    
    async def send_waste_alert(self, robot_name: str, level: int) -> bool:
        """Send waste drawer level alert."""
        message = (
            f"🗑️ <b>{robot_name}</b>\n"
            f"Waste drawer at <b>{level}%</b> — time to empty!\n"
            f"\n<i>LR will stop cycling soon if not emptied.</i>"
        )
        return await self.send_message(message)
    
    async def send_error_alert(self, robot_name: str, status: str, description: str) -> bool:
        """Send error status alert."""
        message = (
            f"⚠️ <b>{robot_name} Issue</b>\n"
            f"Status: <code>{status}</code>\n"
            f"{description}"
        )
        return await self.send_message(message)
    
    async def send_resolved(self, robot_name: str, status: str) -> bool:
        """Send issue resolved notification."""
        message = f"✅ <b>{robot_name}</b> — Issue resolved! Now {status}"
        return await self.send_message(message)
