import asyncio
import base64
import json
import logging
import os
import tempfile
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

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing")

if not AITUNNEL_API_KEY:
    raise RuntimeError("AITUNNEL_API_KEY missing")

# --------------------------------
# CLIENT
# --------------------------------
client = OpenAI(
    api_key=AITUNNEL_API_KEY,
    base_url=AITUNNEL_BASE_URL,
    http_client=httpx.Client(timeout=300),
)

# --------------------------------
# "БАЗА"
# --------------------------------
DB_FILE = "users.json"


def load_db():
    if not os.path.exists(DB_FILE):
        return {"users": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def get_user(user_id):
    db = load_db()
    uid = str(user_id)

    if uid not in db["users"]:
        db["users"][uid] = {"paid": 0}

    save_db(db)
    return db["users"][uid]


def add_generations(user_id, amount):
    db = load_db()
    uid = str(user_id)

    if uid not in db["users"]:
        db["users"][uid] = {"paid": 0}

    db["users"][uid]["paid"] += amount
    save_db(db)


def use_generation(user_id):
    db = load_db()
    uid = str(user_id)

    if uid not in db["users"]:
        db["users"][uid] = {"paid": 0}

    if db["users"][uid]["paid"] <= 0:
        save_db(db)
        return False

    db["users"][uid]["paid"] -= 1
    save_db(db)
    return True


def get_balance(user_id) -> int:
    user = get_user(user_id)
    return int(user.get("paid", 0))


# --------------------------------
# UTILS
# --------------------------------
IMAGE_TRIGGERS = [
    "сгенерируй",
    "создай картинку",
    "создай изображение",
    "нарисуй",
    "generate image",
    "draw",
    "create image",
]


def is_image(text: str):
    text = text.lower()
    return any(t in text for t in IMAGE_TRIGGERS)


async def send_typing(update: Update, action=ChatAction.TYPING):
    if update.effective_chat:
        await update.effective_chat.send_action(action=action)


def extract_image(response) -> Optional[str]:
    data = getattr(response, "data", None)
    if data and len(data) > 0:
        return getattr(data[0], "b64_json", None)
    return None


# --------------------------------
# OPENAI
# --------------------------------
def gen_text(text):
    r = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[{"role": "user", "content": text}],
    )
    return r.choices[0].message.content or "Не удалось получить ответ."


def gen_image(prompt):
    r = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size="1024x1024",
    )

    img = extract_image(r)
    if not img:
        raise RuntimeError("Изображение не получено от API")

    return base64.b64decode(img)


# --------------------------------
# КОМАНДЫ В МЕНЮ
# --------------------------------
async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Запуск бота"),BotCommand("popolnit", "Купить генерации"),
        BotCommand("balance", "Проверить баланс"),
        BotCommand("help", "Помощь"),
    ])


# --------------------------------
# КОМАНДЫ
# --------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет!\n\n"
        "Я умею:\n"
        "- генерировать текст\n"
        "- генерировать изображения\n\n"
        "Для генерации изображений сначала пополни баланс.\n"
        "/popolnit — купить генерации\n"
        "/balance — посмотреть баланс"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Команды:\n"
        "/start — запуск\n"
        "/popolnit — купить генерации\n"
        "/balance — проверить баланс\n\n"
        "Для генерации изображения просто напиши:\n"
        "«Нарисуй город будущего ночью»"
    )


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = get_balance(user_id)
    await update.message.reply_text(
        f"На вашем балансе {balance} генераций."
    )


async def popolnit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("150 ⭐ — 4 генерации", callback_data="buy_4")],
        [InlineKeyboardButton("300 ⭐ — 10 генераций", callback_data="buy_10")],
        [InlineKeyboardButton("800 ⭐ — 25 генераций", callback_data="buy_25")],
    ]

    await update.message.reply_text(
        "Привет, у тебя нет генераций.\n\n"
        "Купи пакет ниже 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


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
    elif query.data == "buy_10":
        title = "10 генераций"
        payload = "buy_10"
        price = 300
    else:
        title = "25 генераций"
        payload = "buy_25"
        price = 800

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=title,
        description="Покупка генераций изображений",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(title, price)],
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    user_id = update.effective_user.id

    if payload == "buy_4":
        amount = 4
    elif payload == "buy_10":
        amount = 10
    else:
        amount = 25

    add_generations(user_id, amount)
    balance = get_balance(user_id)

    await update.message.reply_text(
        f"Платеж успешно получен! На вашем балансе {balance} генераций."
    )


# --------------------------------
# ОБРАБОТКА СООБЩЕНИЙ
# --------------------------------
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    user_id = update.effective_user.id

    if is_image(text):
        if get_balance(user_id) <= 0:
            await update.message.reply_text(
                "❌ У тебя нет генераций.\n"
                "Используй /popolnit"
            )
            return

        await send_typing(update, ChatAction.UPLOAD_PHOTO)

        try:
            img = await asyncio.to_thread(gen_image, text)
            use_generation(user_id)

            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
                f.write(img)
                path = f.name

            with open(path, "rb") as photo:
                await update.message.reply_photo(photo=photo)

            os.remove(path)

        except Exception as e:
            logger.exception("Image generation error: %s", e)
            await update.message.reply_text("Ошибка генерации изображения.")
    else:
        await send_typing(update)

        try:
            ans = await asyncio.to_thread(gen_text, text)
            await update.message.reply_text(ans)
        except Exception as e:
            logger.exception("Text generation error: %s", e)
            await update.message.reply_text("Ошибка генерации текста.")


# --------------------------------
# MAIN
# --------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("popolnit", popolnit))

    app.add_handler(CallbackQueryHandler(buy_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
