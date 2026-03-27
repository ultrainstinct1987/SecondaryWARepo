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

import io
import os
import re
import json
import logging
from datetime import datetime
from dateutil.relativedelta import relativedelta
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ChatMemberHandler, CallbackQueryHandler, filters, ContextTypes
)

# ── Paper info collection ─────────────────────────────────────────────────────
EXAM_TYPES = ['WA1', 'WA2', 'WA3', 'EOY', 'Practice']
YEARS      = ['2023', '2024', '2025', '2026']

def build_filename(info: dict) -> str:
    """Build standardised filename from collected paper info."""
    level   = info['level'].replace(' ', '')           # Sec3
    subject = info['subject'].replace('-','').replace(' ','')  # EMaths / AMaths
    year    = info['year']
    etype   = info['exam_type']
    school  = re.sub(r'\s+', '', info['school'].lower())  # anglicanhigh
    return f"{level}_{subject}_{year}_{etype}_{school}.pdf"

def summary_text(info: dict) -> str:
    return (
        f"Level    : {info.get('level','—')}\n"
        f"Subject  : {info.get('subject','—')}\n"
        f"Exam type: {info.get('exam_type','—')}\n"
        f"Year     : {info.get('year','—')}\n"
        f"School   : {info.get('school','—')}"
    )

def kb_level():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('Sec 1', callback_data='pi:level:1'),
        InlineKeyboardButton('Sec 2', callback_data='pi:level:2'),
        InlineKeyboardButton('Sec 3', callback_data='pi:level:3'),
        InlineKeyboardButton('Sec 4', callback_data='pi:level:4'),
    ]])

def kb_subject():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('E-Maths',  callback_data='pi:subject:E-Maths'),
        InlineKeyboardButton('Add Maths', callback_data='pi:subject:Add Maths'),
    ]])

def kb_examtype():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(e, callback_data=f'pi:examtype:{e}') for e in EXAM_TYPES[:3]],
        [InlineKeyboardButton(e, callback_data=f'pi:examtype:{e}') for e in EXAM_TYPES[3:]],
    ])

def kb_year():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(y, callback_data=f'pi:year:{y}') for y in YEARS
    ]])

def kb_confirm():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ Confirm', callback_data='pi:confirm:yes'),
        InlineKeyboardButton('❌ Cancel',  callback_data='pi:confirm:no'),
    ]])

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN          = os.getenv('BOT_TOKEN',          '8745432625:AAEcTZSsGqvfmOlUGx5463qtamomRnVnoHk')
ADMIN_IDS          = [int(x) for x in os.getenv('ADMIN_IDS', '411713323').split(',')]
STORAGE_CHANNEL_ID = int(os.getenv('STORAGE_CHANNEL_ID', '-1003662706262'))  # set this to your WAStorage channel ID
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
            data.setdefault('pending', {})
            return data
    except Exception as e:
        logging.warning(f"Load failed: {e}")
    return _default_data()


async def tg_save(bot, data: dict) -> str | None:
    """Save data. Returns None on success, or an error string on failure."""
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
        return None
    except Exception as e:
        logging.error(f"Save failed: {e}")
        return str(e)


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
    """Silently register members. Also captures school name during paper info collection."""
    msg  = update.message
    user = msg.from_user if msg else None
    if not user or user.is_bot:
        return

    # Capture school name if this user is in the middle of filling in paper info
    col = context.user_data.get('collecting')
    if col and col.get('step') == 'school' and col['uid'] == str(user.id):
        col['school'] = msg.text.strip()
        filename = build_filename(col)
        col['step'] = 'confirm'
        await msg.reply_text(
            f"Rename to:\n<code>{filename}</code>\n\n"
            f"<i>{summary_text(col)}</i>\n\nConfirm?",
            parse_mode='HTML',
            reply_markup=kb_confirm()
        )
        return

    # Auto-register on first message
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

    # Register member
    data = await load_and_check(context.bot)
    uid  = str(user.id)
    data['members'][uid] = {'name': user.full_name, 'username': user.username or ''}
    await tg_save(context.bot, data)

    # Start paper info collection — store state in user_data
    context.user_data['collecting'] = {
        'file_id':      doc.file_id,
        'chat_id':      msg.chat_id,
        'orig_msg_id':  msg.message_id,
        'uid':          uid,
        'user_name':    user.full_name,
        'step':         'level',
    }

    prompt = await msg.reply_text(
        f"📄 <b>{user.first_name}</b> uploaded a PDF.\n\nStep 1/5 — Select level:",
        parse_mode='HTML',
        reply_markup=kb_level()
    )
    context.user_data['collecting']['prompt_msg_id'] = prompt.message_id


