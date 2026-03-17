import asyncio
import base64
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from openai import OpenAI
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    BotCommand,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    filters,
)

# --------------------------------
# ЛОГИ
# --------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------------------
# ENV
# --------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
AITUNNEL_API_KEY = os.getenv("AITUNNEL_API_KEY")
AITUNNEL_BASE_URL = os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1")

TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-4o-mini")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "300"))

# Для Render лучше использовать Persistent Disk:
# например DB_PATH=/var/data/bot.db
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

if not AITUNNEL_API_KEY:
    raise RuntimeError("AITUNNEL_API_KEY missing")

# --------------------------------
# ЛИМИТЫ
# --------------------------------
DAILY_TEXT_LIMIT = 20
MSK = timezone(timedelta(hours=3))

# --------------------------------
# OPENAI CLIENT
# --------------------------------
client = OpenAI(
    api_key=AITUNNEL_API_KEY,
    base_url=AITUNNEL_BASE_URL,
    http_client=httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT)),
)

# --------------------------------
# SQLITE
# --------------------------------
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            paid_generations INTEGER NOT NULL DEFAULT 0,
            daily_text_count INTEGER NOT NULL DEFAULT 0,
            last_reset TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            telegram_payment_charge_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            payload TEXT NOT NULL,
            amount INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


