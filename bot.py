import os
import asyncio
import logging
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters
)

# =====================
# Basic setup
# =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Prague")  # Your timezone
DB_PATH = os.getenv("BOT_DB", "bot.db")
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("Please set BOT_TOKEN environment variable with your BotFather token.")

# =====================
# Database helpers
# =====================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL UNIQUE,
            chat_id INTEGER NOT NULL,
            username TEXT
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            time_str TEXT NOT NULL,          -- HH:MM 24h
            days TEXT NOT NULL,              -- comma-separated ISO weekday numbers 1..7
            created_at TEXT NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            done_date TEXT NOT NULL          -- YYYY-MM-DD in Europe/Prague
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS leaderboard (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            week_start TEXT NOT NULL,        -- Monday YYYY-MM-DD (Europe/Prague)
            points INTEGER NOT NULL
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS timezones (
            user_id INTEGER PRIMARY KEY,
            tz_name TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


# Utility functions

def iso_week_monday(d: date) -> date:
    return d - timedelta(days=(d.isoweekday() - 1))


def today_for_user(user_id: int) -> date:
    return datetime.now(get_user_tz(user_id)).date()

def now_for_user(user_id: int) -> datetime:
    return datetime.now(get_user_tz(user_id))


def parse_time_str(s: str) -> time | None:
    try:
        hh, mm = s.strip().split(":")
        hh, mm = int(hh), int(mm)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return time(hh, mm)
        return None
    except Exception:
        return None


def upsert_leaderboard_points(user_id: int, week_start: date, delta: int):
    conn = db()
    cur = conn.cursor()
    ws = week_start.isoformat()
    cur.execute("SELECT id, points FROM leaderboard WHERE user_id=? AND week_start=?", (user_id, ws))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE leaderboard SET points=? WHERE id=?", (row[1] + delta, row[0]))
    else:
        cur.execute("INSERT INTO leaderboard (user_id, week_start, points) VALUES (?,?,?)", (user_id, ws, delta))
    conn.commit()
    conn.close()


def get_user(chat_user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (chat_user_id,))
    row = cur.fetchone()
    conn.close()
    return row

def get_user_tz(user_id: int) -> ZoneInfo:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT tz_name FROM timezones WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    tz_name = row[0] if row else "Europe/Prague"  # По умолчанию Прага
    return ZoneInfo(tz_name)

def set_user_tz(user_id: int, tz_name: str):
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO timezones (user_id, tz_name) VALUES (?, ?)",
        (user_id, tz_name)
    )
    conn.commit()
    conn.close()

def ensure_user(update: Update):
    u = update.effective_user
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE user_id=?", (u.id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users (user_id, chat_id, username) VALUES (?,?,?)",
            (u.id, update.effective_chat.id, u.username or "")
        )
        conn.commit()
    conn.close()


# =====================
# Conversation to create a task
# =====================
ASK_NAME, ASK_TIME, ASK_DAYS, CONFIRM = range(4)


def days_keyboard(selected: set[int] | None = None) -> InlineKeyboardMarkup:
    selected = selected or set()
    labels = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    rows = []
    row = []
    for i, lbl in enumerate(labels, start=1):
        prefix = "✅ " if i in selected else "▫️ "
        row.append(InlineKeyboardButton(prefix + lbl, callback_data=f"toggle_day:{i}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Готово", callback_data="days_done")])
    return InlineKeyboardMarkup(rows)


async def newtask_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    await update.message.reply_text("Название задачи? (например: Английский 30 мин)")
    return ASK_NAME


async def newtask_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["task_name"] = update.message.text.strip()
    await update.message.reply_text("Во сколько выполнять? Напиши время в формате HH:MM (например 09:00)")
    return ASK_TIME


async def newtask_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = parse_time_str(update.message.text)
    if not t:
        await update.message.reply_text("Формат времени неверный. Пример: 07:30")
        return ASK_TIME
    context.user_data["task_time"] = t.strftime("%H:%M")
    context.user_data["task_days"] = set()
    await update.message.reply_text(
        "Выбери дни недели для этой задачи (нажимай, затем 'Готово')",
        reply_markup=days_keyboard(set())
    )
    return ASK_DAYS


async def newtask_days_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, val = q.data.split(":")
    sel: set[int] = context.user_data.get("task_days", set())
    d = int(val)
    if d in sel:
        sel.remove(d)
    else:
        sel.add(d)
    context.user_data["task_days"] = sel
    await q.edit_message_reply_markup(reply_markup=days_keyboard(sel))


