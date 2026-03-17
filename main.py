import asyncio
import base64
import json
import logging
import os
import tempfile
from typing import Optional, Dict, Any

import httpx
from openai import OpenAI
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
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
    with open(DB_FILE, "r") as f:
        return json.load(f)


def save_db(db):
    with open(DB_FILE, "w") as f:
        json.dump(db, f)


def get_user(user_id):
    db = load_db()
    uid = str(user_id)

    if uid not in db["users"]:
        db["users"][uid] = {
            "paid": 0,
        }

    save_db(db)
    return db["users"][uid]


def add_generations(user_id, amount):
    db = load_db()
    user = get_user(user_id)
    user["paid"] += amount
    db["users"][str(user_id)] = user
    save_db(db)


def use_generation(user_id):
    db = load_db()
    user = get_user(user_id)

    if user["paid"] <= 0:
        return False

    user["paid"] -= 1
    db["users"][str(user_id)] = user
    save_db(db)
    return True


# --------------------------------
# UTILS
# --------------------------------
IMAGE_TRIGGERS = [
    "сгенерируй",
    "создай картинку",
    "нарисуй",
    "generate image",
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
        messages=[
            {"role": "user", "content": text},
        ],
    )
    return r.choices[0].message.content


def gen_image(prompt):
    r = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size="1024x1024",
    )

    img = extract_image(r)
    return base64.b64decode(img)


# --------------------------------
# КОМАНДЫ
# --------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет!\n\n"
        "Я умею:\n"
        "- генерировать текст\n"
        "- генерировать изображения\n\n"
        "⚠️ Для изображений нужна оплата\n"
        "/popolnit — купить генерации"
    )


async def popolnit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("300 ⭐ — 10 генераций", callback_data="buy_10")],
        [InlineKeyboardButton("800 ⭐ — 25 генераций", callback_data="buy_25")],
    ]

    await update.message.reply_text(
        "Привет, у тебя нет генераций.\n\n"
        "Купи пакет ниже👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# --------------------------------
# ОПЛАТА
# --------------------------------
async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "buy_10":
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
        provider_token="",  # ВАЖНО
        currency="XTR",
        prices=[LabeledPrice(title, price)],
    )


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    payload = payment.invoice_payload
    user_id = update.effective_user.id

    if payload == "buy_10":
        add_generations(user_id, 10)
        text = "✅ Начислено 10 генераций"
    else:
        add_generations(user_id, 25)
        text = "✅ Начислено 25 генераций"

    await update.message.reply_text(text)


# --------------------------------
# ОБРАБОТКА
# --------------------------------
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if is_image(text):
        user = get_user(user_id)

        if user["paid"] <= 0:
            await update.message.reply_text(
                "❌ У тебя нет генераций\n"
                "/popolnit — купить"
            )
            return

        await send_typing(update, ChatAction.UPLOAD_PHOTO)

        try:
            img = await asyncio.to_thread(gen_image, text)

            use_generation(user_id)

            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
                f.write(img)
                path = f.name

            await update.message.reply_photo(photo=open(path, "rb"))
            os.remove(path)

        except Exception as e:
            logger.exception(e)
            await update.message.reply_text("Ошибка генерации")

    else:
        await send_typing(update)

        try:
            ans = await asyncio.to_thread(gen_text, text)
            await update.message.reply_text(ans)
        except Exception:
            await update.message.reply_text("Ошибка")


# --------------------------------
# MAIN
# --------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("popolnit", popolnit))

    app.add_handler(CallbackQueryHandler(buy_callback))

    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    app.run_polling()


if __name__ == "__main__":
    main()
