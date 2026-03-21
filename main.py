import asyncio
import base64
import logging
import os
from io import BytesIO
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

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-ai-bot")

# =========================================================
# ENV / SETTINGS
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

AITUNNEL_API_KEY = os.getenv("AITUNNEL_API_KEY", "").strip()
AITUNNEL_BASE_URL = os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1").strip()

TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-4o-mini").strip()
VISION_MODEL = os.getenv("VISION_MODEL", TEXT_MODEL).strip()
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1").strip()

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "300"))
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "12000"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not AITUNNEL_API_KEY:
    raise RuntimeError("AITUNNEL_API_KEY is missing")

http_client = httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT))

client = OpenAI(
    api_key=AITUNNEL_API_KEY,
    base_url=AITUNNEL_BASE_URL,
    http_client=http_client,
)

# =========================================================
# PROMPTS / TRIGGERS
# =========================================================

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

EDIT_IMAGE_TRIGGERS = [
    "измени фото",
    "измени изображение",
    "отредактируй фото",
    "отредактируй изображение",
    "редактируй фото",
    "редактируй изображение",
    "замени фон",
    "сделай фон",
    "убери фон",
    "добавь на фото",
    "добавь к фото",
    "edit image",
    "edit photo",
]

SYSTEM_PROMPT = (
    "Ты полезный Telegram-бот. "
    "Отвечай кратко, ясно и по делу. "
    "Если пользователь прислал изображение без явного запроса на редактирование, "
    "опиши, что на нём видно, и дай полезный краткий анализ."
)

VISION_PROMPT = (
    "Опиши изображение кратко и точно на русском языке. "
    "Если это товарное фото, выдели: что за предмет, стиль, цвета, материалы, "
    "фон, качество фото и возможные улучшения для карточки товара."
)

IMAGE_GEN_STYLE_PROMPT = (
    "Сделай результат очень качественным, как премиальная рекламная съёмка. "
    "Высокая детализация, реалистичный свет, хорошая композиция, "
    "дорогой визуальный стиль, без текста и водяных знаков."
)

# =========================================================
# HELPERS
# =========================================================

def is_image_request(text: str) -> bool:
    text = text.lower().strip()
    return any(trigger in text for trigger in IMAGE_TRIGGERS)

def is_image_edit_request(text: str) -> bool:
    text = text.lower().strip()
    return any(trigger in text for trigger in EDIT_IMAGE_TRIGGERS)

def trim_text(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."

def split_long_message(text: str, chunk_size: int = 4000) -> list[str]:
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]

def extract_image_b64_from_images_response(response) -> Optional[str]:
    """
    Пытаемся достать base64-картинку из разных совместимых ответов API.
    """
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
            pass

    return None

async def send_typing(update: Update, action: str = ChatAction.TYPING) -> None:
    if update.effective_chat:
        await update.effective_chat.send_action(action=action)

async def get_telegram_image_bytes(update: Update) -> Optional[bytes]:
    if not update.message:
        return None

    tg_file = None

    if update.message.photo:
        tg_file = await update.message.photo[-1].get_file()
    elif update.message.document and update.message.document.mime_type:
        if update.message.document.mime_type.startswith("image/"):
            tg_file = await update.message.document.get_file()

    if tg_file is None:
        return None

    bio = BytesIO()
    await tg_file.download_to_memory(out=bio)
    return bio.getvalue()

# =========================================================
# OPENAI / AITUNNEL CALLS
# =========================================================

def generate_text_blocking(user_text: str) -> str:
    """
    Через chat.completions для лучшей совместимости с прокси/OpenAI-compatible API.
    """
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ],
        temperature=0.7,
    )

    answer = response.choices[0].message.content
    if not answer:
        return "Не удалось получить ответ."
    return answer.strip()

def analyze_image_blocking(image_bytes: bytes, caption_text: str = "") -> str:
    """
    Распознавание изображения через chat.completions с image_url.
    Это обычно лучше совместимо с OpenAI-compatible proxy.
    """
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    prompt = VISION_PROMPT
    if caption_text:
        prompt += f"\n\nДополнительный запрос пользователя: {caption_text}"

    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}"
                        },
                    },
                ],
            },
        ],
        temperature=0.3,
    )

    answer = response.choices[0].message.content
    if not answer:
        return "Не удалось распознать изображение."
    return answer.strip()

def generate_image_blocking(prompt: str) -> bytes:
    response = client.images.generate(
        model=IMAGE_MODEL,
        prompt=f"{prompt}\n\n{IMAGE_GEN_STYLE_PROMPT}",
        size="1024x1024",
        quality="high",
        output_format="png",
    )

    image_b64 = extract_image_b64_from_images_response(response)
    if not image_b64:
        raise RuntimeError("API не вернул изображение в base64")
    return base64.b64decode(image_b64)

