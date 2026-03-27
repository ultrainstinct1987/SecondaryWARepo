"""
Telegram Group Contribution Tracker Bot

Every 3 months, members must upload 2 PDFs to the group.
The bot tracks who has and hasn't met the requirement.

Bot Commands:
  /status       — (admin) show all members' contribution status for this period
  /mystatus     — show your own contribution count
  /period       — show current period dates and deadline
  /members      — (admin) list all tracked members
  /setstart DD/MM/YYYY  — (admin) set the start date of the current period

Setup:
  1. Create a bot via @BotFather — get BOT_TOKEN
  2. Add the bot to your group and make it an admin (so it can see all messages)
  3. Fill in BOT_TOKEN and ADMIN_IDS below
  4. pip install -r requirements.txt
  5. python bot.py
"""

import sqlite3
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
import os

BOT_TOKEN     = os.getenv('BOT_TOKEN', '8745432625:AAEcTZSsGqvfmOlUGx5463qtamomRnVnoHk')
ADMIN_IDS     = [int(x) for x in os.getenv('ADMIN_IDS', '411713323').split(',')]
REQUIRED_PDFS = 2
PERIOD_MONTHS = 3
DB_PATH       = 'contributions.db'
# ─────────────────────────────────────────────────────────────────────────────


# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS members (
            user_id   INTEGER PRIMARY KEY,
            username  TEXT,
            full_name TEXT,
            joined_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS contributions (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id   INTEGER,
            filename  TEXT,
            sent_at   TEXT DEFAULT (datetime('now')),
            period    INTEGER
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        INSERT OR IGNORE INTO settings (key, value)
            VALUES ('period_start', date('now'));
    """)
    con.commit()
    con.close()


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def get_period_start() -> datetime:
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key='period_start'").fetchone()
    return datetime.strptime(row['value'], '%Y-%m-%d') if row else datetime.now()


def set_period_start(dt: datetime):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('period_start', ?)",
                    (dt.strftime('%Y-%m-%d'),))


def current_period() -> tuple[int, datetime, datetime]:
    """Return (period_number, period_start, period_end)."""
    start = get_period_start()
    now   = datetime.now()
    months_elapsed = max(0, (now.year - start.year) * 12 + (now.month - start.month))
    period_num     = months_elapsed // PERIOD_MONTHS
    period_start   = start + relativedelta(months=period_num * PERIOD_MONTHS)
    period_end     = period_start + relativedelta(months=PERIOD_MONTHS)
    return period_num, period_start, period_end


def upsert_member(user_id: int, username: str | None, full_name: str):
    with db() as con:
        con.execute("""
            INSERT INTO members (user_id, username, full_name) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username  = excluded.username,
                full_name = excluded.full_name
        """, (user_id, username or '', full_name))


def record_pdf(user_id: int, filename: str, period_num: int):
    with db() as con:
        con.execute(
            "INSERT INTO contributions (user_id, filename, period) VALUES (?, ?, ?)",
            (user_id, filename, period_num)
        )


def contributions_this_period(period_num: int) -> dict[int, list[str]]:
    """Return {user_id: [filename, ...]} for current period."""
    with db() as con:
        rows = con.execute(
            "SELECT user_id, filename FROM contributions WHERE period = ?",
            (period_num,)
        ).fetchall()
    result: dict[int, list[str]] = {}
    for r in rows:
        result.setdefault(r['user_id'], []).append(r['filename'])
    return result


def all_members() -> list:
    with db() as con:
        return con.execute("SELECT * FROM members ORDER BY full_name").fetchall()


def member_count() -> int:
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM members").fetchone()[0]


# ── Helpers ───────────────────────────────────────────────────────────────────
def fmt_date(dt: datetime) -> str:
    return dt.strftime('%d %b %Y')


def user_display(user) -> str:
    name = (user.full_name or '').strip() or 'Unknown'
    if user.username:
        return f"{name} (@{user.username})"
    return name


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ── Handlers ──────────────────────────────────────────────────────────────────
async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register users when they join the group."""
    result = update.chat_member
    if result and result.new_chat_member.status not in ('left', 'kicked', 'banned'):
        u = result.new_chat_member.user
        if not u.is_bot:
            upsert_member(u.id, u.username, u.full_name)
            logging.info(f"Registered new member: {u.full_name} ({u.id})")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track PDF uploads in the group."""
    msg  = update.message
    if not msg or not msg.document:
        return

    doc = msg.document
    # Accept PDFs only
    if doc.mime_type != 'application/pdf' and not (doc.file_name or '').lower().endswith('.pdf'):
        return

    user = msg.from_user
    if not user or user.is_bot:
        return

    # Auto-register member on first PDF (in case they joined before the bot)
    upsert_member(user.id, user.username, user.full_name)

    period_num, _, _ = current_period()
    filename = doc.file_name or f'file_{msg.message_id}.pdf'
    record_pdf(user.id, filename, period_num)

    # Count how many they've uploaded this period
    contribs = contributions_this_period(period_num)
    count = len(contribs.get(user.id, []))

    if count == REQUIRED_PDFS:
        await msg.reply_text(
            f"Thanks {user.first_name}! You've now uploaded {count}/{REQUIRED_PDFS} PDFs "
            f"for this period. Requirement met!"
        )
    elif count < REQUIRED_PDFS:
        remaining = REQUIRED_PDFS - count
        await msg.reply_text(
            f"Got it {user.first_name}! {count}/{REQUIRED_PDFS} PDFs uploaded. "
            f"{remaining} more to go this period."
        )
    # Don't spam if they upload extras beyond the requirement


async def cmd_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current period info."""
    period_num, start, end = current_period()
    now = datetime.now()
    days_left = (end - now).days

    text = (
        f"📅 *Current Period*\n"
        f"Period #{period_num + 1}\n"
        f"Start  : {fmt_date(start)}\n"
        f"End    : {fmt_date(end)}\n"
        f"Days left : {max(0, days_left)}\n\n"
        f"Requirement: {REQUIRED_PDFS} PDFs per member"
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_mystatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the user's own contribution status."""
    user = update.effective_user
    upsert_member(user.id, user.username, user.full_name)

    period_num, start, end = current_period()
    contribs = contributions_this_period(period_num)
    files = contribs.get(user.id, [])
    count = len(files)
    met = count >= REQUIRED_PDFS

    status_icon = "✅" if met else "❌"
    text = (
        f"{status_icon} *Your Status — Period #{period_num + 1}*\n"
        f"({fmt_date(start)} – {fmt_date(end)})\n\n"
        f"Uploaded: {count}/{REQUIRED_PDFS} PDFs\n"
    )
    if files:
        text += "\nFiles uploaded:\n" + "\n".join(f"  • {f}" for f in files)
    if not met:
        text += f"\n\n{REQUIRED_PDFS - count} more PDF(s) needed before {fmt_date(end)}."

    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Admin) Show all members' contribution status."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    period_num, start, end = current_period()
    contribs = contributions_this_period(period_num)
    members  = all_members()

    if not members:
        await update.message.reply_text("No members tracked yet.")
        return

    done   = []
    not_done = []
    for m in members:
        count = len(contribs.get(m['user_id'], []))
        label = f"{m['full_name']}" + (f" (@{m['username']})" if m['username'] else '')
        if count >= REQUIRED_PDFS:
            done.append(f"  ✅ {label} ({count} PDFs)")
        else:
            not_done.append(f"  ❌ {label} ({count}/{REQUIRED_PDFS})")

    text = (
        f"📊 *Contribution Status — Period #{period_num + 1}*\n"
        f"({fmt_date(start)} – {fmt_date(end)})\n\n"
        f"✅ Met requirement ({len(done)}/{len(members)}):\n"
        + ("\n".join(done) if done else "  (none yet)") + "\n\n"
        f"❌ Still pending ({len(not_done)}/{len(members)}):\n"
        + ("\n".join(not_done) if not_done else "  (all done!)")
    )
    await update.message.reply_text(text, parse_mode='Markdown')


async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Admin) List all tracked members."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    members = all_members()
    if not members:
        await update.message.reply_text("No members tracked yet.")
        return

    lines = [f"👥 *Members ({len(members)} total)*\n"]
    for m in members:
        name = m['full_name'] or 'Unknown'
        uname = f" @{m['username']}" if m['username'] else ''
        joined = m['joined_at'][:10] if m['joined_at'] else '?'
        lines.append(f"  • {name}{uname}  (joined {joined})")

    await update.message.reply_text("\n".join(lines), parse_mode='Markdown')


