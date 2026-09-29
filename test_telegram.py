#!/usr/bin/env python3
"""Test Telegram bot connectivity."""
import asyncio
import os
from notifier import TelegramNotifier

async def main():
    notifier = TelegramNotifier(
        os.environ["TELEGRAM_BOT_TOKEN"],
        os.environ["TELEGRAM_CHAT_ID"]
    )
    
    print("Sending DRAWER_FULL alert to group...")
    success = await notifier.send_waste_alert("COPTiP", 100)
    
    if success:
        print("✅ Drawer full alert sent to group!")
    else:
        print("❌ Failed to send message")

if __name__ == "__main__":
    asyncio.run(main())