async def newtask_days_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sel: set[int] = context.user_data.get("task_days", set())
    if not sel:
        await q.edit_message_text("Нужно выбрать хотя бы один день. Выбери дни и снова нажми 'Готово'.")
        return ASK_DAYS
    # Save to DB
    name = context.user_data["task_name"]
    time_str = context.user_data["task_time"]
    days_csv = ",".join(str(x) for x in sorted(sel))

    u = update.effective_user
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tasks (user_id, name, time_str, days, created_at) VALUES (?,?,?,?,?)",
        (u.id, name, time_str, days_csv, now_for_user(update.effective_user.id).isoformat())
    )
    conn.commit()
    conn.close()

# 👉 сразу создаём 3 напоминания для только что добавленной задачи
    schedule_task_jobs(context.application, u.id, name, time_str, days_csv)

    await q.edit_message_text(f"✅ Задача создана: {name}\nВремя: {time_str}\nДни: {days_csv}")

# (старый общий пересчёт можно убрать, он больше не обязателен)
# await schedule_all_user_tasks(update.get_bot(), u.id)
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# =====================
# List tasks & mark as done
# =====================
async def mytasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    u = update.effective_user
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, time_str, days FROM tasks WHERE user_id=? ORDER BY id DESC", (u.id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("У тебя пока нет задач. Создай через /newtask")
        return


    # Build a message per task with a Done button
    today = today_for_user(update.effective_user.id)
    iso_wd = today.isoweekday()
    for r in rows:
        task_id, name, times, days = r[0], r[1], r[2], r[3]
        scheduled_today = str(iso_wd) in days.split(",")
        # Check if already done today
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM completions WHERE task_id=? AND done_date=?", (task_id, today.isoformat()))
        done = cur.fetchone() is not None
        conn.close()

        btns = []
        if scheduled_today and not done:
            btns = [[InlineKeyboardButton("✅ Отметить выполнено (сегодня)", callback_data=f"done:{task_id}")]]
        elif done:
            btns = [[InlineKeyboardButton("✔️ Выполнено сегодня", callback_data="noop")]]
        else:
            btns = [[InlineKeyboardButton("Сегодня не по расписанию", callback_data="noop")]]
        await update.message.reply_text(
            f"📝 {name}\n⏰ {times}\n📅 Дни: {days}\n",
            reply_markup=InlineKeyboardMarkup(btns)
        )


async def mark_done_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, task_id_s = q.data.split(":", 1)
    task_id = int(task_id_s)
    today = today_for_user(update.effective_user.id)

    # Check if already marked
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM completions WHERE task_id=? AND done_date=?", (task_id, today.isoformat()))
    if cur.fetchone():
        await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✔️ Уже отмечено", callback_data="noop")]]))
        # Сообщение пользователю
        await q.message.reply_text(
            "💪 Отлично, ты справился с задачей и получил 10 монет! "
            "Посмотри, как у других через /leaderboard 👀"
        )
        conn.close()
        return

    # Insert completion
    cur.execute("INSERT INTO completions (task_id, done_date) VALUES (?,?)", (task_id, today.isoformat()))

    # Add 10 points to leaderboard for current week
    cur.execute("SELECT user_id FROM tasks WHERE id=?", (task_id,))
    row = cur.fetchone()
    user_id = row[0]
    conn.commit()
    conn.close()

    upsert_leaderboard_points(user_id, iso_week_monday(today), 10)

    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✔️ Выполнено!", callback_data="noop")]]))


# =====================
# Progress & Leaderboard
# =====================
TIMEZONE_OPTIONS = {
    "Europe/Prague": "🇨🇿 Европа/Прага",
    "Asia/Baku": "🇦🇿 Баку",
    "Europe/Moscow": "🇷🇺 Москва"
}

