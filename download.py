"""
Telegram Group File Downloader
Downloads all files from your group and organises them by level/subject.

Folders created:
    downloads/Sec 1/
    downloads/Sec 2/
    downloads/Sec 3 E-Maths/
    downloads/Sec 3 Add Maths/
    downloads/Sec 4 E-Maths/
    downloads/Sec 4 Add Maths/
    downloads/Uncategorised/

Setup:
    1. pip install telethon
    2. Get api_id and api_hash from https://my.telegram.org
       (Log in → API Development Tools → Create app)
    3. Fill in API_ID, API_HASH, and GROUP below
    4. python download.py
       (First run: enter your phone number + Telegram OTP to log in)
"""

import asyncio
import re
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.types import MessageMediaDocument

# ── Config ────────────────────────────────────────────────────────────────────
API_ID   = 39978206       # integer, e.g. 12345678
API_HASH = '5974a0eaf7d6464a7ebc72c567f1a802'      # string,  e.g. 'abcdef1234567890abcdef1234567890'

# Your group: https://t.me/+9MaJvz5A9TNlNTBl
# To find the numeric ID: forward any group message to @userinfobot
GROUP = 'https://t.me/+9MaJvz5A9TNlNTBl'

OUTPUT = Path('downloads')
# ─────────────────────────────────────────────────────────────────────────────


def classify(filename: str, caption: str) -> str:
    """Return the folder name for a file based on its name + caption."""
    text = (filename + ' ' + caption).lower()

    # Detect level
    level = None
    if   re.search(r'sec\s*1|secondary\s*1|\bs1\b', text): level = 1
    elif re.search(r'sec\s*2|secondary\s*2|\bs2\b', text): level = 2
    elif re.search(r'sec\s*3|secondary\s*3|\bs3\b', text): level = 3
    elif re.search(r'sec\s*4|secondary\s*4|\bs4\b', text): level = 4

    if level is None:
        return 'Uncategorised'

    # Sec 1 and Sec 2 — no subject split needed
    if level in (1, 2):
        return f'Sec {level}'

    # Sec 3 / Sec 4 — detect subject
    is_add = bool(re.search(
        r'a\s*math|add\s*math|additional\s*math|amath|amaths|a-math', text))
    is_e   = bool(re.search(
        r'e\s*math|elem\w*\s*math|emath|emaths|e-math', text))

    if is_add and not is_e:
        return f'Sec {level} Add Maths'
    if is_e and not is_add:
        return f'Sec {level} E-Maths'

    # Both or neither — can't tell
    return 'Uncategorised'


async def main():
    if not API_ID or not API_HASH or not GROUP:
        print("ERROR: Fill in API_ID, API_HASH, and GROUP in download.py first.")
        return

    async with TelegramClient('session', API_ID, API_HASH) as client:
        entity = await client.get_entity(GROUP)
        name   = getattr(entity, 'title', str(GROUP))
        print(f"\nGroup : {name}")
        print(f"Output: {OUTPUT.resolve()}\n")

        ok = skip = err = 0

        async for msg in client.iter_messages(entity, reverse=True):
            # Only process messages that have a document attached
            if not msg.media or not isinstance(msg.media, MessageMediaDocument):
                continue

            doc = msg.media.document

            # Get original filename from document attributes
            filename = None
            for attr in doc.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    filename = attr.file_name
                    break
            if not filename:
                # Fallback name using message ID and mime type
                ext = doc.mime_type.split('/')[-1] if doc.mime_type else 'bin'
                filename = f'file_{msg.id}.{ext}'

            caption = msg.message or ''
            folder  = classify(filename, caption)

            dest_dir = OUTPUT / folder
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / filename

            if dest.exists():
                skip += 1
                continue

            size_kb = doc.size // 1024
            print(f"  [{folder}] {filename} ({size_kb} KB)", end=' ... ', flush=True)
            try:
                await client.download_media(msg, dest)
                print("OK")
                ok += 1
            except Exception as e:
                print(f"ERR: {e}")
                err += 1

        print(f"\n{'='*50}")
        print(f"  Done!  Downloaded={ok}  Skipped={skip}  Errors={err}")
        print(f"  Files in: {OUTPUT.resolve()}")
        print(f"{'='*50}")


if __name__ == '__main__':
    asyncio.run(main())
