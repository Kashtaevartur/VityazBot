import requests
import sqlite3
from datetime import datetime, timedelta
from config import BOT_TOKEN, COOKIE, CSRF_TOKEN

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# --- БД ---
conn = sqlite3.connect("reservations.db", check_same_thread=False)
cursor = conn.cursor()


def log_user(update: Update):
    try:
        user = update.effective_user
        if not user:
            return

        cursor.execute("""
        INSERT INTO users (telegram_id, username, first_name, last_name, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_seen=excluded.last_seen
        """, (
            user.id,
            user.username,
            user.first_name,
            user.last_name,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

        conn.commit()

    except Exception as e:
        print("Ошибка логирования пользователя:", e)


cursor.execute("""
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT,
    phone TEXT UNIQUE,
    date TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    last_seen TEXT
)
""")

conn.commit()

# --- состояния ---
CHECK_PHONE = 1
ADD_PHONE = 2
ADD_COMPANY = 3

# --- клавиатура ---
keyboard = ReplyKeyboardMarkup(
    [["Проверить бронь", "Сделать бронь"]],
    resize_keyboard=True
)

# =========================
# 🔄 ПАРСИНГ ФУНКЦИЯ
# =========================
def update_database():
    url = "https://vityaz.salesdrive.me/contacts/"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Cookie": COOKIE,
        "X-CSRF-Token": CSRF_TOKEN,
        "Accept": "application/json"
    }

    base_params = {
        "formId": 1,
        "mobileMode": 0,
        "mode": "contactList",
        "search": "",
        "per-page": 100
    }

    cutoff_date = datetime.now() - timedelta(days=4)

    page = 1
    stop_parsing = False

    while not stop_parsing:
        params = base_params.copy()
        params["page"] = page

        response = requests.get(url, headers=headers, params=params)

        if response.status_code != 200:
            print("Ошибка API")
            break

        data = response.json()
        contacts = data.get("data", [])

        if not contacts:
            break

        for contact in contacts:
            date_str = contact["createTime"]
            contact_date = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")

            if contact_date < cutoff_date:
                stop_parsing = True
                break

            for phone in contact.get("phone", []):
                phone = phone.strip()

                if not phone.isdigit() or len(phone) < 10:
                    continue

                cursor.execute("""
                INSERT OR IGNORE INTO reservations (company, phone, date)
                VALUES (?, ?, ?)
                """, ("Vityaz", phone, date_str))

        page += 1

    conn.commit()


# =========================
# 🤖 БОТ
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user(update)
    await update.message.reply_text("Выберите действие:", reply_markup=keyboard)


# --- ПРОВЕРКА ---
async def check_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user(update)
    await update.message.reply_text("Введите фрагмент номера:")
    return CHECK_PHONE


async def check_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    digits = update.message.text.strip()

    await update.message.reply_text("⏳ Обновляю базу...")
    update_database()

    cursor.execute("""
    SELECT company, phone, date 
    FROM reservations 
    WHERE phone LIKE ?
    """, ('%' + digits,))

    result = cursor.fetchone()

    if result:
        company, phone, date = result
        await update.message.reply_text(
            f"✅ Найдено:\nКомпания: {company}\nТелефон: {phone}\nДата: {date}"
        )
    else:
        await update.message.reply_text("❌ Бронь не найдена", reply_markup=keyboard)

    return ConversationHandler.END


# --- ДОБАВЛЕНИЕ ---
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user(update)
    await update.message.reply_text("Введите номер телефона:")
    return ADD_PHONE


async def add_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user(update)
    context.user_data["phone"] = update.message.text.strip()
    await update.message.reply_text("За кем закрепить бронь?")
    return ADD_COMPANY


async def add_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = context.user_data["phone"]
    company = update.message.text.strip()

    cursor.execute("""
    INSERT OR IGNORE INTO reservations (company, phone, date)
    VALUES (?, ?, ?)
    """, (company, phone, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    conn.commit()
    log_user(update)

    await update.message.reply_text("✅ Бронь сохранена!", reply_markup=keyboard)
    return ConversationHandler.END


# --- кнопки ---
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    log_user(update)

    if text == "Проверить бронь":
        return await check_start(update, context)

    elif text == "Сделать бронь":
        return await add_start(update, context)


# --- запуск ---
app = ApplicationBuilder().token(BOT_TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons)],
    states={
        CHECK_PHONE: [MessageHandler(filters.TEXT, check_phone)],
        ADD_PHONE: [MessageHandler(filters.TEXT, add_phone)],
        ADD_COMPANY: [MessageHandler(filters.TEXT, add_company)],
    },
    fallbacks=[]
)

app.add_handler(CommandHandler("start", start))
app.add_handler(conv_handler)

app.run_polling()