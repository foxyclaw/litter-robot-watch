#!/usr/bin/env python3
"""Get chat ID from Telegram bot updates."""
import asyncio
import os
import aiohttp

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

async def main():
    async with aiohttp.ClientSession() as session:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        
        async with session.get(url) as response:
            data = await response.json()
            
            if not data.get("ok"):
                print(f"Error: {data}")
                return
            
            updates = data.get("result", [])
            
            if not updates:
                print("No updates found. Send a message to your bot or group first!")
                print("\nSteps:")
                print("1. Add @COPTiP_Monitor_Bot to your group")
                print("2. Send a message in the group (or /start)")
                print("3. Run this script again")
                return
            
            print("Found chats:\n")
            seen_chats = set()
            
            for update in updates:
                # Check for message or edited_message
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                
                chat = message.get("chat", {})
                chat_id = chat.get("id")
                chat_type = chat.get("type")
                chat_title = chat.get("title", "Private Chat")
                
                if chat_id in seen_chats:
                    continue
                seen_chats.add(chat_id)
                
                if chat_type == "private":
                    username = chat.get("username", "N/A")
                    print(f"👤 Private chat: {username}")
                    print(f"   Chat ID: {chat_id}")
                elif chat_type in ["group", "supergroup"]:
                    print(f"👥 Group: {chat_title}")
                    print(f"   Chat ID: {chat_id}")
                print()
            
            print("\nCopy the Group Chat ID to your config.yaml")
            print("(Group IDs usually start with -100 for supergroups)")

if __name__ == "__main__":
    asyncio.run(main())
