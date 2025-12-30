#!/usr/bin/env python3
import os
import sqlite3
import secrets
import string
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ========== Настройки ==========
BOT_TOKEN = os.getenv("BOT_TOKEN")  # задайте через env
TIMEZONE = os.getenv("TIMEZONE", "UTC")  # например "Europe/Riga"
BASE_DIR = os.getenv("BASE_DIR", "/opt/telegram-bot")
DB_PATH = os.path.join(BASE_DIR, "feedings.db")

# ========== БД ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ts_utc TEXT NOT NULL,
            ml INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invites (
            code TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL,
            invited_id INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def add_feeding_db(user_id: int, ts_utc: datetime, ml: int | None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO feedings (user_id, ts_utc, ml) VALUES (?, ?, ?)",
                (user_id, ts_utc.isoformat(), ml))
    conn.commit()
    conn.close()

def get_feedings_last_24h_for_owner(owner_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now_utc = datetime.now(timezone.utc)
    since = (now_utc - timedelta(hours=24)).isoformat()
    cur.execute("SELECT ts_utc, ml FROM feedings WHERE user_id = ? AND ts_utc >= ? ORDER BY ts_utc ASC",
                (owner_id, since))
    rows = cur.fetchall()
    conn.close()
    return [(datetime.fromisoformat(r[0]).astimezone(timezone.utc), r[1]) for r in rows]

def delete_last_feeding(owner_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id FROM feedings WHERE user_id = ? ORDER BY ts_utc DESC LIMIT 1", (owner_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    cur.execute("DELETE FROM feedings WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()
    return True

def delete_all_feedings(owner_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM feedings WHERE user_id = ?", (owner_id,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted

def create_invite_code(owner_id: int) -> str:
    code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO invites (code, owner_id, invited_id, created_at) VALUES (?, ?, NULL, ?)",
                (code, owner_id, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return code

def join_with_code(code: str, invited_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT owner_id, invited_id FROM invites WHERE code = ?", (code,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None, "not_found"
    owner_id, existing = row
    if existing is not None:
        conn.close()
        return None, "already_used"
    cur.execute("UPDATE invites SET invited_id = ? WHERE code = ?", (invited_id, code))
    conn.commit()
    conn.close()
    return owner_id, "ok"

def get_owner_by_invited(invited_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT owner_id FROM invites WHERE invited_id = ?", (invited_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

# ========== UI helpers ==========
def main_keyboard():
    keyboard = [
        [InlineKeyboardButton("➕ Добавить кормление", callback_data="add")],
        [InlineKeyboardButton("📊 Статистика (24ч)", callback_data="stats")],
        [InlineKeyboardButton("🗑️ Удалить последнее", callback_data="del_last"),
         InlineKeyboardButton("🧹 Удалить все", callback_data="del_all")],
        [InlineKeyboardButton("🔗 Поделиться (invite)", callback_data="share")]
    ]
    return InlineKeyboardMarkup(keyboard)

def amount_keyboard():
    keys = [
        [InlineKeyboardButton("90 мл", callback_data="ml_90"),
         InlineKeyboardButton("120 мл", callback_data="ml_120")],
        [InlineKeyboardButton("150 мл", callback_data="ml_150"),
         InlineKeyboardButton("180 мл", callback_data="ml_180")],
        [InlineKeyboardButton("210 мл", callback_data="ml_210"),
         InlineKeyboardButton("✏️ Другое", callback_data="ml_custom")],
        [InlineKeyboardButton("↩️ Отмена", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keys)

# ========== Handlers ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = f"Привет, {user.first_name or 'друг'}! 🍼\nВыбирай действие:"
    await update.message.reply_text(text, reply_markup=main_keyboard())

# state for custom ml input: context.user_data['awaiting_ml'] = owner_id (owner for whom adding)
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    tz = ZoneInfo(TIMEZONE)

    # determine owner: if user is invited, they act on behalf of owner
    user_id = query.from_user.id
    owner_id = get_owner_by_invited(user_id) or user_id

    if data == "add":
        await query.edit_message_text("Выберите объём молока для записи 🥛:", reply_markup=amount_keyboard())

    elif data.startswith("ml_"):
        if data == "ml_custom":
            # ask for custom ml
            context.user_data['awaiting_ml'] = owner_id
            await query.edit_message_text("Введите количество мл (целое число), например 135. Отправьте сообщение или /cancel.")
            return
        # preset
        ml = int(data.split("_")[1])
        now_utc = datetime.now(timezone.utc)
        add_feeding_db(owner_id, now_utc, ml)
        local = now_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
        await query.edit_message_text(f"✅ Кормление добавлено: {local} — **{ml} мл** 🍼", reply_markup=main_keyboard())

    elif data == "cancel":
        await query.edit_message_text("Отменено ↩️", reply_markup=main_keyboard())

    elif data == "stats":
        rows = get_feedings_last_24h_for_owner(owner_id)
        if not rows:
            await query.edit_message_text("За последние 24 часа кормлений не было. 😴", reply_markup=main_keyboard())
            return
        lines = []
        for ts, ml in rows:
            local = ts.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
            ml_text = f" — {ml} мл" if ml else ""
            lines.append(f"{local}{ml_text}")
        msg = "📋 Кормления за последние 24 часа:\n\n" + "\n".join(lines) + f"\n\nОбщее количество: {len(lines)} 🧾"
        await query.edit_message_text(msg, reply_markup=main_keyboard())

    elif data == "del_last":
        ok = delete_last_feeding(owner_id)
        if ok:
            await query.edit_message_text("🗑️ Последнее кормление удалено.", reply_markup=main_keyboard())
        else:
            await query.edit_message_text("Нечего удалять — записей нет.", reply_markup=main_keyboard())

    elif data == "del_all":
        deleted = delete_all_feedings(owner_id)
        await query.edit_message_text(f"🧹 Удалено записей: {deleted}", reply_markup=main_keyboard())

    elif data == "share":
        # create invite code for this user (owner)
        code = create_invite_code(user_id)
        await query.edit_message_text(
            f"🔗 Код приглашения создан: <b>{code}</b>\nОтправьте этот код тому, кого хотите пригласить.\n"
            "Приглашённый должен отправить команду /join <код> в этом боте.",
            parse_mode="HTML",
            reply_markup=main_keyboard()
        )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # handle custom ml input or /join etc.
    user = update.effective_user
    text = update.message.text.strip()

    # custom ml flow
    if context.user_data.get('awaiting_ml'):
        try:
            ml = int(text)
            if ml <= 0:
                raise ValueError()
        except ValueError:
            await update.message.reply_text("Неверное значение. Введите положительное целое число мл или /cancel.")
            return
        owner_id = context.user_data.pop('awaiting_ml')
        now_utc = datetime.now(timezone.utc)
        add_feeding_db(owner_id, now_utc, ml)
        tz = ZoneInfo(TIMEZONE)
        local = now_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
        await update.message.reply_text(f"✅ Кормление добавлено: {local} — **{ml} мл** 🍼", parse_mode="Markdown", reply_markup=main_keyboard())
        return

    # other text: ignore or help
    await update.message.reply_text("Используйте меню или /start чтобы открыть его.", reply_markup=main_keyboard())

# Commands for sharing/joining
async def share_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    code = create_invite_code(user.id)
    await update.message.reply_text(
        f"🔗 Код приглашения: <b>{code}</b>\nОтправьте его человеку, которого хотите пригласить.\n"
        "Он должен выполнить: /join <код>",
        parse_mode="HTML"
    )

async def join_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /join <КОД>\nПример: /join ABC123")
        return
    code = args[0].strip().upper()
    owner_id, status = join_with_code(code, user.id)
    if status == "not_found":
        await update.message.reply_text("Код не найден или неверный. ❌")
    elif status == "already_used":
        await update.message.reply_text("Этот код уже использован. ❌")
    else:
        await update.message.reply_text(f"Вы присоединились к пользователю {owner_id}. Теперь вы можете добавлять кормления и смотреть статистику этого пользователя. ✅")

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_ml'):
        context.user_data.pop('awaiting_ml', None)
        await update.message.reply_text("Ввод отменён.", reply_markup=main_keyboard())
    else:
        await update.message.reply_text("Нет активных операций.", reply_markup=main_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — открыть меню\n"
        "/share — создать код приглашения\n"
        "/join <код> — присоединиться к владельцу по коду\n"
        "/cancel — отменить ввод"
    )

# ========== Запуск ==========
def run():
    init_db()
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Установите переменную окружения BOT_TOKEN.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("share", share_cmd))
    app.add_handler(CommandHandler("join", join_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("Bot started...")
    app.run_polling()

if __name__ == "__main__":
    run()
