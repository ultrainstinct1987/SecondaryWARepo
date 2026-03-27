"""
Telegram Group Contribution Tracker Bot

Stores all data inside Telegram itself — no files, no database, no 3rd party.
Data is kept as a pinned message in a private storage channel.

Setup:
  1. Create a NEW private Telegram channel (e.g. "Bot Storage")
  2. Add your bot to that channel and make it admin (allow: post, edit, pin)
  3. Forward any message from that channel to @userinfobot to get the channel ID
  4. Fill in BOT_TOKEN, ADMIN_IDS, and STORAGE_CHANNEL_ID below
  5. python bot.py

Commands:
  /mystatus     — your own contribution count
  /period       — current period dates and deadline
  /status       — (admin) everyone's contribution status
  /scan         — (admin) register all group admins
  /addmember    — (admin) reply to a message to register that member
  /setstart DD/MM/YYYY  — (admin) start a new period
"""

import os
import json
import logging
from datetime import datetime
from dateutil.relativedelta import relativedelta
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ChatMemberHandler, CallbackQueryHandler, filters, ContextTypes
)

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN          = os.getenv('BOT_TOKEN',          '8745432625:AAEcTZSsGqvfmOlUGx5463qtamomRnVnoHk')
ADMIN_IDS          = [int(x) for x in os.getenv('ADMIN_IDS', '411713323').split(',')]
STORAGE_CHANNEL_ID = int(os.getenv('STORAGE_CHANNEL_ID', '411713323'))
REQUIRED_PDFS      = 2
PERIOD_MONTHS      = 3
# ─────────────────────────────────────────────────────────────────────────────


# ── Telegram as storage ───────────────────────────────────────────────────────
def _default_data() -> dict:
    return {
        'period_start':  datetime.now().strftime('%Y-%m-%d'),
        'members':       {},   # { "user_id": {"name": "...", "username": "..."} }
        'contributions': {},   # { "user_id": ["file1.pdf", "file2.pdf"] }
        'pending':       {}    # { "orig_msg_id": {"user_id": "...", "filename": "..."} }
    }


async def tg_load(bot) -> dict:
    try:
        chat = await bot.get_chat(STORAGE_CHANNEL_ID)
        if chat.pinned_message and chat.pinned_message.text:
            data = json.loads(chat.pinned_message.text)
            data.setdefault('pending', {})   # handle old data without pending key
            return data
    except Exception as e:
        logging.warning(f"Load failed: {e}")
    return _default_data()


async def tg_save(bot, data: dict):
    text = json.dumps(data, ensure_ascii=False)
    try:
        chat = await bot.get_chat(STORAGE_CHANNEL_ID)
        if chat.pinned_message:
            await bot.edit_message_text(
                chat_id=STORAGE_CHANNEL_ID,
                message_id=chat.pinned_message.message_id,
                text=text
            )
        else:
            msg = await bot.send_message(STORAGE_CHANNEL_ID, text)
            await bot.pin_chat_message(STORAGE_CHANNEL_ID, msg.message_id,
                                       disable_notification=True)
    except Exception as e:
        logging.error(f"Save failed: {e}")


async def load_and_check(bot) -> dict:
    """Load data and auto-reset contributions if the period has expired."""
    data = await tg_load(bot)
    start = datetime.strptime(data['period_start'], '%Y-%m-%d')
    end   = start + relativedelta(months=PERIOD_MONTHS)
    if datetime.now() >= end:
        data['period_start']  = end.strftime('%Y-%m-%d')
        data['contributions'] = {}
        data['pending']       = {}
        await tg_save(bot, data)
        logging.info(f"Period auto-reset. New start: {data['period_start']}")
    return data


# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt(dt: datetime) -> str:
    return dt.strftime('%d %b %Y')


def period_dates(data: dict) -> tuple[datetime, datetime]:
    start = datetime.strptime(data['period_start'], '%Y-%m-%d')
    return start, start + relativedelta(months=PERIOD_MONTHS)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ── Handlers ──────────────────────────────────────────────────────────────────
