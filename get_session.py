"""
Run this script ONCE locally to generate your Telethon session string.
Copy the printed string and set it as the TELETHON_SESSION environment variable on Railway.

Usage:
    python get_session.py
"""

from telethon import TelegramClient
from telethon.sessions import StringSession
import asyncio

API_ID   = 39978206
API_HASH = '5974a0eaf7d6464a7ebc72c567f1a802'


async def main():
    async with TelegramClient(StringSession(), API_ID, API_HASH) as client:
        session_string = client.session.save()
        print("\n" + "=" * 60)
        print("Your TELETHON_SESSION string (copy everything between the lines):")
        print("=" * 60)
        print(session_string)
        print("=" * 60)
        print("\nSet this as the TELETHON_SESSION environment variable on Railway.")


asyncio.run(main())