async def on_paper_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the step-by-step paper info buttons."""
    query = update.callback_query
    col   = context.user_data.get('collecting')

    # Only the uploader can fill in the form
    if not col or col['uid'] != str(query.from_user.id):
        await query.answer("Only the person who uploaded can fill this in.", show_alert=True)
        return

    await query.answer()

    _, field, value = query.data.split(':', 2)

    if field == 'level':
        col['level'] = f'Sec {value}'
        col['step']  = 'subject'
        await query.edit_message_text(
            f"Step 2/5 — Select subject:\n<i>Level: Sec {value} ✅</i>",
            parse_mode='HTML',
            reply_markup=kb_subject()
        )

    elif field == 'subject':
        col['subject'] = value
        col['step']    = 'examtype'
        await query.edit_message_text(
            f"Step 3/5 — Select exam type:\n<i>{summary_text(col)}</i>",
            parse_mode='HTML',
            reply_markup=kb_examtype()
        )

    elif field == 'examtype':
        col['exam_type'] = value
        col['step']      = 'year'
        await query.edit_message_text(
            f"Step 4/5 — Select year:\n<i>{summary_text(col)}</i>",
            parse_mode='HTML',
            reply_markup=kb_year()
        )

    elif field == 'year':
        col['year'] = value
        col['step'] = 'school'
        await query.edit_message_text(
            f"Step 5/5 — Type the <b>school name</b> and send it:\n<i>{summary_text(col)}</i>",
            parse_mode='HTML'
        )

    elif field == 'confirm':
        if value == 'no':
            context.user_data.pop('collecting', None)
            await query.edit_message_text("❌ Cancelled. The original PDF was not counted.")
            return

        # Download, rename, re-upload, then send for approval
        await query.edit_message_text("⏳ Processing...")
        col = context.user_data.pop('collecting')

        filename = build_filename(col)

        try:
            tg_file = await context.bot.get_file(col['file_id'])
            bio = io.BytesIO()
            await tg_file.download_to_memory(out=bio)
            bio.seek(0)

            sent = await context.bot.send_document(
                chat_id=col['chat_id'],
                document=bio,
                filename=filename,
                caption=f"📄 {filename}"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to process file: {e}")
            return

        # Only delete original AFTER confirming new file was sent
        try:
            await context.bot.delete_message(col['chat_id'], col['orig_msg_id'])
        except Exception:
            pass

        await query.edit_message_text(f"✅ Renamed to <code>{filename}</code>", parse_mode='HTML')

        # Store as pending approval
        data = await load_and_check(context.bot)
        data['pending'][str(sent.message_id)] = {
            'user_id':  col['uid'],
            'filename': filename,
        }
        await tg_save(context.bot, data)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{col['uid']}:{sent.message_id}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{col['uid']}:{sent.message_id}"),
        ]])
        await context.bot.send_message(
            col['chat_id'],
            f"📄 <b>{col['user_name']}</b> — <code>{filename}</code>\nWaiting for admin approval.",
            parse_mode='HTML',
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
            f"✅ <b>Approved</b> by {admin_name}\n"
            f"👤 {name}: <code>{filename}</code>\n"
            f"{progress}{extra}",
            parse_mode='HTML'
        )
    else:
        await query.edit_message_text(
            f"❌ <b>Rejected</b> by {admin_name}\n"
            f"👤 {name}: <code>{filename}</code>\n"
            f"This PDF was not counted.",
            parse_mode='HTML'
        )

    data['pending'] = pending
    await tg_save(context.bot, data)


async def cmd_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await load_and_check(context.bot)
    start, end = period_dates(data)
    days_left  = max(0, (end - datetime.now()).days)
    await update.message.reply_text(
        f"📅 <b>Current Period</b>\n"
        f"Start    : {fmt(start)}\n"
        f"End      : {fmt(end)}\n"
        f"Days left: {days_left}\n\n"
        f"Requirement: {REQUIRED_PDFS} PDFs per member",
        parse_mode='HTML'
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
        f"{icon} <b>Your Status</b>\n"
        f"({fmt(start)} – {fmt(end)})\n\n"
        f"Approved: {count}/{REQUIRED_PDFS} PDFs\n"
    )
    if pending_count:
        text += f"Pending approval: {pending_count} PDF(s)\n"
    if files:
        text += "\nApproved files:\n" + "\n".join(f"  • <code>{f}</code>" for f in files)
    if count < REQUIRED_PDFS:
        text += f"\n\n{REQUIRED_PDFS - count} more approved PDF(s) needed before {fmt(end)}."

    await update.message.reply_text(text, parse_mode='HTML')


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
        f"📊 <b>Contribution Status</b>\n"
        f"({fmt(start)} – {fmt(end)})\n\n"
        f"✅ Done ({len(done)}/{total}):\n"
        + ("\n".join(done) or "  (none yet)") + "\n\n"
        f"❌ Pending ({len(waiting)}/{total}):\n"
        + ("\n".join(waiting) or "  (all done!)")
    )
    await update.message.reply_text(text, parse_mode='HTML')


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


async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all registered members."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    data = await load_and_check(context.bot)
    members = data['members']

    if not members:
        await update.message.reply_text(
            "No members registered yet.\n\n"
            "Run /debug to check if storage is working, then use /scan to add admins."
        )
        return

    lines = [f"👥 <b>Registered Members ({len(members)})</b>\n"]
    for i, (uid, info) in enumerate(members.items(), 1):
        name  = info.get('name', 'Unknown')
        uname = f" @{info['username']}" if info.get('username') else ''
        lines.append(f"{i}. {name}{uname}")

    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Admin) Show storage status and data summary for diagnosing issues."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    lines = [f"🔧 <b>Debug Info</b>\n"]
    lines.append(f"Storage channel ID: `{STORAGE_CHANNEL_ID}`")

    # Try to reach the storage channel
    try:
        chat = await context.bot.get_chat(STORAGE_CHANNEL_ID)
        lines.append(f"Channel reachable: ✅ ({chat.title or chat.type})")

        if chat.pinned_message:
            raw = chat.pinned_message.text or ''
            lines.append(f"Pinned message: ✅ ({len(raw)} chars)")
            try:
                data = json.loads(raw)
                lines.append(f"Data valid JSON: ✅")
                lines.append(f"Members stored: {len(data.get('members', {}))}")
                lines.append(f"Contributions: {len(data.get('contributions', {}))}")
                lines.append(f"Pending: {len(data.get('pending', {}))}")
                lines.append(f"Period start: {data.get('period_start', '?')}")
            except Exception as e:
                lines.append(f"Data parse error: ❌ {e}")
        else:
            lines.append("Pinned message: ❌ (none — bot hasn't saved data yet)")
            lines.append("Fix: make sure bot is admin in the storage channel and run /scan")

    except Exception as e:
        lines.append(f"Channel reachable: ❌\nError: {e}")
        lines.append(
            "\nFix: Create a private channel, add the bot as admin, "
            "get its ID (forward a message to @userinfobot), "
            "then set STORAGE_CHANNEL_ID to that value."
        )

    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def cmd_addmember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    replied = update.message.reply_to_message

    if not replied:
        await update.message.reply_text(
            "How to use:\n"
            "1. Long-press any message from the member\n"
            "2. Tap Reply\n"
            "3. Type /addmember manually (do not pick from the / menu)\n"
            "4. Send"
        )
        return

    u = replied.from_user
    if not u or u.is_bot:
        await update.message.reply_text("That message is from a bot — cannot add.")
        return

    data = await load_and_check(context.bot)
    uid     = str(u.id)
    already = uid in data['members']
    data['members'][uid] = {'name': u.full_name, 'username': u.username or ''}
    err = await tg_save(context.bot, data)

    if err:
        await update.message.reply_text(
            f"Storage error — could not save.\n\n"
            f"Error: {err}\n\n"
            f"Run /debug to check the storage channel setup."
        )
    elif already:
        await update.message.reply_text(f"Updated: {u.full_name} (already registered)")
    else:
        await update.message.reply_text(f"Added: {u.full_name}")


# ── Register commands ─────────────────────────────────────────────────────────
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand('mystatus',   'Check your own contribution status'),
        BotCommand('period',     'Show current period dates and deadline'),
        BotCommand('status',     '(Admin) Show all members contribution status'),
        BotCommand('members',    '(Admin) List all registered members'),
        BotCommand('debug',      '(Admin) Check storage and data status'),
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
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, on_any_message))
    app.add_handler(CallbackQueryHandler(on_paper_info, pattern=r'^pi:'))
    app.add_handler(CallbackQueryHandler(on_approval,   pattern=r'^(approve|reject):'))
    app.add_handler(CommandHandler('period',    cmd_period))
    app.add_handler(CommandHandler('mystatus',  cmd_mystatus))
    app.add_handler(CommandHandler('status',    cmd_status))
    app.add_handler(CommandHandler('setstart',  cmd_setstart))
    app.add_handler(CommandHandler('members',   cmd_members))
    app.add_handler(CommandHandler('debug',     cmd_debug))
    app.add_handler(CommandHandler('scan',      cmd_scan))
    app.add_handler(CommandHandler('addmember', cmd_addmember))
    app.add_error_handler(on_error)

    print("Bot running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
