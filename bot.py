"""
Telegram Group Contribution Tracker Bot

Tracks PDF contributions per 3-month period using a simple JSON file.
Automatically resets when the period ends — no database needed.

Commands:
  /status       — (admin) show all members' contribution status
  /mystatus     — show your own contribution count
  /period       — show current period dates and deadline
  /setstart DD/MM/YYYY  — (admin) manually start a new period
"""

import os
import json
import logging
from datetime import datetime
from dateutil.relativedelta import relativedelta
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ChatMemberHandler, filters, ContextTypes
)

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv('BOT_TOKEN', '8745432625:AAEcTZSsGqvfmOlUGx5463qtamomRnVnoHk')
ADMIN_IDS     = [int(x) for x in os.getenv('ADMIN_IDS', '411713323').split(',')]
REQUIRED_PDFS = 2
PERIOD_MONTHS = 3
DATA_FILE     = 'data.json'
# ─────────────────────────────────────────────────────────────────────────────


# ── JSON storage (replaces database) ─────────────────────────────────────────
# data.json structure:
# {
#   "period_start": "2025-01-01",
#   "members":       { "user_id": {"name": "...", "username": "..."} },
#   "contributions": { "user_id": ["file1.pdf", "file2.pdf"] }
# }

def load() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    # First run — start period from today
    return {
        'period_start':  datetime.now().strftime('%Y-%m-%d'),
        'members':       {},
        'contributions': {}
    }


def save(data: dict):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def check_and_reset(data: dict) -> dict:
    """If the 3-month period has expired, reset contributions and start a new period."""
    start = datetime.strptime(data['period_start'], '%Y-%m-%d')
    end   = start + relativedelta(months=PERIOD_MONTHS)
    if datetime.now() >= end:
        data['period_start']  = end.strftime('%Y-%m-%d')
        data['contributions'] = {}
        save(data)
        logging.info(f"Period reset. New period started: {data['period_start']}")
    return data


def period_dates(data: dict) -> tuple[datetime, datetime]:
    start = datetime.strptime(data['period_start'], '%Y-%m-%d')
    end   = start + relativedelta(months=PERIOD_MONTHS)
    return start, end


# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt(dt: datetime) -> str:
    return dt.strftime('%d %b %Y')


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ── Handlers ──────────────────────────────────────────────────────────────────
async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return
    u = result.new_chat_member.user
    if u.is_bot or result.new_chat_member.status in ('left', 'kicked', 'banned'):
        return

    data = check_and_reset(load())
    data['members'][str(u.id)] = {'name': u.full_name, 'username': u.username or ''}
    save(data)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.document:
        return

    doc = msg.document
    if doc.mime_type != 'application/pdf' and not (doc.file_name or '').lower().endswith('.pdf'):
        return

    user = msg.from_user
    if not user or user.is_bot:
        return

    data = check_and_reset(load())

    # Register member automatically on first upload
    uid = str(user.id)
    data['members'].setdefault(uid, {'name': user.full_name, 'username': user.username or ''})
    data['members'][uid] = {'name': user.full_name, 'username': user.username or ''}

    # Record contribution
    filename = doc.file_name or f'file_{msg.message_id}.pdf'
    data['contributions'].setdefault(uid, []).append(filename)
    count = len(data['contributions'][uid])
    save(data)

    _, end = period_dates(data)

    if count == REQUIRED_PDFS:
        await msg.reply_text(
            f"Thanks {user.first_name}! You've uploaded {count}/{REQUIRED_PDFS} PDFs. Requirement met! ✅"
        )
    elif count < REQUIRED_PDFS:
        await msg.reply_text(
            f"Got it {user.first_name}! {count}/{REQUIRED_PDFS} PDFs uploaded. "
            f"{REQUIRED_PDFS - count} more needed before {fmt(end)}."
        )


async def cmd_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = check_and_reset(load())
    start, end = period_dates(data)
    days_left = max(0, (end - datetime.now()).days)
    await update.message.reply_text(
        f"📅 *Current Period*\n"
        f"Start    : {fmt(start)}\n"
        f"End      : {fmt(end)}\n"
        f"Days left: {days_left}\n\n"
        f"Requirement: {REQUIRED_PDFS} PDFs per member",
        parse_mode='Markdown'
    )


async def cmd_mystatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = check_and_reset(load())
    start, end = period_dates(data)

    uid   = str(user.id)
    files = data['contributions'].get(uid, [])
    count = len(files)
    icon  = "✅" if count >= REQUIRED_PDFS else "❌"

    text = (
        f"{icon} *Your Status*\n"
        f"({fmt(start)} – {fmt(end)})\n\n"
        f"Uploaded: {count}/{REQUIRED_PDFS} PDFs\n"
    )
    if files:
        text += "\nFiles:\n" + "\n".join(f"  • {f}" for f in files)
    if count < REQUIRED_PDFS:
        text += f"\n\n{REQUIRED_PDFS - count} more PDF(s) needed before {fmt(end)}."

    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    data = check_and_reset(load())
    start, end = period_dates(data)
    members = data['members']

    if not members:
        await update.message.reply_text("No members tracked yet.")
        return

    done, pending = [], []
    for uid, info in members.items():
        count = len(data['contributions'].get(uid, []))
        label = info['name'] + (f" (@{info['username']})" if info['username'] else '')
        if count >= REQUIRED_PDFS:
            done.append(f"  ✅ {label}")
        else:
            done_count = count
            pending.append(f"  ❌ {label} ({done_count}/{REQUIRED_PDFS})")

    total = len(members)
    text = (
        f"📊 *Contribution Status*\n"
        f"({fmt(start)} – {fmt(end)})\n\n"
        f"✅ Done ({len(done)}/{total}):\n"
        + ("\n".join(done) or "  (none yet)") + "\n\n"
        f"❌ Pending ({len(pending)}/{total}):\n"
        + ("\n".join(pending) or "  (all done!)")
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_setstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /setstart DD/MM/YYYY\nExample: /setstart 01/01/2025")
        return

    try:
        dt = datetime.strptime(context.args[0], '%d/%m/%Y')
    except ValueError:
        await update.message.reply_text("Invalid date. Use DD/MM/YYYY, e.g. 01/01/2025")
        return

    data = load()
    data['period_start']  = dt.strftime('%Y-%m-%d')
    data['contributions'] = {}   # reset contributions for the new period
    save(data)

    _, end = period_dates(data)
    await update.message.reply_text(
        f"New period started: {fmt(dt)} → {fmt(end)}\n"
        f"Contributions have been reset."
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        print("ERROR: Set BOT_TOKEN first.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(ChatMemberHandler(on_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CommandHandler('period',   cmd_period))
    app.add_handler(CommandHandler('mystatus', cmd_mystatus))
    app.add_handler(CommandHandler('status',   cmd_status))
    app.add_handler(CommandHandler('setstart', cmd_setstart))

    print("Bot running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