async def cmd_setstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """(Admin) Set the period start date. Usage: /setstart 01/01/2025"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("This command is for admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /setstart DD/MM/YYYY\nExample: /setstart 01/01/2025")
        return

    try:
        dt = datetime.strptime(context.args[0], '%d/%m/%Y')
    except ValueError:
        await update.message.reply_text("Invalid date. Use DD/MM/YYYY format, e.g. 01/01/2025")
        return

    set_period_start(dt)
    period_num, start, end = current_period()
    await update.message.reply_text(
        f"Period start set to {fmt_date(dt)}.\n"
        f"Current period: #{period_num + 1} ({fmt_date(start)} – {fmt_date(end)})"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_member(user.id, user.username, user.full_name)
    await update.message.reply_text(
        f"Hi {user.first_name}! You're now being tracked.\n"
        f"Use /mystatus to see your contribution status, or /period to see the current period."
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        print("ERROR: Fill in BOT_TOKEN in bot.py first.")
        return

    if not ADMIN_IDS:
        print("WARNING: ADMIN_IDS is empty — no one can use admin commands.")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(ChatMemberHandler(on_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CommandHandler('start',    cmd_start))
    app.add_handler(CommandHandler('period',   cmd_period))
    app.add_handler(CommandHandler('mystatus', cmd_mystatus))
    app.add_handler(CommandHandler('status',   cmd_status))
    app.add_handler(CommandHandler('members',  cmd_members))
    app.add_handler(CommandHandler('setstart', cmd_setstart))

    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