def edit_image_blocking(image_bytes: bytes, prompt: str) -> bytes:
    """
    Если у AITunnel images.edit совместим с OpenAI SDK, это будет работать.
    Если их прокси не поддерживает edit endpoint, бот вернёт понятную ошибку.
    """
    response = client.images.edit(
        model=IMAGE_MODEL,
        image=("input.png", image_bytes, "image/png"),
        prompt=f"{prompt}\n\n{IMAGE_GEN_STYLE_PROMPT}",
        size="1024x1024",
        quality="high",
        output_format="png",
    )

    image_b64 = extract_image_b64_from_images_response(response)
    if not image_b64:
        raise RuntimeError("API не вернул отредактированное изображение")
    return base64.b64decode(image_b64)

# =========================================================
# COMMANDS
# =========================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Привет. Я AI-бот.\n\n"
        "Что умею:\n"
        "— отвечать на текстовые вопросы\n"
        "— распознавать присланные изображения\n"
        "— генерировать картинки по описанию\n"
        "— редактировать присланные фото по инструкции\n\n"
        "Примеры:\n"
        "— Сгенерируй картинку премиального флакона духов на мраморе\n"
        "— Пришли фото товара и подпиши: замени фон на люкс-студию\n"
        "— Просто пришли фото без подписи, и я его опишу"
    )
    await update.message.reply_text(text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Команды:\n"
        "/start — запуск\n"
        "/help — помощь\n\n"
        "Сценарии:\n"
        "1) Текст → отвечаю текстом\n"
        "2) Текст с запросом на картинку → генерирую изображение\n"
        "3) Фото без запроса → распознаю и описываю\n"
        "4) Фото + инструкция → редактирую фото"
    )
    await update.message.reply_text(text)

# =========================================================
# HANDLERS
# =========================================================

async def handle_image_request(update: Update, user_text: str) -> None:
    await send_typing(update, ChatAction.UPLOAD_PHOTO)

    try:
        image_bytes = await asyncio.to_thread(generate_image_blocking, user_text)
        await update.message.reply_photo(photo=image_bytes)
    except Exception as e:
        logger.exception("Image generation failed: %s", e)
        await update.message.reply_text(
            f"Не удалось сгенерировать изображение.\n\nОшибка: {e}"
        )

async def handle_text_request(update: Update, user_text: str) -> None:
    await send_typing(update, ChatAction.TYPING)

    try:
        answer = await asyncio.to_thread(generate_text_blocking, user_text)
        for chunk in split_long_message(trim_text(answer), 4000):
            await update.message.reply_text(chunk)
    except Exception as e:
        logger.exception("Text generation failed: %s", e)
        await update.message.reply_text(
            f"Не удалось получить ответ.\n\nОшибка: {e}"
        )

async def handle_photo_analysis(update: Update, caption_text: str = "") -> None:
    await send_typing(update, ChatAction.TYPING)

    try:
        image_bytes = await get_telegram_image_bytes(update)
        if not image_bytes:
            await update.message.reply_text("Не удалось скачать изображение.")
            return

        answer = await asyncio.to_thread(analyze_image_blocking, image_bytes, caption_text)
        for chunk in split_long_message(trim_text(answer), 4000):
            await update.message.reply_text(chunk)
    except Exception as e:
        logger.exception("Image analysis failed: %s", e)
        await update.message.reply_text(
            f"Не удалось распознать изображение.\n\nОшибка: {e}"
        )

async def handle_photo_edit(update: Update, edit_prompt: str) -> None:
    await send_typing(update, ChatAction.UPLOAD_PHOTO)

    try:
        image_bytes = await get_telegram_image_bytes(update)
        if not image_bytes:
            await update.message.reply_text("Не удалось скачать изображение.")
            return

        result_bytes = await asyncio.to_thread(edit_image_blocking, image_bytes, edit_prompt)
        await update.message.reply_photo(photo=result_bytes)
    except Exception as e:
        logger.exception("Image edit failed: %s", e)
        await update.message.reply_text(
            f"Не удалось отредактировать изображение.\n\nОшибка: {e}"
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    has_image = bool(update.message.photo) or (
        update.message.document
        and update.message.document.mime_type
        and update.message.document.mime_type.startswith("image/")
    )

    if has_image:
        caption = (update.message.caption or "").strip()
        logger.info(
            "Image message from user_id=%s caption=%s",
            update.effective_user.id if update.effective_user else "unknown",
            caption[:200],
        )

        if caption and is_image_edit_request(caption):
            await handle_photo_edit(update, caption)
        else:
            await handle_photo_analysis(update, caption)
        return

    if not update.message.text:
        return

    user_text = update.message.text.strip()
    if not user_text:
        await update.message.reply_text("Отправь текстовый запрос.")
        return

    logger.info(
        "Text message from user_id=%s text=%s",
        update.effective_user.id if update.effective_user else "unknown",
        user_text[:200],
    )

    if is_image_request(user_text):
        await handle_image_request(update, user_text)
    else:
        await handle_text_request(update, user_text)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)

# =========================================================
# MAIN
# =========================================================

def main() -> None:
    logger.info("Building Telegram application")
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.IMAGE | (filters.TEXT & ~filters.COMMAND),
            handle_message,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Bot is starting polling")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