def today_msk_str() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def ensure_user(user_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()

    if row is None:
        cur.execute(
            """
            INSERT INTO users (user_id, paid_generations, daily_text_count, last_reset)
            VALUES (?, 0, 0, ?)
            """,
            (user_id, today_msk_str()),
        )
        conn.commit()

    conn.close()


def reset_daily_if_needed(user_id: int) -> None:
    ensure_user(user_id)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT daily_text_count, last_reset FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()

    today = today_msk_str()
    if row and row["last_reset"] != today:
        cur.execute(
            """
            UPDATE users
            SET daily_text_count = 0, last_reset = ?
            WHERE user_id = ?
            """,
            (today, user_id),
        )
        conn.commit()

    conn.close()


def get_balance(user_id: int) -> int:
    ensure_user(user_id)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT paid_generations FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()

    balance = int(row["paid_generations"]) if row else 0
    logger.info("get_balance user_id=%s balance=%s", user_id, balance)
    return balance


def add_generations(user_id: int, amount: int) -> int:
    ensure_user(user_id)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE users
        SET paid_generations = paid_generations + ?
        WHERE user_id = ?
        """,
        (amount, user_id),
    )
    conn.commit()

    cur.execute(
        "SELECT paid_generations FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()

    new_balance = int(row["paid_generations"]) if row else 0
    logger.info(
        "add_generations user_id=%s amount=%s new_balance=%s",
        user_id,
        amount,
        new_balance,
    )
    return new_balance


def consume_generation(user_id: int) -> bool:
    ensure_user(user_id)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "SELECT paid_generations FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()

    current = int(row["paid_generations"]) if row else 0
    logger.info("consume_generation user_id=%s current=%s", user_id, current)

    if current <= 0:
        conn.close()
        return False

    cur.execute(
        """
        UPDATE users
        SET paid_generations = paid_generations - 1
        WHERE user_id = ?
        """,
        (user_id,),
    )
    conn.commit()
    conn.close()
    return True


def get_daily_text_left(user_id: int) -> int:
    reset_daily_if_needed(user_id)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT daily_text_count FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()

    used = int(row["daily_text_count"]) if row else 0
    return max(0, DAILY_TEXT_LIMIT - used)


def can_send_text(user_id: int) -> bool:
    return get_daily_text_left(user_id) > 0


def increment_text_count(user_id: int) -> int:
    reset_daily_if_needed(user_id)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE users
        SET daily_text_count = daily_text_count + 1
        WHERE user_id = ?
        """,
        (user_id,),
    )
    conn.commit()

    cur.execute(
        "SELECT daily_text_count FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()

    return int(row["daily_text_count"]) if row else 0


def payment_already_processed(telegram_payment_charge_id: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT telegram_payment_charge_id
        FROM payments
        WHERE telegram_payment_charge_id = ?
        """,
        (telegram_payment_charge_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def save_payment(
    telegram_payment_charge_id: str,
    user_id: int,
    payload: str,
    amount: int,
) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO payments
        (telegram_payment_charge_id, user_id, payload, amount, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            telegram_payment_charge_id,
            user_id,
            payload,
            amount,
            datetime.now(MSK).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


# --------------------------------
# ТРИГГЕРЫ
# --------------------------------
IMAGE_TRIGGERS = [
    "сгенерируй",
    "создай картинку",
    "создай изображение",
    "нарисуй",
    "сделай фото",
    "сделай картинку",
    "generate image",
    "draw",
    "create image",
    "make image",
]

TOPUP_TEXT_TRIGGERS = [
    "пополнить",
    "купить",
    "купить генерации",
    "тариф",
    "тарифы",
]


def is_image_request(text: str) -> bool:
    text = text.lower().strip()
    return any(trigger in text for trigger in IMAGE_TRIGGERS)


def is_topup_text_request(text: str) -> bool:
    text = text.lower().strip()
    return text in TOPUP_TEXT_TRIGGERS


async def send_typing(update: Update, action: str = ChatAction.TYPING) -> None:
    if update.effective_chat:
        await update.effective_chat.send_action(action=action)


def extract_image_b64(response) -> Optional[str]:
    data = getattr(response, "data", None)
    if data and len(data) > 0:
        item = data[0]
        b64_json = getattr(item, "b64_json", None)
        if b64_json:
            return b64_json

    output = getattr(response, "output", None)
    if output:
        try:
            for out_item in output:
                content = getattr(out_item, "content", None) or []
                for c in content:
                    image_base64 = getattr(c, "image_base64", None)
                    if image_base64:
                        return image_base64
        except Exception:
            logger.exception("Ошибка при разборе изображения из API")

    return None


def build_buy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("150 ⭐ — 4 генерации", callback_data="buy_4")],
            [InlineKeyboardButton("300 ⭐ — 10 генераций", callback_data="buy_10")],
            [InlineKeyboardButton("800 ⭐ — 25 генераций", callback_data="buy_25")],
        ]
    )


# --------------------------------
# OPENAI
# --------------------------------
def generate_text_blocking(user_text: str) -> str:
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {
                "role": "system",
                "content": "Ты полезный Telegram-бот. Отвечай кратко, ясно и по делу.",
            },
            {"role": "user", "content": user_text},
        ],
    )

    answer = response.choices[0].message.content
    if not answer:
        return "Не удалось получить ответ."
    return answer.strip()


def generate_image_blocking(prompt: str) -> bytes:
    response = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size="1024x1024",
    )

    image_b64 = extract_image_b64(response)
    if not image_b64:
        raise RuntimeError("API не вернул изображение")

    return base64.b64decode(image_b64)


# --------------------------------
# КОМАНДЫ В МЕНЮ
# --------------------------------
async def post_init(application: Application) -> None:
    init_db()

    await application.bot.set_my_commands(
        [
            BotCommand("start", "Запуск бота"),
            BotCommand("popolnit", "Купить генерации"),
            BotCommand("balance", "Мой баланс"),
            BotCommand("limit", "Лимит на сегодня"),
            BotCommand("stars", "Баланс звёзд бота"),
            BotCommand("help", "Помощь"),
        ]
    )
    logger.info("Команды бота установлены")


# --------------------------------
# UI
# --------------------------------
async def send_topup_menu(update: Update, balance: int) -> None:
    text = (
        f"На вашем балансе сейчас {balance} генераций.\n\n"
        "Выберите пакет ниже 👇"
    )
    await update.message.reply_text(
        text,
        reply_markup=build_buy_keyboard(),
    )


# --------------------------------
# КОМАНДЫ
# --------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    ensure_user(user_id)

    balance = get_balance(user_id)
    daily_left = get_daily_text_left(user_id)

    text = (
        "Привет!\n\n"
        "Я умею:\n"
        "- отвечать на текстовые вопросы\n"
        "- генерировать изображения\n\n"
        "Для генерации изображений нужны генерации на балансе.\n"
        f"Сейчас у вас: {balance} генераций.\n"
        f"Осталось текстовых запросов сегодня: {daily_left}/{DAILY_TEXT_LIMIT}\n\n"
        "Команды:\n"
        "/popolnit — купить генерации\n"
        "/balance — показать баланс генераций\n"
        "/limit — показать дневной лимит\n""/stars — показать баланс звёзд бота\n"
        "/help — помощь"
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    ensure_user(user_id)

    balance = get_balance(user_id)
    daily_left = get_daily_text_left(user_id)

    text = (
        "Команды:\n"
        "/start — запуск\n"
        "/help — помощь\n"
        "/balance — показать баланс генераций\n"
        "/limit — показать лимит на сегодня\n"
        "/popolnit — купить генерации\n"
        "/stars — баланс звёзд бота\n\n"
        f"Сейчас на балансе: {balance} генераций.\n"
        f"Осталось текстовых запросов сегодня: {daily_left}/{DAILY_TEXT_LIMIT}\n\n"
        "Пример для картинки:\n"
        '"Сгенерируй картинку города будущего ночью"'
    )
    await update.message.reply_text(text)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    balance = get_balance(update.effective_user.id)
    await update.message.reply_text(
        f"На вашем балансе {balance} генераций."
    )


async def limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    daily_left = get_daily_text_left(user_id)
    await update.message.reply_text(
        "Лимит текстовых запросов обновляется каждый день по МСК.\n"
        f"Сегодня осталось: {daily_left}/{DAILY_TEXT_LIMIT}."
    )


async def popolnit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    balance = get_balance(update.effective_user.id)
    await send_topup_menu(update, balance)


async def stars_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        balance = await context.bot.get_my_star_balance()
        amount = getattr(balance, "amount", None)
        if amount is None:
            await update.message.reply_text("Не удалось прочитать баланс звёзд бота.")
            return

        await update.message.reply_text(f"Баланс звёзд бота: {amount} ⭐")
    except Exception as e:
        logger.exception("Не удалось получить баланс звёзд: %s", e)
        await update.message.reply_text("Не удалось получить баланс звёзд бота.")


# --------------------------------
# ОПЛАТА
# --------------------------------
async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "buy_4":
        title = "4 генерации"
        payload = "buy_4"
        price = 150
        description = "Покупка 4 генераций изображений"
    elif query.data == "buy_10":
        title = "10 генераций"
        payload = "buy_10"
        price = 300
        description = "Покупка 10 генераций изображений"
    elif query.data == "buy_25":
        title = "25 генераций"
        payload = "buy_25"
        price = 800
        description = "Покупка 25 генераций изображений"
    else:
        await query.message.reply_text("Неизвестный пакет.")
        return

    logger.info(
        "Invoice requested: user_id=%s payload=%s price=%s",
        update.effective_user.id if update.effective_user else "unknown",
        payload,
        price,
    )

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(title, price)],
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query

    logger.info(
        "PreCheckout received: user_id=%s payload=%s total_amount=%s currency=%s",
        query.from_user.id,
        query.invoice_payload,
        query.total_amount,
        query.currency,
    )

    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    payload = payment.invoice_payload
    charge_id = payment.telegram_payment_charge_id

    logger.info(
        "Successful payment: user_id=%s payload=%s total_amount=%s currency=%s charge_id=%s",
        user_id,
        payload,
        payment.total_amount,
        payment.currency,
        charge_id,
    )

    if payment_already_processed(charge_id):
        balance = get_balance(user_id)
        await update.message.reply_text(
            f"Платеж уже был обработан ранее. На вашем балансе {balance} генераций."
        )
        return

    if payload == "buy_4":
        amount = 4
    elif payload == "buy_10":
        amount = 10
    elif payload == "buy_25":
        amount = 25
    else:
        amount = 0

    save_payment(charge_id, user_id, payload, payment.total_amount)

    if amount > 0:
        new_balance = add_generations(user_id, amount)
    else:
        new_balance = get_balance(user_id)

    await update.message.reply_text(
        f"Платеж успешно получен! На вашем балансе {new_balance} генераций."
    )


# --------------------------------
# ОБРАБОТКА ЗАПРОСОВ
# --------------------------------
async def handle_image_request(update: Update, user_text: str) -> None:
    user_id = update.effective_user.id
    ensure_user(user_id)

    balance = get_balance(user_id)

    if balance <= 0:
        await update.message.reply_text(
            "У вас нет доступных генераций.\nИспользуйте /popolnit."
        )
        return

    await send_typing(update, ChatAction.UPLOAD_PHOTO)

    try:
        image_bytes = await asyncio.to_thread(generate_image_blocking, user_text)

        ok = consume_generation(user_id)
        if not ok:
            await update.message.reply_text("Не удалось списать генерацию.")
            return

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
            f.write(image_bytes)
            temp_path = f.name

        with open(temp_path, "rb") as photo:
            await update.message.reply_photo(photo=photo)

        os.remove(temp_path)

        new_balance = get_balance(user_id)
        await update.message.reply_text(
            f"Готово. Остаток на балансе: {new_balance} генераций."
        )

    except Exception as e:
        logger.exception("Image generation failed: %s", e)
        await update.message.reply_text(
            "Не удалось сгенерировать изображение. Попробуйте еще раз."
        )


async def handle_text_request(update: Update, user_text: str) -> None:
    user_id = update.effective_user.id
    ensure_user(user_id)

    if not can_send_text(user_id):
        await update.message.reply_text(
            "Лимит текстовых запросов на сегодня исчерпан.\n"
            "Попробуйте завтра после 00:00 по МСК."
        )
        return

    await send_typing(update, ChatAction.TYPING)

    try:
        answer = await asyncio.to_thread(generate_text_blocking, user_text)
        increment_text_count(user_id)

        max_len = 4000
        for i in range(0, len(answer), max_len):
            await update.message.reply_text(answer[i:i + max_len])

        left = get_daily_text_left(user_id)
        await update.message.reply_text(
            f"Осталось текстовых запросов сегодня: {left}/{DAILY_TEXT_LIMIT}."
        )

    except Exception as e:
        logger.exception("Text generation failed: %s", e)
        await update.message.reply_text("Не удалось получить ответ.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    ensure_user(user_id)

    user_text = update.message.text.strip()
    if not user_text:
        return

    logger.info(
        "Message from user_id=%s text=%s",
        update.effective_user.id if update.effective_user else "unknown",
        user_text[:200],
    )

    if is_topup_text_request(user_text):
        balance = get_balance(user_id)
        await send_topup_menu(update, balance)
        return

    if is_image_request(user_text):await handle_image_request(update, user_text)
        return

    await handle_text_request(update, user_text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)


# --------------------------------
# MAIN
# --------------------------------
def main() -> None:
    logger.info("Building Telegram application")

    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("limit", limit_command))
    app.add_handler(CommandHandler("popolnit", popolnit_command))
    app.add_handler(CommandHandler("stars", stars_command))

    app.add_handler(CallbackQueryHandler(buy_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    logger.info("Bot is starting polling")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
