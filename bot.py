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

import asyncio
import io
import os
import re
import json
import logging
import fitz  # PyMuPDF
from datetime import datetime
from dateutil.relativedelta import relativedelta
from telethon import TelegramClient
from telethon.sessions import StringSession
from telegram import Update, BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeChat, BotCommandScopeDefault, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ChatMemberHandler, CallbackQueryHandler, filters, ContextTypes
)

# ── PDF compression ───────────────────────────────────────────────────────────
def compress_pdf_bytes(pdf_bytes: bytes) -> bytes:
    """
    Compress a scanned PDF by re-rendering each page at 150 DPI as JPEG.
    Significantly reduces file size for image-heavy scanned PDFs.
    """
    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()

    for page in src:
        pix = page.get_pixmap(matrix=fitz.Matrix(150/72, 150/72))
        img_bytes = pix.tobytes(output="jpeg", jpg_quality=75)
        new_page = out.new_page(width=page.rect.width, height=page.rect.height)
        new_page.insert_image(new_page.rect, stream=img_bytes)

    result = out.tobytes(deflate=True, garbage=4)
    src.close()
    out.close()
    return result


# ── Paper info collection ─────────────────────────────────────────────────────
EXAM_TYPES = ['WA1', 'WA2', 'WA3', 'EOY', 'Practice']
YEARS      = ['2023', '2024', '2025', '2026']

def build_filename(info: dict) -> str:
    """Build standardised filename from collected paper info."""
    year    = info['year']
    level   = info['level']                            # S1 / S2 / S3 / S4
    subject = info['subject']                          # EM / AM
    grade   = info['grade']                            # G1 / G2 / G3
    etype   = info['exam_type']
    school  = re.sub(r'\s+', '', info['school'].lower())
    return f"{year}_{level}_{subject}_{grade}_{etype}_{school}.pdf"

def summary_text(info: dict) -> str:
    return (
        f"Level    : {info.get('level','—')}\n"
        f"Subject  : {info.get('subject','—')}\n"
        f"Grade    : {info.get('grade','—')}\n"
        f"Exam type: {info.get('exam_type','—')}\n"
        f"Year     : {info.get('year','—')}\n"
        f"School   : {info.get('school','—')}"
    )

def kb_mode():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('📋 Fill in form',    callback_data='pi:mode:form'),
        InlineKeyboardButton('✏️ Rename manually', callback_data='pi:mode:manual'),
    ]])

def kb_level():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('Sec 1', callback_data='pi:level:1'),
         InlineKeyboardButton('Sec 2', callback_data='pi:level:2'),
         InlineKeyboardButton('Sec 3', callback_data='pi:level:3'),
         InlineKeyboardButton('Sec 4', callback_data='pi:level:4')],
        [InlineKeyboardButton('⬅️ Back', callback_data='pi:back:mode')],
    ])

def kb_subject():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('EM', callback_data='pi:subject:EM'),
         InlineKeyboardButton('AM', callback_data='pi:subject:AM')],
        [InlineKeyboardButton('⬅️ Back', callback_data='pi:back:level')],
    ])

def kb_grade():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('G1', callback_data='pi:grade:G1'),
         InlineKeyboardButton('G2', callback_data='pi:grade:G2'),
         InlineKeyboardButton('G3', callback_data='pi:grade:G3')],
        [InlineKeyboardButton('⬅️ Back', callback_data='pi:back:subject')],
    ])

def kb_examtype():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(e, callback_data=f'pi:examtype:{e}') for e in EXAM_TYPES[:3]],
        [InlineKeyboardButton(e, callback_data=f'pi:examtype:{e}') for e in EXAM_TYPES[3:]],
        [InlineKeyboardButton('⬅️ Back', callback_data='pi:back:grade')],
    ])

def kb_year():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(y, callback_data=f'pi:year:{y}') for y in YEARS],
        [InlineKeyboardButton('⬅️ Back', callback_data='pi:back:examtype')],
    ])