async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silently register any user who sends a message in the group."""
    msg  = update.message
    user = msg.from_user if msg else None
    if not user or user.is_bot:
        return
    data = await load_and_check(context.bot)
    uid  = str(user.id)
    if uid not in data['members']:
        data['members'][uid] = {'name': user.full_name, 'username': user.username or ''}
        await tg_save(context.bot, data)
        logging.info(f"Auto-registered: {user.full_name} ({user.id})")


async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return
    u = result.new_chat_member.user
    if u.is_bot or result.new_chat_member.status in ('left', 'kicked', 'banned'):
        return
    data = await load_and_check(context.bot)
    data['members'][str(u.id)] = {'name': u.full_name, 'username': u.username or ''}
    await tg_save(context.bot, data)


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

    data = await load_and_check(context.bot)

    uid      = str(user.id)
    filename = doc.file_name or f'file_{msg.message_id}.pdf'

    # Register member if not already tracked
    data['members'][uid] = {'name': user.full_name, 'username': user.username or ''}

    # Store as pending — awaiting admin approval
    data['pending'][str(msg.message_id)] = {
        'user_id':  uid,
        'filename': filename,
    }
    await tg_save(context.bot, data)

    # Send approval request with Approve / Reject buttons
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{uid}:{msg.message_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{uid}:{msg.message_id}"),
    ]])
    await msg.reply_text(
        f"📄 *{user.full_name}* uploaded `{filename}`\n"
        f"Waiting for admin approval.",
        parse_mode='Markdown',
        reply_markup=keyboard
    )


async def on_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Approve / Reject button presses."""
    query = update.callback_query

    # Only admins can approve/reject
    if not is_admin(query.from_user.id):
        await query.answer("Only admins can approve or reject.", show_alert=True)
        return

    await query.answer()

    parts   = query.data.split(':')
    action  = parts[0]           # 'approve' or 'reject'
    uid     = parts[1]
    msg_id  = parts[2]

    data = await load_and_check(context.bot)
    pending = data.get('pending', {})

    if msg_id not in pending:
        await query.edit_message_text("⚠️ This submission was already processed.")
        return

    entry    = pending.pop(msg_id)
    filename = entry['filename']
    member   = data['members'].get(uid, {})
    name     = member.get('name', 'Member')
    admin_name = query.from_user.first_name

    if action == 'approve':
        data['contributions'].setdefault(uid, []).append(filename)
        count = len(data['contributions'][uid])
        _, end = period_dates(data)

        progress = f"{count}/{REQUIRED_PDFS} PDFs this period"
        extra    = " — Requirement met! 🎉" if count >= REQUIRED_PDFS else f" — {REQUIRED_PDFS - count} more needed"

        await query.edit_message_text(
            f"✅ *Approved* by {admin_name}\n"
            f"👤 {name}: `{filename}`\n"
            f"{progress}{extra}",
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text(
            f"❌ *Rejected* by {admin_name}\n"
            f"👤 {name}: `{filename}`\n"
            f"This PDF was not counted.",
            parse_mode='Markdown'
        )

    data['pending'] = pending
    await tg_save(context.bot, data)


async def cmd_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await load_and_check(context.bot)
    start, end = period_dates(data)
    days_left  = max(0, (end - datetime.now()).days)
    await update.message.reply_text(
        f"📅 *Current Period*\n"
        f"Start    : {fmt(start)}\n"
        f"End      : {fmt(end)}\n"
        f"Days left: {days_left}\n\n"
        f"Requirement: {REQUIRED_PDFS} PDFs per member",
        parse_mode='Markdown'
    )


async def cmd_mystatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user  = update.effective_user
    data  = await load_and_check(context.bot)
    start, end = period_dates(data)

    uid   = str(user.id)
    files = data['contributions'].get(uid, [])
    count = len(files)
    icon  = "✅" if count >= REQUIRED_PDFS else "❌"

    # Check if they have pending submissions
    pending_count = sum(
        1 for p in data['pending'].values() if p['user_id'] == uid
    )

    text = (
        f"{icon} *Your Status*\n"
        f"({fmt(start)} – {fmt(end)})\n\n"
        f"Approved: {count}/{REQUIRED_PDFS} PDFs\n"
    )
    if pending_count:
        text += f"Pending approval: {pending_count} PDF(s)\n"
    if files:
        text += "\nApproved files:\n" + "\n".join(f"  • {f}" for f in files)
    if count < REQUIRED_PDFS:
        text += f"\n\n{REQUIRED_PDFS - count} more approved PDF(s) needed before {fmt(end)}."

    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    data = await load_and_check(context.bot)
    start, end = period_dates(data)
    members = data['members']

    if not members:
        await update.message.reply_text("No members tracked yet.")
        return

    done, waiting = [], []
    for uid, info in members.items():
        count   = len(data['contributions'].get(uid, []))
        pending = sum(1 for p in data['pending'].values() if p['user_id'] == uid)
        label   = info['name'] + (f" (@{info['username']})" if info['username'] else '')
        if count >= REQUIRED_PDFS:
            done.append(f"  ✅ {label}")
        else:
            note = f" (+{pending} pending)" if pending else ""
            waiting.append(f"  ❌ {label} ({count}/{REQUIRED_PDFS}{note})")

    total = len(members)
    text  = (
        f"📊 *Contribution Status*\n"
        f"({fmt(start)} – {fmt(end)})\n\n"
        f"✅ Done ({len(done)}/{total}):\n"
        + ("\n".join(done) or "  (none yet)") + "\n\n"
        f"❌ Pending ({len(waiting)}/{total}):\n"
        + ("\n".join(waiting) or "  (all done!)")
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_setstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /setstart DD/MM/YYYY\nExample: /setstart 01/04/2025")
        return

    try:
        dt = datetime.strptime(context.args[0], '%d/%m/%Y')
    except ValueError:
        await update.message.reply_text("Invalid date. Use DD/MM/YYYY, e.g. 01/04/2025")
        return

    data = await tg_load(context.bot)
    data['period_start']  = dt.strftime('%Y-%m-%d')
    data['contributions'] = {}
    data['pending']       = {}
    await tg_save(context.bot, data)

    _, end = period_dates(data)
    await update.message.reply_text(
        f"New period started: {fmt(dt)} → {fmt(end)}\n"
        f"Contributions and pending approvals have been reset."
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    chat   = update.effective_chat
    admins = await chat.get_administrators()
    data   = await load_and_check(context.bot)

    added = []
    for member in admins:
        u = member.user
        if u.is_bot:
            continue
        uid = str(u.id)
        if uid not in data['members']:
            data['members'][uid] = {'name': u.full_name, 'username': u.username or ''}
            added.append(u.full_name)

    await tg_save(context.bot, data)

    if added:
        await update.message.reply_text(
            f"Scanned admins. Added {len(added)} new member(s):\n" +
            "\n".join(f"  • {n}" for n in added)
        )
    else:
        await update.message.reply_text(
            "Scanned admins — no new members to add.\n\n"
            "For regular members, ask them to send any message and they'll be registered automatically."
        )


async def cmd_addmember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    if update.message.reply_to_message:
        u = update.message.reply_to_message.from_user
        if u and not u.is_bot:
            data = await load_and_check(context.bot)
            data['members'][str(u.id)] = {'name': u.full_name, 'username': u.username or ''}
            await tg_save(context.bot, data)
            await update.message.reply_text(f"Added: {u.full_name}")
            return

    await update.message.reply_text(
        "Reply to any message from the member you want to add, then send /addmember."
    )


# ── Register commands ─────────────────────────────────────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand('mystatus',   'Check your own contribution status'),
        BotCommand('period',     'Show current period dates and deadline'),
        BotCommand('status',     '(Admin) Show all members contribution status'),
        BotCommand('scan',       '(Admin) Register all group admins'),
        BotCommand('addmember',  '(Admin) Reply to a message to register that member'),
        BotCommand('setstart',   '(Admin) Start a new period — /setstart DD/MM/YYYY'),
    ])


# ── Error handler ─────────────────────────────────────────────────────────────
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Update {update} caused error: {context.error}", exc_info=context.error)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        print("ERROR: Set BOT_TOKEN.")
        return
    if not STORAGE_CHANNEL_ID:
        print("ERROR: Set STORAGE_CHANNEL_ID.")
        return

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(ChatMemberHandler(on_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS, on_any_message))
    app.add_handler(CallbackQueryHandler(on_approval, pattern=r'^(approve|reject):'))
    app.add_handler(CommandHandler('period',    cmd_period))
    app.add_handler(CommandHandler('mystatus',  cmd_mystatus))
    app.add_handler(CommandHandler('status',    cmd_status))
    app.add_handler(CommandHandler('setstart',  cmd_setstart))
    app.add_handler(CommandHandler('scan',      cmd_scan))
    app.add_handler(CommandHandler('addmember', cmd_addmember))
    app.add_error_handler(on_error)

    print("Bot running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
