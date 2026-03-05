import requests
import sqlite3
import asyncio
from datetime import datetime, timedelta
from config import BOT_TOKEN, COOKIE, CSRF_TOKEN

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

ADMIN_CHAT_ID = -5239727089

# --- БД ---
def get_db():
    conn = sqlite3.connect("reservations.db", timeout=10)
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn

def log_user(update: Update):
    try:
        user = update.effective_user
        if not user:
            return

        conn = get_db()
        cursor = conn.cursor()

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
        conn.close()

    except Exception as e:
        print("Ошибка логирования пользователя:", e)


def init_db():
    for db_name in ["reservations.db", "reservations_read.db"]:
        conn = sqlite3.connect(db_name, timeout=10)
        cursor = conn.cursor()

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
        conn.close()

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
    import sqlite3
    import requests
    from datetime import datetime, timedelta

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

    cutoff_date = datetime.now() - timedelta(days=5)

    # 🔴 основная БД (запись)
    conn = sqlite3.connect("reservations.db", timeout=10)
    cursor = conn.cursor()

    page = 1
    stop_parsing = False

    try:
        while not stop_parsing:
            params = base_params.copy()
            params["page"] = page

            response = requests.get(url, headers=headers, params=params)

            if response.status_code != 200:
                print("Ошибка API:", response.status_code)
                break

            data = response.json()
            contacts = data.get("data", [])

            if not contacts:
                break

            batch = []

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

                    batch.append(("Vityaz", phone, date_str))

            if batch:
                cursor.executemany("""
                    INSERT OR IGNORE INTO reservations (company, phone, date)
                    VALUES (?, ?, ?)
                """, batch)

                conn.commit()

            page += 1

        # ✅ ДЕЛАЕМ КОПИЮ БД (ключевой момент)
        read_conn = sqlite3.connect("reservations_read.db")
        conn.backup(read_conn)
        read_conn.close()

    except Exception as e:
        print("Ошибка в update_database:", e)

    finally:
        conn.close()


# =========================
# 🤖 БОТ
# =========================
def mask_phone(phone: str) -> str:
    if not phone:
        return "N/A"

    phone = str(phone).strip()

    if len(phone) <= 4:
        return phone

    visible_digits = 4
    return "*" * (len(phone) - visible_digits) + phone[-visible_digits:]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_user(update)
    await update.message.reply_text("Выберите действие:", reply_markup=keyboard)


# --- ПРОВЕРКА ---
async def check_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message or not update.message.text:
            return ConversationHandler.END

        text = update.message.text.strip()

        # ❗ защита от нажатия кнопок вместо номера
        if text in ["Проверить бронь", "Сделать бронь"]:
            await update.message.reply_text("⚠️ Введите фрагмент номера")
            return CHECK_PHONE

        digits = text

        await update.message.reply_text("⏳ Обновляю базу...")

        # 🔥 НЕ БЛОКИРУЕМ EVENT LOOP
        import asyncio
        await asyncio.to_thread(update_database)

        # ✅ читаем из read БД
        conn = sqlite3.connect("reservations_read.db", timeout=5)
        cursor = conn.cursor()

        cursor.execute("""
        SELECT company, phone, date 
        FROM reservations 
        WHERE phone LIKE ?
        """, ('%' + digits + '%',))

        result = cursor.fetchone()
        conn.close()

        if result:
            company, phone, date = result
            masked = mask_phone(phone or "")

            await update.message.reply_text(
                f"✅ Найдено:\nКомпания: {company}\nТелефон: {masked}\nДата: {date}",
                reply_markup=keyboard
            )
        else:
            await update.message.reply_text(
                "❌ Бронь не найдена",
                reply_markup=keyboard
            )

    except Exception as e:
        print("Ошибка в check_phone:", e)
        await update.message.reply_text("⚠️ Ошибка при проверке")

    finally:
        # 🔓 снимаем блок ВСЕГДА
        context.user_data.pop("busy", None)

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
    try:
        phone = context.user_data["phone"]
        company = update.message.text.strip()

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("""
        INSERT OR IGNORE INTO reservations (company, phone, date)
        VALUES (?, ?, ?)
        """, (company, phone, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

        conn.commit()
        conn.close()

        log_user(update)

        # ✅ маскируем номер
        masked_phone = mask_phone(phone)

        user = update.effective_user
        text_to_admin = (
            f"📅 Новая бронь!\n\n"
            f"👤 Telegram: {user.full_name}\n"
            f"🆔 ID: {user.id}\n"
            f"📱 Телефон: {masked_phone}\n"
            f"🏷 На кого: {company}\n"
            f"🕒 Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        # 🔥 отправка в админ-чат
        await context.bot.send_message(
            chat_id=-5239727089,
            text=text_to_admin
        )

        await update.message.reply_text(
            "✅ Бронь сохранена!",
            reply_markup=keyboard
        )

    except Exception as e:
        print("Ошибка в add_company:", e)
        await update.message.reply_text("⚠️ Ошибка при сохранении")

    finally:
        context.user_data["busy"] = False

    return ConversationHandler.END


# --- кнопки ---
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    log_user(update)

    # 🔒 если уже в процессе — стоп
    if context.user_data.get("busy"):
        await update.message.reply_text("⏳ Подожди, предыдущий запрос ещё выполняется")
        return ConversationHandler.END

    if text == "Проверить бронь":
        context.user_data["busy"] = True  # ✅ ставим ЗДЕСЬ
        await update.message.reply_text("Введите фрагмент номера:")
        return CHECK_PHONE

    elif text == "Сделать бронь":
        context.user_data["busy"] = True  # ✅ и здесь
        return await add_start(update, context)


# --- запуск ---
app = ApplicationBuilder().token(BOT_TOKEN).build()

async def fallback_text(update, context):
    await update.message.reply_text("👇Нажми на кнопку ниже 👇")

conv_handler = ConversationHandler(
    entry_points=[
        MessageHandler(
            filters.Regex("^(Проверить бронь|Сделать бронь)$"),
            handle_buttons
        )
    ],
    states={
        CHECK_PHONE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, check_phone)
        ],
        ADD_PHONE: [
            MessageHandler(filters.TEXT, add_phone)
        ],
        ADD_COMPANY: [
            MessageHandler(filters.TEXT, add_company)
        ],
    },
    fallbacks=[
        MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text)
    ]
)

init_db()

app.add_handler(CommandHandler("start", start))
app.add_handler(conv_handler)

# самый последний!
app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text)
)

app.run_polling()