def kb_confirm():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Confirm', callback_data='pi:confirm:yes'),
         InlineKeyboardButton('❌ Cancel',  callback_data='pi:confirm:no')],
        [InlineKeyboardButton('⬅️ Back',    callback_data='pi:back:school')],
    ])

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN          = os.getenv('BOT_TOKEN',          '8745432625:AAEcTZSsGqvfmOlUGx5463qtamomRnVnoHk')
ADMIN_IDS          = [int(x) for x in os.getenv('ADMIN_IDS', '411713323').split(',')]
STORAGE_CHANNEL_ID = int(os.getenv('STORAGE_CHANNEL_ID', '-1003662706262'))
REQUIRED_PDFS      = 2
PERIOD_MONTHS      = 3

# Telethon — needed to download files over 20 MB (Bot API limit)
# Run get_session.py once locally, then set TELETHON_SESSION as an env var on Railway
API_ID           = int(os.getenv('API_ID',   '39978206'))
API_HASH         = os.getenv('API_HASH',     '5974a0eaf7d6464a7ebc72c567f1a802')
TELETHON_SESSION = os.getenv('TELETHON_SESSION', '')
# ─────────────────────────────────────────────────────────────────────────────

_tl_session = StringSession(TELETHON_SESSION) if TELETHON_SESSION else 'session'
tl_client   = TelegramClient(_tl_session, API_ID, API_HASH)


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
async def _delete_after(bot, chat_id: int, message_id: int, delay: int = 60):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


def fmt(dt: datetime) -> str:
    return dt.strftime('%d %b %Y')


def period_dates(data: dict) -> tuple[datetime, datetime]:
    start = datetime.strptime(data['period_start'], '%Y-%m-%d')
    return start, start + relativedelta(months=PERIOD_MONTHS)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def _delete_cmd(update: Update):
    """Silently delete the command message."""
    try:
        await update.message.delete()
    except Exception:
        pass


