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

def add_reminder_db(owner_id: int, owner_chat_id: int, adder_chat_id: int, remind_at: datetime, interval_minutes: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reminders (owner_id, owner_chat_id, adder_chat_id, remind_at, interval_minutes, created_at, active) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (owner_id, owner_chat_id, adder_chat_id, remind_at.isoformat(), interval_minutes, datetime.now(timezone.utc).isoformat())
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid

def mark_reminder_done(reminder_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE reminders SET active = 0 WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()

def get_active_reminders():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()
    cur.execute("SELECT id, owner_id, owner_chat_id, adder_chat_id, remind_at, interval_minutes FROM reminders WHERE active = 1 AND remind_at >= ? ORDER BY remind_at ASC", (now_iso,))
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        rid, owner_id, owner_chat_id, adder_chat_id, remind_at_iso, interval = r
        remind_at_dt = datetime.fromisoformat(remind_at_iso).astimezone(timezone.utc)
        result.append((rid, owner_id, owner_chat_id, adder_chat_id, remind_at_dt, interval))
    return result

# ========== Helpers ==========
def add_feeding_and_schedule(owner_id: int, ts_local_or_utc: datetime, ml: Optional[int],
                             owner_chat_id: Optional[int], adder_chat_id: Optional[int],
                             reminder_minutes: Optional[int], context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TIMEZONE)
    if ts_local_or_utc.tzinfo is None:
        ts_utc = ts_local_or_utc.replace(tzinfo=tz).astimezone(timezone.utc)
    else:
        ts_utc = ts_local_or_utc.astimezone(timezone.utc)
    ts_utc = strip_seconds(ts_utc)

    add_feeding_db(owner_id, ts_utc, ml)

    if reminder_minutes:
        remind_at = ts_utc + timedelta(minutes=reminder_minutes)
        rid = add_reminder_db(owner_id, owner_chat_id, adder_chat_id, remind_at, reminder_minutes)
        schedule_reminder_job(context, rid, owner_id, owner_chat_id, adder_chat_id, remind_at, reminder_minutes)

    local = ts_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
    return local

def strip_seconds(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)

def parse_user_datetime(text: str, tz: ZoneInfo):
    """
    Принимает 'YYYY-MM-DD HH:MM' или 'HH:MM' (локальное время в TIMEZONE).
    Возвращает datetime в UTC или None.
    """
    text = text.strip()
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M")
        local = dt.replace(tzinfo=tz)
        return local.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        t = datetime.strptime(text, "%H:%M").time()
        now_local = datetime.now(tz)
        local_dt = datetime(now_local.year, now_local.month, now_local.day, t.hour, t.minute, tzinfo=tz)
        if local_dt.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            local_dt = local_dt + timedelta(days=1)
        return local_dt.astimezone(timezone.utc)
    except Exception:
        return None

def schedule_reminder_job(context: ContextTypes.DEFAULT_TYPE, reminder_id: int, owner_id: int,
                          owner_chat_id: int, adder_chat_id: int, remind_at: datetime, interval_minutes: int):
    delay = (remind_at - datetime.now(timezone.utc)).total_seconds()
    if delay <= 0:
        mark_reminder_done(reminder_id)
        return
    context.application.job_queue.run_once(
        reminder_callback,
        when=delay,
        data={
            "reminder_id": reminder_id,
            "owner_id": owner_id,
            "owner_chat_id": owner_chat_id,
            "adder_chat_id": adder_chat_id,
            "interval": interval_minutes
        }
    )

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
        [InlineKeyboardButton("⏰ Указать время", callback_data="time_custom"),
         InlineKeyboardButton("↩️ Отмена", callback_data="cancel")]
    ]
    return InlineKeyboardMarkup(keys)

def reminder_keyboard():
    keys = [
        [InlineKeyboardButton("⏱ 2:00", callback_data="rem_120"),
         InlineKeyboardButton("⏱ 2:30", callback_data="rem_150")],
        [InlineKeyboardButton("⏱ 3:00", callback_data="rem_180"),
         InlineKeyboardButton("⏱ 3:30", callback_data="rem_210")],
        [InlineKeyboardButton("⏱ 4:00", callback_data="rem_240"),
         InlineKeyboardButton("↩️ Пропустить", callback_data="rem_none")]
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

    elif data == "time_custom":
        context.user_data['awaiting_time'] = {
            "owner_id": owner_id,
            "adder_chat_id": query.from_user.id
        }
        await query.edit_message_text(
            "Введите время кормления в формате:\n"
            "• YYYY-MM-DD HH:MM\n"
            "• или HH:MM (сегодня/завтра)\n\n"
            "Например: 2025-12-30 14:30 или 14:30",
        )
        return
    
    elif data.startswith("ml_"):
        if data == "ml_custom":
            context.user_data['awaiting_ml'] = owner_id
            await query.edit_message_text("Введите количество мл (целое число), например 135. Отправьте сообщение или /cancel.")
            return
    
        ml = int(data.split("_")[1])
    
        # сохраняем pending, но НЕ добавляем кормление сразу
        context.user_data['pending'] = {
            "owner_id": owner_id,
            "ts_utc": datetime.now(timezone.utc),
            "ml": ml,
            "adder_chat_id": query.from_user.id,
            "owner_chat_id": get_owner_chat_id(owner_id) or query.from_user.id
        }
    
        await query.edit_message_text(
            f"Выбрано {ml} мл. Хотите установить напоминание?",
            reply_markup=reminder_keyboard()
        )
        return

    elif data.startswith("rem_"):
        pending = context.user_data.pop('pending', None)
        if not pending:
            await query.edit_message_text("Нет данных для создания напоминания. Начните заново.", reply_markup=main_keyboard())
            return
    
        if data == "rem_none":
            minutes = None
        else:
            minutes = int(data.split("_")[1])
    
        local_str = add_feeding_and_schedule(
            owner_id=pending["owner_id"],
            ts_local_or_utc=pending["ts_utc"],
            ml=pending["ml"],
            owner_chat_id=pending["owner_chat_id"],
            adder_chat_id=pending["adder_chat_id"],
            reminder_minutes=minutes,
            context=context
        )

    await query.edit_message_text(
        f"✅ Кормление добавлено: {local_str} — **{pending['ml']} мл** 🍼",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )
    return

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
    user = update.effective_user
    text = update.message.text.strip()
    tz = ZoneInfo(TIMEZONE)

    # === 1) Пользователь вводит мл после "Другое" (ml_custom) ===
    if context.user_data.get('awaiting_ml'):
        owner_id = context.user_data.pop('awaiting_ml')

        try:
            ml = int(text)
            if ml <= 0:
                raise ValueError()
        except ValueError:
            await update.message.reply_text("Неверное значение. Введите положительное целое число мл или /cancel.")
            return

        # сохраняем pending, но НЕ добавляем кормление сразу
        context.user_data['pending'] = {
            "owner_id": owner_id,
            "ts_utc": datetime.now(timezone.utc),
            "ml": ml,
            "adder_chat_id": update.effective_chat.id,
            "owner_chat_id": get_owner_chat_id(owner_id) or update.effective_chat.id
        }

        await update.message.reply_text(
            f"Вы ввели {ml} мл. Хотите установить напоминание?",
            reply_markup=reminder_keyboard()
        )
        return

    # === 2) Пользователь вводит время после "Указать время" ===
    if context.user_data.get('awaiting_time'):
        info = context.user_data.pop('awaiting_time')

        dt_utc = parse_user_datetime(text, tz)
        if not dt_utc:
            await update.message.reply_text(
                "Неверный формат времени.\n"
                "Используйте YYYY-MM-DD HH:MM или HH:MM.\n"
                "Попробуйте ещё раз или /cancel."
            )
            return

        # теперь нужно спросить мл
        context.user_data['awaiting_ml_for_time'] = {
            "owner_id": info["owner_id"],
            "adder_chat_id": info["adder_chat_id"],
            "owner_chat_id": get_owner_chat_id(info["owner_id"]) or update.effective_chat.id,
            "ts_utc": dt_utc
        }

        await update.message.reply_text("Введите количество мл (целое число):")
        return

    # === 3) Пользователь вводит мл после указания времени ===
    if context.user_data.get('awaiting_ml_for_time'):
        pending = context.user_data.pop('awaiting_ml_for_time')

        try:
            ml = int(text)
            if ml <= 0:
                raise ValueError()
        except ValueError:
            await update.message.reply_text("Введите положительное целое число мл или /cancel.")
            return

        pending["ml"] = ml
        context.user_data['pending'] = pending

        local_time = pending["ts_utc"].astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
        await update.message.reply_text(
            f"Кормление запланировано на {local_time}.\nХотите установить напоминание?",
            reply_markup=reminder_keyboard()
        )
        return

    # === 4) Любой другой текст — подсказка ===
    await update.message.reply_text(
        "Используйте меню или /start чтобы открыть его.",
        reply_markup=main_keyboard()
    )

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

async def reminder_callback(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data or {}
    reminder_id = data.get("reminder_id")
    owner_chat_id = data.get("owner_chat_id")
    adder_chat_id = data.get("adder_chat_id")
    interval = data.get("interval")

    hours = interval // 60
    minutes = interval % 60
    minutes_text = f" {minutes} мин" if minutes else ""
    text = f"⏰ Напоминание: прошло {hours} ч{minutes_text} с последнего кормления. Пора покормить ребёнка! 🍼"

    if owner_chat_id:
        try:
            await context.bot.send_message(chat_id=owner_chat_id, text=text)
        except Exception:
            pass
    if adder_chat_id and adder_chat_id != owner_chat_id:
        try:
            await context.bot.send_message(chat_id=adder_chat_id, text=text)
        except Exception:
            pass

    if reminder_id:
        mark_reminder_done(reminder_id)

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