async def settimezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"settz:{tz}")]
        for tz, label in TIMEZONE_OPTIONS.items()
    ]
    await update.message.reply_text(
        "Выбери свой часовой пояс:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def settimezone_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tz = q.data.split(":")[1]
    user_id = update.effective_user.id
    set_user_tz(user_id, tz)
    await q.edit_message_text(f"✅ Твой часовой пояс установлен: {TIMEZONE_OPTIONS[tz]}")
    
async def progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, time_str, days FROM tasks WHERE user_id=?", (u.id,))
    tasks_rows = cur.fetchall()
    if not tasks_rows:
        conn.close()
        await update.message.reply_text("Нет задач. Создай через /newtask")
        return

    # current week range
    today = today_for_user(update.effective_user.id)
    week_start = iso_week_monday(today)
    week_dates = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]

    lines = ["📊 Прогресс за эту неделю:"]
    total_points = 0

    for t in tasks_rows:
        tid, name, _, days_csv = t[0], t[1], t[2], t[3]
        days = set(int(x) for x in days_csv.split(","))
        scheduled_dates = [d for d in week_dates if date.fromisoformat(d).isoweekday() in days]

        cur.execute("SELECT done_date FROM completions WHERE task_id=? AND done_date BETWEEN ? AND ?",
                    (tid, week_start.isoformat(), (week_start + timedelta(days=6)).isoformat()))
        done_dates = {r[0] for r in cur.fetchall()}

        done_count = len(done_dates)
        sched_count = len(scheduled_dates)
        points_now = done_count * 10
        total_points += points_now
        percent = int(round((done_count / sched_count) * 100)) if sched_count else 0
        lines.append(f"• {name}: {done_count}/{sched_count} ({percent}%) → {points_now} монет")

    # Leaderboard points so far (without weekly bonus)
    cur.execute("SELECT points FROM leaderboard WHERE user_id=? AND week_start=?", (u.id, week_start.isoformat()))
    row = cur.fetchone()
    lb_points = row[0] if row else 0
    conn.close()

    lines.append("")
    lines.append(f"💰 Твои монеты за неделю (без бонусов): {lb_points}")
    lines.append("🔔 В понедельник добавятся бонусы (+30 за каждую задачу, где выполнены все запланированные дни).")

    await update.message.reply_text("\n".join(lines))


# async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     today = today_for_user(update.effective_user.id)
#     week_start = iso_week_monday(today)
#     conn = db()
#     cur = conn.cursor()
#     cur.execute(
#         "SELECT user_id, points FROM leaderboard WHERE week_start=? ORDER BY points DESC LIMIT 10",
#         (week_start.isoformat(),)
#     )
#     rows = cur.fetchall()
#
#     if not rows:
#         conn.close()
#         await update.message.reply_text("Пока нет очков за эту неделю. Выполняйте задачи!")
#         return
#
#     lines = ["🏆 Таблица лидеров (эта неделя):"]
#     for i, r in enumerate(rows, start=1):
#         uid, pts = r[0], r[1]
#         cur.execute("SELECT username FROM users WHERE user_id=?", (uid,))
#         ur = cur.fetchone()
#         uname = ur[0] if ur and ur[0] else str(uid)
#         lines.append(f"{i}. @{uname} — {pts} монет")
#
#     conn.close()
#     await update.message.reply_text("\n".join(lines))

async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today = today_for_user(update.effective_user)
    week_start = iso_week_monday(today)

    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, points FROM leaderboard WHERE week_start=? ORDER BY points DESC",
        (week_start.isoformat(),)
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Пока нет очков за эту неделю. Выполняй задачи, чтобы попасть в рейтинг 💪")
        return

    # Считаем место пользователя
    total = len(rows)
    rank = next((i + 1 for i, (uid, _) in enumerate(rows) if uid == user_id), None)
    user_points = next((pts for uid, pts in rows if uid == user_id), 0)

    if rank:
        await update.message.reply_text(
            f"🏆 Ты на {rank}-м месте из {total} участников этой недели!\n"
            f"💪 У тебя {user_points} очков."
        )
    else:
        await update.message.reply_text(
            "Ты ещё не попал в таблицу лидеров. Делай задачи и зарабатывай очки 💥"
        )


