#!/usr/bin/env python3
"""Test Telegram commands."""
import asyncio
import os
import aiohttp

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

async def send_command(command: str):
    """Send a command to the bot (simulates user typing it)."""
    async with aiohttp.ClientSession() as session:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": command
        }
        async with session.post(url, json=payload) as resp:
            return resp.status == 200

async def main():
    print("Testing Telegram bot commands...")
    
    # Send commands
    commands = ["/help", "/status", "/level", "/issues"]
    for cmd in commands:
        print(f"Sending {cmd}...")
        await send_command(cmd)
        await asyncio.sleep(1)
    
    print("Commands sent! Check your group chat for responses.")

if __name__ == "__main__":
    asyncio.run(main())
