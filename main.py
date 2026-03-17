import asyncio
import base64
import json
import logging
import os
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

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

if not AITUNNEL_API_KEY:
    raise RuntimeError("AITUNNEL_API_KEY missing")

# --------------------------------
# ЛИМИТЫ
# --------------------------------
# Сколько текстовых сообщений в день может отправить пользователь
DAILY_TEXT_LIMIT = 20

# Московское время (UTC+3)
MSK = timezone(timedelta(hours=3))

# --------------------------------
# CLIENT
# --------------------------------
client = OpenAI(
    api_key=AITUNNEL_API_KEY,
    base_url=AITUNNEL_BASE_URL,
    http_client=httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT)),
)

# --------------------------------
# БАЗА
# --------------------------------
DB_FILE = "users.json"


def load_db():
    if not os.path.exists(DB_FILE):
        return {"users": {}}

    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("Не удалось прочитать users.json")
        return {"users": {}}


def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def today_msk_str() -> str:
    return datetime.now(MSK).strftime("%Y-%m-%d")


def ensure_user(user_id: int):
    db = load_db()
    uid = str(user_id)

    if uid not in db["users"]:
        db["users"][uid] = {
            "paid_generations": 0,
            "daily_text_count": 0,
            "last_reset": today_msk_str(),
        }
        save_db(db)

    return db["users"][uid]


def reset_daily_if_needed(user_id: int):
    db = load_db()
    uid = str(user_id)

    if uid not in db["users"]:
        db["users"][uid] = {
            "paid_generations": 0,
            "daily_text_count": 0,
            "last_reset": today_msk_str(),
        }
        save_db(db)
        return db["users"][uid]

    today = today_msk_str()
    user = db["users"][uid]

    if user.get("last_reset") != today:
        user["daily_text_count"] = 0
        user["last_reset"] = today
        db["users"][uid] = user
        save_db(db)

    return user


def get_balance(user_id: int) -> int:
    db = load_db()
    uid = str(user_id)

    if uid not in db["users"]:
        db["users"][uid] = {
            "paid_generations": 0,
            "daily_text_count": 0,
            "last_reset": today_msk_str(),
        }
        save_db(db)

    return int(db["users"][uid].get("paid_generations", 0))


def add_generations(user_id: int, amount: int) -> int:
    db = load_db()
    uid = str(user_id)

    if uid not in db["users"]:
        db["users"][uid] = {
            "paid_generations": 0,
            "daily_text_count": 0,
            "last_reset": today_msk_str(),
        }

    db["users"][uid]["paid_generations"] += amount
    save_db(db)
    return int(db["users"][uid]["paid_generations"])


def consume_generation(user_id: int) -> bool:
    db = load_db()
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {
            "paid_generations": 0,
            "daily_text_count": 0,
            "last_reset": today_msk_str(),
        }

    current = int(db["users"][uid].get("paid_generations", 0))
    if current <= 0:
        save_db(db)
        return False

    db["users"][uid]["paid_generations"] = current - 1
    save_db(db)
    return True


def get_daily_text_left(user_id: int) -> int:
    user = reset_daily_if_needed(user_id)
    used = int(user.get("daily_text_count", 0))
    left = DAILY_TEXT_LIMIT - used
    return max(0, left)


def can_send_text(user_id: int) -> bool:
    return get_daily_text_left(user_id) > 0


def increment_text_count(user_id: int) -> int:
    db = load_db()
    uid = str(user_id)

    if uid not in db["users"]:
        db["users"][uid] = {
            "paid_generations": 0,
            "daily_text_count": 0,
            "last_reset": today_msk_str(),
        }

    # Перед увеличением проверяем, не наступил ли новый день по МСК
    today = today_msk_str()
    if db["users"][uid].get("last_reset") != today:
        db["users"][uid]["daily_text_count"] = 0
        db["users"][uid]["last_reset"] = today

    db["users"][uid]["daily_text_count"] += 1
    save_db(db)
    return int(db["users"][uid]["daily_text_count"])


# --------------------------------
# УТИЛИТЫ
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


async def send_typing(update: Update, action=ChatAction.TYPING):
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
            logger.exception("Ошибка при разборе ответа изображения")

    return None


def build_buy_keyboard():
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
                "content": (
                    "Ты полезный Telegram-бот. "
                    "Отвечай кратко, ясно и по делу."
                ),
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
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Запуск бота"),
            BotCommand("popolnit", "Купить генерации"),
            BotCommand("balance", "Мой баланс"),
            BotCommand("limit", "Лимит на сегодня"),
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
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
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
        "/limit — показать дневной лимит\n"
        "/help — помощь"
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = get_balance(user_id)
    daily_left = get_daily_text_left(user_id)

    text = (
        "Команды:\n"
        "/start — запуск\n"
        "/help — помощь\n"
        "/balance — показать баланс генераций\n"
        "/limit — показать лимит на сегодня\n"
        "/popolnit — купить генерации\n\n"
        f"Сейчас на балансе: {balance} генераций.\n"
        f"Осталось текстовых запросов сегодня: {daily_left}/{DAILY_TEXT_LIMIT}\n\n"
        "Пример для картинки:\n"
        '"Сгенерируй картинку города будущего ночью"'
    )
    await update.message.reply_text(text)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balance = get_balance(update.effective_user.id)
    await update.message.reply_text(
        f"На вашем балансе {balance} генераций."
    )


async def limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    daily_left = get_daily_text_left(user_id)
    await update.message.reply_text(
        "Лимит текстовых запросов обновляется каждый день по МСК.\n"
        f"Сегодня осталось: {daily_left}/{DAILY_TEXT_LIMIT}."
    )


async def popolnit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balance = get_balance(update.effective_user.id)
    await send_topup_menu(update, balance)


# --------------------------------
# ОПЛАТА
# --------------------------------
async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    logger.info(
        "PreCheckout received: user_id=%s payload=%s total_amount=%s currency=%s",
        query.from_user.id,
        query.invoice_payload,
        query.total_amount,
        query.currency,
    )

    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    payload = payment.invoice_payload

    logger.info(
        "Successful payment: user_id=%s payload=%s total_amount=%s currency=%s",
        user_id,
        payload,
        payment.total_amount,
        payment.currency,
    )

    if payload == "buy_4":
        new_balance = add_generations(user_id, 4)
    elif payload == "buy_10":
        new_balance = add_generations(user_id, 10)
    elif payload == "buy_25":
        new_balance = add_generations(user_id, 25)
    else:
        new_balance = get_balance(user_id)

    await update.message.reply_text(
        f"Платеж успешно получен! На вашем балансе {new_balance} генераций."
    )


# --------------------------------
# ОБРАБОТКА ЗАПРОСОВ
# --------------------------------
async def handle_image_request(update: Update, user_text: str):
    user_id = update.effective_user.id
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


async def handle_text_request(update: Update, user_text: str):
    user_id = update.effective_user.id

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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
            user_text = update.message.text.strip()
    if not user_text:
        return

    logger.info(
        "Message from user_id=%s text=%s",
        update.effective_user.id if update.effective_user else "unknown",
        user_text[:200],
    )

    if is_topup_text_request(user_text):
        balance = get_balance(update.effective_user.id)
        await send_topup_menu(update, balance)
        return

    if is_image_request(user_text):
        await handle_image_request(update, user_text)
        return

    await handle_text_request(update, user_text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error: %s", context.error)


# --------------------------------
# MAIN
# --------------------------------
def main():
    logger.info("Building Telegram application")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("limit", limit_command))
    app.add_handler(CommandHandler("popolnit", popolnit_command))

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