# ── Handlers ──────────────────────────────────────────────────────────────────
async def on_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silently register members. Also captures school name during paper info collection."""
    msg  = update.message
    user = msg.from_user if msg else None
    if not user or user.is_bot:
        return

    # Capture text input during paper info collection
    col = context.user_data.get('collecting')
    if col and col['uid'] == str(user.id):
        if col.get('step') == 'school':
            col['school']        = msg.text.strip()
            col['school_msg_id'] = msg.message_id
            filename = build_filename(col)
            col['step'] = 'confirm'
            reply = await msg.reply_text(
                f"Rename to:\n<code>{filename}</code>\n\n"
                f"<i>{summary_text(col)}</i>\n\nConfirm?",
                parse_mode='HTML',
                reply_markup=kb_confirm()
            )
            col['confirm_msg_id'] = reply.message_id
            return

        if col.get('step') == 'manual_name':
            name = msg.text.strip()
            if not name.lower().endswith('.pdf'):
                name += '.pdf'
            col['manual_filename'] = name
            col['school_msg_id']   = msg.message_id  # reuse for cleanup
            col['step'] = 'confirm'
            reply = await msg.reply_text(
                f"Rename to:\n<code>{name}</code>\n\nConfirm?",
                parse_mode='HTML',
                reply_markup=kb_confirm()
            )
            col['confirm_msg_id'] = reply.message_id
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


async def _start_next_queued(context: ContextTypes.DEFAULT_TYPE):
    """Start the next queued file form if one exists."""
    queue = context.user_data.get('queue', [])
    if not queue:
        return
    nxt = queue.pop(0)
    context.user_data['queue'] = queue
    nxt['step'] = 'level'
    context.user_data['collecting'] = nxt
    remaining = len(queue)
    note = f"\n<i>({remaining} more in queue)</i>" if remaining else ""
    prompt = await context.bot.send_message(
        nxt['chat_id'],
        f"📄 <b>Next file ({nxt['user_name']}).</b>{note}\n\nHow would you like to name this file?",
        message_thread_id=nxt.get('thread_id'),
        parse_mode='HTML',
        reply_markup=kb_mode(),
        disable_notification=True
    )
    context.user_data['collecting']['prompt_msg_id'] = prompt.message_id


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

    large = bool(doc.file_size and doc.file_size > 20 * 1024 * 1024)
    entry = {
        'file_id':      doc.file_id,
        'chat_id':      msg.chat_id,
        'thread_id':    msg.message_thread_id,
        'orig_msg_id':  msg.message_id,
        'uid':          uid,
        'user_name':    user.full_name,
        'use_telethon': large,
    }

    # If a form is already active, queue this file
    if context.user_data.get('collecting'):
        queue = context.user_data.setdefault('queue', [])
        queue.append(entry)
        position = len(queue)
        await msg.reply_text(
            f"📥 Added to queue (position {position}). "
            f"This file will be processed after the current one.",
        )
        return

    # Start paper info collection — ask how to name the file
    entry['step'] = 'mode'
    context.user_data['collecting'] = entry
    prompt = await msg.reply_text(
        f"📄 <b>{user.first_name}</b> uploaded a PDF.\n\nHow would you like to name this file?",
        parse_mode='HTML',
        reply_markup=kb_mode(),
        disable_notification=True
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

    if field == 'mode':
        if value == 'form':
            col['step'] = 'level'
            await query.edit_message_text(
                "Step 1/6 — Select level:",
                parse_mode='HTML',
                reply_markup=kb_level()
            )
        else:  # manual
            col['step'] = 'manual_name'
            await query.edit_message_text(
                "✏️ <b>Manual rename</b>\n\n"
                "Type the filename below (without <code>.pdf</code>):\n"
                "Example: <code>2026_S4_AM_G3_WA1_JSS</code>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton('⬅️ Back', callback_data='pi:back:mode')
                ]])
            )

    elif field == 'level':
        col['level'] = f'S{value}'
        col['step']  = 'subject'
        await query.edit_message_text(
            f"Step 2/6 — Select subject:\n<i>Level: S{value} ✅</i>",
            parse_mode='HTML',
            reply_markup=kb_subject()
        )

    elif field == 'subject':
        col['subject'] = value
        col['step']    = 'grade'
        await query.edit_message_text(
            f"Step 3/6 — Select grade:\n<i>{summary_text(col)}</i>",
            parse_mode='HTML',
            reply_markup=kb_grade()
        )

    elif field == 'grade':
        col['grade'] = value
        col['step']  = 'examtype'
        await query.edit_message_text(
            f"Step 4/6 — Select exam type:\n<i>{summary_text(col)}</i>",
            parse_mode='HTML',
            reply_markup=kb_examtype()
        )

    elif field == 'examtype':
        col['exam_type'] = value
        col['step']      = 'year'
        await query.edit_message_text(
            f"Step 5/6 — Select year:\n<i>{summary_text(col)}</i>",
            parse_mode='HTML',
            reply_markup=kb_year()
        )

    elif field == 'year':
        col['year'] = value
        col['step'] = 'school'
        await query.edit_message_text(
            f"<i>{summary_text(col)}</i>",
            parse_mode='HTML'
        )
        last_step_msg = await context.bot.send_message(
            col['chat_id'],
            "⬇️ <b>Last step!</b>\n\n"
            "Type the <b>school name</b> in the chat and send it.\n"
            "Example: <code>Anglican High</code>",
            message_thread_id=col.get('thread_id'),
            parse_mode='HTML',
            disable_notification=True,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('⬅️ Back', callback_data='pi:back:year')
            ]])
        )
        col['last_step_msg_id'] = last_step_msg.message_id

    elif field == 'back':
        target = value  # the step to go back TO

        # Clear all fields set at or after the target step
        clear_map = {
            'mode':     ['level', 'subject', 'grade', 'exam_type', 'year', 'manual_filename'],
            'level':    ['level', 'subject', 'grade', 'exam_type', 'year'],
            'subject':  ['subject', 'grade', 'exam_type', 'year'],
            'grade':    ['grade', 'exam_type', 'year'],
            'examtype': ['exam_type', 'year'],
            'year':     ['year'],
            'school':   ['school'],
        }
        for f in clear_map.get(target, []):
            col.pop(f, None)
        col['step'] = target

        if target == 'mode':
            await query.edit_message_text(
                f"📄 How would you like to name this file?",
                parse_mode='HTML',
                reply_markup=kb_mode()
            )

        elif target == 'school':
            # Back from confirm: delete confirm msg and school text msg, re-show school prompt
            try:
                await query.message.delete()
            except Exception:
                pass
            col.pop('confirm_msg_id', None)
            if col.get('school_msg_id'):
                try:
                    await context.bot.delete_message(col['chat_id'], col['school_msg_id'])
                except Exception:
                    pass
                col.pop('school_msg_id', None)
            # last_step_msg is still visible with its Back button — user can type again

        elif target == 'year':
            # Back from school prompt: delete last_step_msg, restore year keyboard on prompt_msg
            try:
                await query.message.delete()
            except Exception:
                pass
            col.pop('last_step_msg_id', None)
            await context.bot.edit_message_text(
                chat_id=col['chat_id'],
                message_id=col['prompt_msg_id'],
                text=f"Step 5/6 — Select year:\n<i>{summary_text(col)}</i>",
                parse_mode='HTML',
                reply_markup=kb_year()
            )

        elif target == 'level':
            remaining = len(context.user_data.get('queue', []))
            note = f"\n<i>({remaining} more in queue)</i>" if remaining else ""
            await query.edit_message_text(
                f"📄 <b>{col['user_name']}</b> uploaded a PDF.{note}\n\nStep 1/6 — Select level:",
                parse_mode='HTML',
                reply_markup=kb_level()
            )

        elif target == 'subject':
            await query.edit_message_text(
                f"Step 2/6 — Select subject:\n<i>Level: {col.get('level','—')} ✅</i>",
                parse_mode='HTML',
                reply_markup=kb_subject()
            )

        elif target == 'grade':
            await query.edit_message_text(
                f"Step 3/6 — Select grade:\n<i>{summary_text(col)}</i>",
                parse_mode='HTML',
                reply_markup=kb_grade()
            )

        elif target == 'examtype':
            await query.edit_message_text(
                f"Step 4/6 — Select exam type:\n<i>{summary_text(col)}</i>",
                parse_mode='HTML',
                reply_markup=kb_examtype()
            )

    elif field == 'confirm':
        if value == 'no':
            col = context.user_data.pop('collecting')
            for mid in [col.get('prompt_msg_id'), col.get('last_step_msg_id')]:
                if mid:
                    try:
                        await context.bot.delete_message(col['chat_id'], mid)
                    except Exception:
                        pass
            await query.message.delete()
            await _start_next_queued(context)
            return

        # Download, rename, re-upload, then send for approval
        await query.edit_message_text("⏳ Processing...")
        col = context.user_data.pop('collecting')

        filename = col.get('manual_filename') or build_filename(col)

        try:
            bio = io.BytesIO()
            if col.get('use_telethon'):
                if not tl_client.is_connected():
                    await query.edit_message_text(
                        "❌ This file is over 20 MB and the large-file client is not connected.\n"
                        "Make sure TELETHON_SESSION is set correctly in Railway Variables."
                    )
                    return
                tl_msg = await tl_client.get_messages(col['chat_id'], ids=col['orig_msg_id'])
                if tl_msg is None:
                    await query.edit_message_text("❌ Could not find the original message. Please re-upload the file.")
                    return
                await tl_client.download_media(tl_msg, bio)
            else:
                tg_file = await context.bot.get_file(col['file_id'])
                await tg_file.download_to_memory(out=bio)
            bio.seek(0)
            if bio.getbuffer().nbytes == 0:
                await query.edit_message_text("❌ Downloaded file is empty. Please re-upload and try again.")
                return

            # Compress only large files (over 20 MB, downloaded via Telethon)
            if col.get('use_telethon'):
                compressed = await asyncio.get_event_loop().run_in_executor(
                    None, compress_pdf_bytes, bio.read()
                )
                bio = io.BytesIO(compressed)

            sent = await context.bot.send_document(
                chat_id=col['chat_id'],
                message_thread_id=col.get('thread_id'),
                document=bio,
                filename=filename,
                caption=f"📄 {filename}\nUploaded by: {col['user_name']}"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Failed to process file: {e}")
            return

        # Delete original PDF and all intermediate bot/user messages
        for mid in [
            col.get('orig_msg_id'),
            col.get('prompt_msg_id'),
            col.get('last_step_msg_id'),
            col.get('school_msg_id'),
            col.get('confirm_msg_id'),
        ]:
            if mid:
                try:
                    await context.bot.delete_message(col['chat_id'], mid)
                except Exception:
                    pass

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
            message_thread_id=col.get('thread_id'),
            parse_mode='HTML',
            reply_markup=keyboard
        )

        await _start_next_queued(context)


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

    await query.message.delete()

    data['pending'] = pending
    await tg_save(context.bot, data)


async def cmd_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_cmd(update)
    data = await load_and_check(context.bot)
    start, end = period_dates(data)
    days_left  = max(0, (end - datetime.now()).days)
    reply = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text=(
            f"📅 <b>Current Period</b>\n"
            f"Start    : {fmt(start)}\n"
            f"End      : {fmt(end)}\n"
            f"Days left: {days_left}\n\n"
            f"Requirement: {REQUIRED_PDFS} PDFs per member"
        ),
        parse_mode='HTML'
    )
    asyncio.create_task(_delete_after(context.bot, reply.chat_id, reply.message_id, delay=30))


async def cmd_mystatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_cmd(update)
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

    reply = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text=text,
        parse_mode='HTML'
    )
    asyncio.create_task(_delete_after(context.bot, reply.chat_id, reply.message_id, delay=30))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_cmd(update)
    if not is_admin(update.effective_user.id):
        return

    data = await load_and_check(context.bot)
    start, end = period_dates(data)
    members = data['members']

    if not members:
        r = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            message_thread_id=update.message.message_thread_id,
            text="No members tracked yet."
        )
        asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))
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
    r = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text=text,
        parse_mode='HTML'
    )
    asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))


async def cmd_setstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_cmd(update)
    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        r = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            message_thread_id=update.message.message_thread_id,
            text="Usage: /setstart DD/MM/YYYY\nExample: /setstart 01/04/2025"
        )
        asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))
        return

    try:
        dt = datetime.strptime(context.args[0], '%d/%m/%Y')
    except ValueError:
        r = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            message_thread_id=update.message.message_thread_id,
            text="Invalid date. Use DD/MM/YYYY, e.g. 01/04/2025"
        )
        asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))
        return

    data = await tg_load(context.bot)
    data['period_start']  = dt.strftime('%Y-%m-%d')
    data['contributions'] = {}
    data['pending']       = {}
    await tg_save(context.bot, data)

    _, end = period_dates(data)
    r = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text=(
            f"New period started: {fmt(dt)} → {fmt(end)}\n"
            f"Contributions and pending approvals have been reset."
        )
    )
    asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))


async def cmd_resetperiod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_cmd(update)
    if not is_admin(update.effective_user.id):
        return

    data = await tg_load(context.bot)
    start, end = period_dates(data)
    data['contributions'] = {}
    data['pending']       = {}
    await tg_save(context.bot, data)

    r = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text=(
            f"🗑 <b>Period reset.</b>\n"
            f"All contributions and pending approvals cleared.\n"
            f"Period dates unchanged: {fmt(start)} → {fmt(end)}"
        ),
        parse_mode='HTML'
    )
    asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))


async def cmd_newperiod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_cmd(update)
    if not is_admin(update.effective_user.id):
        return

    today = datetime.now()
    data  = await tg_load(context.bot)
    data['period_start']  = today.strftime('%Y-%m-%d')
    data['contributions'] = {}
    data['pending']       = {}
    await tg_save(context.bot, data)

    _, end = period_dates(data)
    r = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text=(
            f"🆕 <b>New period started.</b>\n"
            f"{fmt(today)} → {fmt(end)}\n"
            f"All contributions and pending approvals have been reset."
        ),
        parse_mode='HTML'
    )
    asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_cmd(update)
    if not is_admin(update.effective_user.id):
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
        msg_text = f"Scanned admins. Added {len(added)} new member(s):\n" + "\n".join(f"  • {n}" for n in added)
    else:
        msg_text = (
            "Scanned admins — no new members to add.\n\n"
            "For regular members, ask them to send any message and they'll be registered automatically."
        )
    r = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text=msg_text
    )
    asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))


async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_cmd(update)
    if not is_admin(update.effective_user.id):
        return

    data = await load_and_check(context.bot)
    members = data['members']

    if not members:
        r = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            message_thread_id=update.message.message_thread_id,
            text=(
                "No members registered yet.\n\n"
                "Run /debug to check if storage is working, then use /scan to add admins."
            )
        )
        asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))
        return

    lines = [f"👥 <b>Registered Members ({len(members)})</b>\n"]
    for i, (uid, info) in enumerate(members.items(), 1):
        name  = info.get('name', 'Unknown')
        uname = f" @{info['username']}" if info.get('username') else ''
        lines.append(f"{i}. {name}{uname}")

    r = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text="\n".join(lines),
        parse_mode='HTML'
    )
    asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_cmd(update)
    if not is_admin(update.effective_user.id):
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

    r = await context.bot.send_message(
        chat_id=update.effective_chat.id,
        message_thread_id=update.message.message_thread_id,
        text="\n".join(lines),
        parse_mode='HTML'
    )
    asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))


async def cmd_addmember(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _delete_cmd(update)
    if not is_admin(update.effective_user.id):
        return

    replied = update.message.reply_to_message

    chat_id   = update.effective_chat.id
    thread_id = update.message.message_thread_id

    if not replied:
        r = await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=(
                "How to use:\n"
                "1. Long-press any message from the member\n"
                "2. Tap Reply\n"
                "3. Type /addmember manually (do not pick from the / menu)\n"
                "4. Send"
            )
        )
        asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))
        return

    u = replied.from_user
    if not u or u.is_bot:
        r = await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text="That message is from a bot — cannot add."
        )
        asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))
        return

    data = await load_and_check(context.bot)
    uid     = str(u.id)
    already = uid in data['members']
    data['members'][uid] = {'name': u.full_name, 'username': u.username or ''}
    err = await tg_save(context.bot, data)

    if err:
        reply_text = (
            f"Storage error — could not save.\n\n"
            f"Error: {err}\n\n"
            f"Run /debug to check the storage channel setup."
        )
    elif already:
        reply_text = f"Updated: {u.full_name} (already registered)"
    else:
        reply_text = f"Added: {u.full_name}"
    r = await context.bot.send_message(
        chat_id=chat_id,
        message_thread_id=thread_id,
        text=reply_text
    )
    asyncio.create_task(_delete_after(context.bot, r.chat_id, r.message_id, delay=30))


# ── Register commands ─────────────────────────────────────────────────────────
async def post_init(app: Application):
    user_commands = [
        BotCommand('mystatus', 'Check your own contribution status'),
        BotCommand('period',   'Show current period dates and deadline'),
    ]
    admin_commands = user_commands + [
        BotCommand('status',    'Show all members contribution status'),
        BotCommand('members',   'List all registered members'),
        BotCommand('debug',     'Check storage and data status'),
        BotCommand('scan',      'Register all group admins'),
        BotCommand('addmember', 'Reply to a message to register that member'),
        BotCommand('setstart',     'Start a new period — /setstart DD/MM/YYYY'),
        BotCommand('newperiod',    'Start a new period from today and reset contributions'),
        BotCommand('resetperiod',  'Reset contributions only, keep current period dates'),
    ]

    # Show only user commands to everyone by default
    await app.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    # Show full command list to group admins in all group chats
    await app.bot.set_my_commands(admin_commands, scope=BotCommandScopeAllChatAdministrators())

    # Show full command list to each admin in their private chat with the bot
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as e:
            logging.warning(f"Could not set admin commands for {admin_id}: {e}")

    # Start Telethon for large file support
    try:
        await tl_client.start()
        logging.info("Telethon client started — large file support enabled.")
    except Exception as e:
        logging.warning(f"Telethon failed to start: {e}. Files over 20 MB will not be supported.")


async def post_shutdown(app: Application):
    try:
        await tl_client.disconnect()
    except Exception:
        pass


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

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
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
    app.add_handler(CommandHandler('scan',        cmd_scan))
    app.add_handler(CommandHandler('addmember',   cmd_addmember))
    app.add_handler(CommandHandler('resetperiod', cmd_resetperiod))
    app.add_handler(CommandHandler('newperiod',   cmd_newperiod))
    app.add_error_handler(on_error)

    print("Bot running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