# =====================
# Weekly bonus job
# =====================
async def weekly_bonus_and_summary(app: Application):
    """
    Runs every Monday 00:01 Prague time:
    - For previous week, calculate for each user and each task if all scheduled days were completed; if yes, add +30 points.
    - Send personal summary for previous week.
    """
    # We are on Monday, so previous week = today - 7 days
    today = today_for_user(update.effective_user.id)
    prev_week_start = iso_week_monday(today - timedelta(days=7))
    prev_week_end = prev_week_start + timedelta(days=6)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, chat_id, COALESCE(username,'') FROM users")
    users = cur.fetchall()

    for u in users:
        uid, chat_id, uname = u[0], u[1], u[2]
        # Load tasks
        cur.execute("SELECT id, name, days FROM tasks WHERE user_id=?", (uid,))
        trows = cur.fetchall()
        if not trows:
            continue

        lines = [f"📅 Итоги недели ({prev_week_start.isoformat()} — {prev_week_end.isoformat()}):"]
        bonus_total = 0
        base_points = 0

        for t in trows:
            tid, name, days_csv = t[0], t[1], t[2]
            days = set(int(x) for x in days_csv.split(",")) if days_csv else set()
            # Which dates were scheduled last week?
            sched_dates = [
                (prev_week_start + timedelta(days=i)).isoformat()
                for i in range(7) if (prev_week_start + timedelta(days=i)).isoweekday() in days
            ]
            cur.execute(
                "SELECT done_date FROM completions WHERE task_id=? AND done_date BETWEEN ? AND ?",
                (tid, prev_week_start.isoformat(), prev_week_end.isoformat())
            )
            done_dates = {r[0] for r in cur.fetchall()}
            done_count = len(done_dates)
            sched_count = len(sched_dates)
            base = done_count * 10
            base_points += base

            bonus = 30 if sched_count > 0 and done_count == sched_count else 0
            bonus_total += bonus

            status = f"{done_count}/{sched_count} → {base} монет"
            if bonus:
                status += " + 🎁 30 бонус"
            lines.append(f"• {name}: {status}")

        # Update leaderboard with bonuses
        if bonus_total:
            upsert_leaderboard_points(uid, prev_week_start, bonus_total)

        cur.execute("SELECT points FROM leaderboard WHERE user_id=? AND week_start=?", (uid, prev_week_start.isoformat()))
        row = cur.fetchone()
        total_points = row[0] if row else base_points + bonus_total
        lines.append("")
        lines.append(f"💰 Итого за неделю: {total_points} монет")

        try:
            await app.bot.send_message(chat_id=chat_id, text="\n".join(lines))
        except Exception as e:
            logger.warning(f"Failed to send weekly summary to {uid}: {e}")

    conn.close()


