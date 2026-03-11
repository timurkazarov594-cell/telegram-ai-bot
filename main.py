import asyncio
import base64
import logging
import os
import tempfile
from typing import Optional

import httpx
from openai import OpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------
# ЛОГИ
# ----------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ----------------------------
# ENV / НАСТРОЙКИ
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", 8700127990:AAERFsQ1OKOVpOC9nA3Zz1fnnalLRUUz864")
AITUNNEL_API_KEY = os.getenv("sk-aitunnel-cBbguC18FOui6evysHcjtKDT2aDKWdFc")
AITUNNEL_BASE_URL = os.getenv("https://api.aitunnel.ru/v1")

TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-4o-mini")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "300"))

# ----------------------------
# ПРОВЕРКА ENV
# ----------------------------
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not AITUNNEL_API_KEY:
    raise RuntimeError("AITUNNEL_API_KEY is missing")

BOT_TOKEN = BOT_TOKEN.strip()
AITUNNEL_API_KEY = AITUNNEL_API_KEY.strip()
AITUNNEL_BASE_URL = AITUNNEL_BASE_URL.strip()

logger.info("App started")
logger.info("BOT_TOKEN exists: %s", bool(BOT_TOKEN))
logger.info("BOT_TOKEN prefix: %s", BOT_TOKEN[:10] if BOT_TOKEN else "None")
logger.info("BOT_TOKEN length: %s", len(BOT_TOKEN) if BOT_TOKEN else 0)
logger.info("AITUNNEL_API_KEY exists: %s", bool(AITUNNEL_API_KEY))
logger.info(
    "AITUNNEL_API_KEY prefix: %s",
    AITUNNEL_API_KEY[:10] if AITUNNEL_API_KEY else "None",
)
logger.info("AITUNNEL_BASE_URL: %s", AITUNNEL_BASE_URL)
logger.info("TEXT_MODEL: %s", TEXT_MODEL)
logger.info("IMAGE_MODEL: %s", IMAGE_MODEL)

# ----------------------------
# OPENAI / AITUNNEL CLIENT
# ----------------------------
http_client = httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT))

client = OpenAI(
    api_key=AITUNNEL_API_KEY,
    base_url=AITUNNEL_BASE_URL,
    http_client=http_client,
)

# ----------------------------
# ВСПОМОГАТЕЛЬНОЕ
# ----------------------------
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

SYSTEM_PROMPT = (
    "Ты полезный Telegram-бот. "
    "Отвечай кратко, ясно и по делу. "
    "Если вопрос не требует длинного ответа, не растягивай."
)


def is_image_request(text: str) -> bool:
    text = text.lower().strip()
    return any(trigger in text for trigger in IMAGE_TRIGGERS)


def extract_image_b64(response) -> Optional[str]:
    """
    Пытаемся достать base64-картинку из ответа.
    Под разные совместимые реализации API.
    """
    # Вариант 1: response.data[0].b64_json
    data = getattr(response, "data", None)
    if data and len(data) > 0:
        item = data[0]
        b64_json = getattr(item, "b64_json", None)
        if b64_json:
            return b64_json

    # Вариант 2: response.output[0].content[0].image_base64
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
            pass

    return None


async def send_typing(update: Update, action: str = ChatAction.TYPING) -> None:
    if update.effective_chat:
        await update.effective_chat.send_action(action=action)


# ----------------------------
# OPENAI ВЫЗОВЫ
# ----------------------------
def generate_text_blocking(user_text: str) -> str:
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},],
    )

    answer = response.choices[0].message.content
    if not answer:
        return "Не удалось получить ответ."
    return answer.strip()


def generate_image_blocking(prompt: str) -> bytes:
    """
    Пытаемся сгенерировать картинку.
    """
    # На совместимых API обычно работает images.generate
    response = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size="1024x1024",
    )

    image_b64 = extract_image_b64(response)
    if not image_b64:
        raise RuntimeError("API did not return image data")

    return base64.b64decode(image_b64)


# ----------------------------
# ХЭНДЛЕРЫ TELEGRAM
# ----------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет. Я бот.\n\n"
        "Что умею:\n"
        "— отвечать на текстовые вопросы\n"
        "— генерировать изображения\n\n"
        "Примеры:\n"
        "— Напиши пост про продуктивность\n"
        "— Сгенерируй картинку кота в космосе"
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Команды:\n"
        "/start — запуск\n"
        "/help — помощь\n\n"
        "Просто отправь текст.\n"
        "Для картинки напиши, например:\n"
        "«Сгенерируй картинку города будущего ночью»"
    )
    await update.message.reply_text(text)


async def handle_image_request(update: Update, user_text: str) -> None:
    await send_typing(update, ChatAction.UPLOAD_PHOTO)
    progress_sent = False
    start_time = asyncio.get_running_loop().time()

    try:
        image_bytes = await asyncio.to_thread(generate_image_blocking, user_text)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(image_bytes)
            temp_path = tmp.name

        try:
            with open(temp_path, "rb") as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption="Готово",
                )
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    except Exception as e:
        logger.exception("Image generation failed: %s", e)
        elapsed = asyncio.get_running_loop().time() - start_time
        if elapsed > 60 and not progress_sent:
            progress_sent = True
            await update.message.reply_text(
                "Генерация заняла слишком много времени и завершилась ошибкой."
            )

        await update.message.reply_text(
            "Не удалось создать изображение. Проверь запрос или ключ API."
        )


async def handle_text_request(update: Update, user_text: str) -> None:
    await send_typing(update, ChatAction.TYPING)

    try:
        answer = await asyncio.to_thread(generate_text_blocking, user_text)

        # Telegram ограничивает длину сообщения, разобьём при необходимости
        max_len = 4000
        for i in range(0, len(answer), max_len):
            await update.message.reply_text(answer[i:i + max_len])

    except Exception as e:
        logger.exception("Text generation failed: %s", e)
        await update.message.reply_text(
            "Не удалось получить ответ. Проверь ключ API и модель."
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_text = update.message.text.strip()
    if not user_text:
        await update.message.reply_text("Отправь текстовый запрос.")
        return

    logger.info(
        "Message from user_id=%s text=%s",
        update.effective_user.id if update.effective_user else "unknown",
        user_text[:200],
    )

    if is_image_request(user_text):
        await handle_image_request(update, user_text)
    else:
        await handle_text_request(update, user_text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)


# ----------------------------
# ЗАПУСК
# ----------------------------
def main() -> None:
    logger.info("Building Telegram application")
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    application.add_error_handler(error_handler)

    logger.info("Bot is starting polling")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