# =====================
# Reminders
# =====================
async def send_task_reminder(app: Application, user_id: int, task_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM users WHERE user_id=?", (user_id,))
    urow = cur.fetchone()
    if not urow:
        conn.close()
        return
    chat_id = urow[0]
    cur.execute("SELECT name, time_str, days FROM tasks WHERE id=?", (task_id,))
    trow = cur.fetchone()
    conn.close()
    if not trow:
        return

    name, time_str, days_csv = trow[0], trow[1], trow[2]
    today = today_for_user(update.effective_user.id)
    if str(today.isoweekday()) not in (days_csv or "").split(","):
        return  # Not scheduled today

    btn = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Отметить выполнено", callback_data=f"done:{task_id}")]])
    try:
        await app.bot.send_message(chat_id=chat_id, text=f"🔔 Напоминание: {name} в {time_str}", reply_markup=btn)
    except Exception as e:
        logger.warning(f"Reminder send failed: {e}")


async def schedule_all_user_tasks(bot_app: Application, user_id: int | None = None):
    """Schedule reminder jobs for all tasks (or for a single user's tasks). Uses Application.job_queue."""
    jq = bot_app.job_queue
    conn = db()
    cur = conn.cursor()
    if user_id:
        cur.execute("SELECT id, user_id, time_str FROM tasks WHERE user_id=?", (user_id,))
    else:
        cur.execute("SELECT id, user_id, time_str FROM tasks")
    tasks_rows = cur.fetchall()
    conn.close()

    for t in tasks_rows:
        tid, uid, time_str = t[0], t[1], t[2]
        hh, mm = map(int, time_str.split(":"))
        # Schedule daily at HH:MM Europe/Prague; we'll check inside if today is scheduled
        jq.run_daily(
            lambda ctx: asyncio.create_task(send_task_reminder(bot_app, uid, tid)),
            time=time(hh, mm, tzinfo=TZ),
            name=f"reminder_{tid}",
            days=(0, 1, 2, 3, 4, 5, 6),
            job_kwargs={"misfire_grace_time": 300}
        )


# =====================
# Commands
# =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update)
    msg = (
        "Привет! Я бот-трекер продуктивности 💪\n\n"
        "📋 Основные команды:\n"
        "/newtask — создать новую задачу с расписанием\n"
        "/mytasks — посмотреть и отметить выполненные задачи\n"
        "/delete — удалить задачу\n"
        "/progress — прогресс за неделю\n"
        "/leaderboard — таблица лидеров\n"
        "/settimezone — выбрать свой часовой пояс (Прага, Баку, Москва)\n"
        "/help — показать это сообщение\n\n"
        "💰 За каждое выполненное по расписанию — 10 монет.\n"
        "🎁 Если всю неделю выполняешь все запланированные дни по задаче — +30 бонусных монет.\n"
        "👑 Соревнуйся с друзьями через /leaderboard!"
    )
    await update.message.reply_text(msg)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# =====================
# Main
# =====================

# =====================
# Удаление задач
# =====================

async def delete_task_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT id, name, time_str FROM tasks WHERE user_id=?", (u.id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("У тебя нет задач для удаления.")
        return

    keyboard = [
        [InlineKeyboardButton(f"🗑 {r[1]} ({r[2]})", callback_data=f"delete:{r[0]}")]
        for r in rows
    ]
    await update.message.reply_text(
        "Выбери задачу, которую хочешь удалить:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def delete_task_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    task_id = int(q.data.split(":")[1])

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()
    conn.close()

    await q.edit_message_text("✅ Задача успешно удалена!")

    ##

from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo

def schedule_task_jobs(app, user_id: int, name: str, time_str: str, days_csv: str):
    """Создаёт 3 ежедневных джоба по задаче: за 10 мин, в момент старта и через час.
       Время считается в ЧП пользователя."""
    tz = get_user_tz(user_id)
    days = [int(d) for d in days_csv.split(",")]
    h, m = map(int, time_str.split(":"))

    base_local = time(hour=h, minute=m, tzinfo=tz)

    def shift(t: time, delta: timedelta) -> time:
        dt = datetime.combine(date.today(), t)
        return (dt + delta).timetz()  # time с tzinfo

    jq = app.job_queue

    # ⏰ В момент начала
    jq.run_daily(
        lambda ctx, uid=user_id, n=name: ctx.bot.send_message(uid, f"⏰ Время выполнить задачу: {n}! 💪"),
        time=base_local,
        days=days,
        name=f"task_start_{user_id}_{name}"
    )

    # ⚠️ За 10 минут до начала
    jq.run_daily(
        lambda ctx, uid=user_id, n=name: ctx.bot.send_message(uid, f"⚠️ Через 10 минут начинай: {n}!"),
        time=shift(base_local, timedelta(minutes=-10)),
        days=days,
        name=f"task_early_{user_id}_{name}"
    )

    # ✅ Через 1 час после начала
    jq.run_daily(
        lambda ctx, uid=user_id, n=name: ctx.bot.send_message(uid, f"✅ {n} закончилась! Выполнил? Напиши /done"),
        time=shift(base_local, timedelta(hours=1)),
        days=days,
        name=f"task_check_{user_id}_{name}"
    )

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    # Conversation for /newtask
    conv = ConversationHandler(
        entry_points=[CommandHandler("newtask", newtask_start)],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, newtask_name)],
            ASK_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, newtask_time)],
            ASK_DAYS: [CallbackQueryHandler(newtask_days_toggle, pattern=r"^toggle_day:\d+$"),
                       CallbackQueryHandler(newtask_days_done, pattern=r"^days_done$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True,
    )

    app.add_handler(conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("mytasks", mytasks))
    app.add_handler(CommandHandler("progress", progress))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))

    app.add_handler(CommandHandler("delete", delete_task_menu))
    app.add_handler(CallbackQueryHandler(delete_task_cb, pattern=r"^delete:\d+$"))
    app.add_handler(CommandHandler("settimezone", settimezone))
    app.add_handler(CallbackQueryHandler(settimezone_cb, pattern=r"^settz:.+$"))

    app.add_handler(CallbackQueryHandler(mark_done_cb, pattern=r"^done:\d+$"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern=r"^noop$"))

    # Schedule weekly bonus job: every Monday 00:01 Prague
    app.job_queue.run_daily(
        lambda ctx: asyncio.create_task(weekly_bonus_and_summary(app)),
        time=time(0, 1, tzinfo=TZ),
        days=(0,),  # Monday
        name="weekly_bonus"
    )

    # Load reminder jobs for all tasks
    async def on_startup(app):
        await schedule_all_user_tasks(app)

    app.post_init = on_startup

    logger.info("Bot starting with long polling...")

    # --- Перезапуск задач из базы при старте ---
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, name, time_str, days FROM tasks")
    tasks = cur.fetchall()
    conn.close()

    for user_id, name, time_str, days_csv in tasks:
        try:
            schedule_task_jobs(app, user_id, name, time_str, days_csv)
            logger.info(f"🔁 Восстановлены напоминания для '{name}' (user {user_id})")
        except Exception as e:
            logger.error(f"Ошибка при восстановлении задачи {name}: {e}")

    app.run_polling()


if __name__ == "__main__":
    main()
