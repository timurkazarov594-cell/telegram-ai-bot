import asyncio
import base64
import json
import logging
import os
import sqlite3
import tempfile
import threading
from typing import Dict, Optional, Tuple

import httpx
from openai import OpenAI
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

# =========================================================
# ЛОГИ
# =========================================================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# ENV
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
AITUNNEL_API_KEY = os.getenv("AITUNNEL_API_KEY")
AITUNNEL_BASE_URL = os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1")

TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-4o-mini")
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o-mini")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1-mini")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "300"))
DB_PATH = os.getenv("DB_PATH", "interior_simple_bot.db")

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
# ТАРИФЫ
# =========================================================
FREE_RENDER_LIMIT = 2

PACK_4_PRICE_STARS = 300
PACK_4_CREDITS = 4

PACK_10_PRICE_STARS = 600
PACK_10_CREDITS = 10

PAYLOAD_PACK_4 = "stars_pack_4_renders"
PAYLOAD_PACK_10 = "stars_pack_10_renders"

# =========================================================
# ОПРОС
# =========================================================
QUESTIONS = [
    ("style", "Какой стиль интерьера нужен?\nПримеры: минимализм, современный, лофт, джапанди, неоклассика."),
    ("room_type", "Какое помещение?\nПримеры: кухня, спальня, гостиная, детская, офис, студия."),
    ("colors", "Какие цвета нравятся?\nПримеры: бежевый, серый, белый, дерево, оливковый."),
    ("furniture", "Какая мебель или зоны обязательно нужны?\nПример: большой диван, рабочее место, остров, шкаф до потолка."),
    ("budget", "Какой бюджет?\nПример: до 300 000 ₽ / средний / премиум."),
    ("extra", "Дополнительные пожелания?\nПример: больше света, много хранения, уютно, без темных цветов."),
]

QUESTION_KEYS = [q[0] for q in QUESTIONS]

STATUS_IDLE = "idle"
STATUS_ASKING = "asking"
STATUS_WAITING_PHOTO = "waiting_photo"
STATUS_RENDERING = "rendering"

# =========================================================
# SQLITE
# =========================================================
db_lock = threading.Lock()
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row


def init_db() -> None:
    with db_lock:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                free_renders_used INTEGER NOT NULL DEFAULT 0,
                paid_renders_balance INTEGER NOT NULL DEFAULT 0,
                current_index INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'idle',
                photo_analysis TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_answers (
                user_id INTEGER NOT NULL,
                answer_key TEXT NOT NULL,
                answer_value TEXT,
                PRIMARY KEY (user_id, answer_key)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                invoice_payload TEXT NOT NULL,
                stars_amount INTEGER NOT NULL,
                credits_added INTEGER NOT NULL,
                telegram_payment_charge_id TEXT,
                provider_payment_charge_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.commit()


def ensure_user_exists(user_id: int) -> None:
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,),
        )
        conn.commit()


def get_user(user_id: int) -> sqlite3.Row:
    ensure_user_exists(user_id)
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()


def get_answers(user_id: int) -> Dict[str, str]:
    ensure_user_exists(user_id)
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "SELECT answer_key, answer_value FROM user_answers WHERE user_id = ?",
            (user_id,),
        )
        rows = cur.fetchall()
    return {r["answer_key"]: (r["answer_value"] or "") for r in rows}


def set_user_fields(
    user_id: int,
    *,
    free_renders_used: Optional[int] = None,
    paid_renders_balance: Optional[int] = None,
    current_index: Optional[int] = None,
    status: Optional[str] = None,
    photo_analysis: Optional[str] = None,
    reset_photo_analysis: bool = False,
) -> None:
    ensure_user_exists(user_id)

    fields = []
    values = []

    if free_renders_used is not None:
        fields.append("free_renders_used = ?")
        values.append(free_renders_used)

    if paid_renders_balance is not None:
        fields.append("paid_renders_balance = ?")
        values.append(paid_renders_balance)

    if current_index is not None:
        fields.append("current_index = ?")
        values.append(current_index)

    if status is not None:
        fields.append("status = ?")
        values.append(status)

    if reset_photo_analysis:
        fields.append("photo_analysis = NULL")
    elif photo_analysis is not None:
        fields.append("photo_analysis = ?")
        values.append(photo_analysis)

    if not fields:
        return

    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(user_id)

    with db_lock:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?",
            values,
        )
        conn.commit()


def save_answer(user_id: int, key: str, value: str) -> None:
    ensure_user_exists(user_id)
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_answers (user_id, answer_key, answer_value)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, answer_key)
            DO UPDATE SET answer_value = excluded.answer_value
            """,
            (user_id, key, value),
        )
        cur.execute(
            "UPDATE users SET updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()


def clear_brief(user_id: int) -> None:
    ensure_user_exists(user_id)
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM user_answers WHERE user_id = ?", (user_id,))
        cur.execute(
            """
            UPDATE users
            SET current_index = 0,
                status = ?,
                photo_analysis = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (STATUS_ASKING, user_id),
        )
        conn.commit()


def add_paid_credits(user_id: int, amount: int) -> None:
    ensure_user_exists(user_id)
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET paid_renders_balance = paid_renders_balance + ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (amount, user_id),
        )
        conn.commit()


def register_payment(
    user_id: int,
    invoice_payload: str,
    stars_amount: int,
    credits_added: int,
    telegram_payment_charge_id: Optional[str],
    provider_payment_charge_id: Optional[str],
) -> None:
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO payments (
                user_id,
                invoice_payload,
                stars_amount,
                credits_added,
                telegram_payment_charge_id,
                provider_payment_charge_id
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                invoice_payload,
                stars_amount,
                credits_added,
                telegram_payment_charge_id,
                provider_payment_charge_id,
            ),
        )
        conn.commit()


def get_free_left(user: sqlite3.Row) -> int:
    return max(0, FREE_RENDER_LIMIT - user["free_renders_used"])


def get_total_left(user: sqlite3.Row) -> int:
    return get_free_left(user) + user["paid_renders_balance"]


def can_render(user: sqlite3.Row) -> bool:
    return get_total_left(user) > 0


def consume_one_render(user_id: int) -> Tuple[str, int, int]:
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "SELECT free_renders_used, paid_renders_balance FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()

        free_used = row["free_renders_used"]
        paid_balance = row["paid_renders_balance"]
        free_left = max(0, FREE_RENDER_LIMIT - free_used)

        if free_left > 0:
            free_used += 1
            cur.execute(
                """
                UPDATE users
                SET free_renders_used = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (free_used, user_id),
            )
            conn.commit()
            return "free", max(0, FREE_RENDER_LIMIT - free_used), paid_balance

        if paid_balance > 0:
            paid_balance -= 1
            cur.execute(
                """
                UPDATE users
                SET paid_renders_balance = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ?
                """,
                (paid_balance, user_id),
            )
            conn.commit()
            return "paid", free_left, paid_balance

        raise RuntimeError("No render credits left")


# =========================================================
# ВСПОМОГАТЕЛЬНОЕ
# =========================================================
async def send_typing(update: Update, action: str = ChatAction.TYPING) -> None:
    if update.effective_chat:
        await update.effective_chat.send_action(action=action)


def encode_file_to_data_url(file_path: str, mime_type: str = "image/jpeg") -> str:
    with open(file_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


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
            pass

    return None


def balance_text(user: sqlite3.Row) -> str:
    return (
        f"Баланс:\n"
        f"— Бесплатных генераций осталось: {get_free_left(user)}\n"
        f"— Платных генераций осталось: {user['paid_renders_balance']}\n"
        f"— Всего доступно: {get_total_left(user)}"
    )


def build_prompt(answers: Dict[str, str], photo_analysis: str) -> str:
    return f"""
You are a professional interior designer and prompt engineer.

Create one strong English prompt for an interior render.

Client brief:
- Style: {answers.get("style", "not specified")}
- Room type: {answers.get("room_type", "not specified")}
- Preferred colors: {answers.get("colors", "not specified")}
- Required furniture/zones: {answers.get("furniture", "not specified")}
- Budget: {answers.get("budget", "not specified")}
- Extra wishes: {answers.get("extra", "not specified")}

Photo analysis:
{photo_analysis}

Requirements:
- realistic premium interior render
- cohesive composition
- interior design level quality
- visually rich but not overloaded
- correct lighting
- detailed materials
- furniture appropriate to style
- based on the real room proportions from the photo if possible

Return only one final English image generation prompt, without comments or headings.
""".strip()


# =========================================================
# OPENAI
# =========================================================
def analyze_image_blocking(image_path: str) -> str:
    image_data_url = encode_file_to_data_url(image_path)

    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты дизайнер интерьера. "
                    "Кратко и полезно проанализируй фото помещения. "
                    "Определи пространство, планировку, стиль, свет, материалы, проблемы помещения "
                    "и что важно учесть для будущей визуализации."
                ),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Проанализируй это помещение как интерьерный дизайнер."},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            },
        ],
    )

    answer = response.choices[0].message.content
    return answer.strip() if answer else "Не удалось проанализировать фото."


def generate_render_prompt_blocking(answers: Dict[str, str], photo_analysis: str) -> str:
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты профессиональный интерьерный prompt engineer. "
                    "Возвращай только один сильный prompt на английском для генерации изображения."
                ),
            },
            {
                "role": "user",
                "content": build_prompt(answers, photo_analysis),
            },
        ],
    )

    answer = response.choices[0].message.content
    if not answer:
        raise RuntimeError("Не удалось собрать prompt")
    return answer.strip()


def generate_image_blocking(prompt: str) -> bytes:
    response = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size="1024x1024",
    )

    image_b64 = extract_image_b64(response)
    if not image_b64:
        raise RuntimeError("Не удалось получить изображение из ответа API")

    return base64.b64decode(image_b64)


# =========================================================
# МЕНЮ
# =========================================================
async def setup_bot_commands(application: Application) -> None:
    commands = [
        BotCommand("start", "Начать новый подбор интерьера"),
        BotCommand("balance", "Посмотреть баланс"),
        BotCommand("topup", "Пополнить баланс"),
    ]
    await application.bot.set_my_commands(commands)


# =========================================================
# STARS
# =========================================================
def topup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("300 ⭐ = 4 генерации", callback_data="buy_pack_4"),
            ],
            [
                InlineKeyboardButton("600 ⭐ = 10 генераций", callback_data="buy_pack_10"),
            ],
        ]
    )


async def send_stars_invoice(
    update: Update,
    title: str,
    description: str,
    payload: str,
    stars_amount: int,
    label: str,
) -> None:
    target = update.effective_message
    await target.reply_invoice(
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label, stars_amount)],
    )


async def topup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user(update.effective_user.id)
    await update.message.reply_text(
        "Выбери пакет пополнения:",
        reply_markup=topup_keyboard(),
    )
    await update.message.reply_text(balance_text(user))


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "buy_pack_4":
        await send_stars_invoice(
            update,
            "Пакет 4 генерации",
            "4 дополнительных генерации интерьера",
            PAYLOAD_PACK_4,
            PACK_4_PRICE_STARS,
            "4 генерации",
        )
    elif query.data == "buy_pack_10":
        await send_stars_invoice(
            update,
            "Пакет 10 генераций",
            "10 дополнительных генераций интерьера",
            PAYLOAD_PACK_10,
            PACK_10_PRICE_STARS,
            "10 генераций",
        )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if query.invoice_payload not in {PAYLOAD_PACK_4, PAYLOAD_PACK_10}:
        await query.answer(ok=False, error_message="Неизвестный пакет оплаты.")
        return
    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payment = update.message.successful_payment
    user_id = update.effective_user.id

    if payment.invoice_payload == PAYLOAD_PACK_4:
        credits = PACK_4_CREDITS
        stars = PACK_4_PRICE_STARS
    elif payment.invoice_payload == PAYLOAD_PACK_10:
        credits = PACK_10_CREDITS
        stars = PACK_10_PRICE_STARS
    else:
        await update.message.reply_text("Платеж получен, но пакет не распознан.")
        return

    add_paid_credits(user_id, credits)
    register_payment(
        user_id=user_id,
        invoice_payload=payment.invoice_payload,
        stars_amount=stars,
        credits_added=credits,
        telegram_payment_charge_id=getattr(payment, "telegram_payment_charge_id", None),
        provider_payment_charge_id=getattr(payment, "provider_payment_charge_id", None),
    )

    user = get_user(user_id)
    await update.message.reply_text(
        f"Оплата прошла успешно ✅\n\nНачислено: {credits} генераций\n\n{balance_text(user)}"
    )


# =========================================================
# КОМАНДЫ
# =========================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    clear_brief(user_id)

    await update.message.reply_text(
        "Привет. Я помогу собрать интерьерный бриф и в конце сгенерирую интерьер по фото.\n\n"
        "Отвечай по очереди на вопросы.\n\n"
        f"Вопрос 1/{len(QUESTIONS)}:\n{QUESTIONS[0][1]}"
    )


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user(update.effective_user.id)
    await update.message.reply_text(balance_text(user))


# =========================================================
# ОПРОС
# =========================================================
async def handle_question_answer(update: Update, user: sqlite3.Row, text: str) -> bool:
    user_id = update.effective_user.id

    if user["status"] != STATUS_ASKING:
        return False

    current_index = user["current_index"]

    if current_index < 0 or current_index >= len(QUESTIONS):
        return False

    key, _ = QUESTIONS[current_index]
    save_answer(user_id, key, text)

    next_index = current_index + 1

    if next_index >= len(QUESTIONS):
        set_user_fields(user_id, current_index=len(QUESTIONS), status=STATUS_WAITING_PHOTO)
        await update.message.reply_text(
            "Отлично. Теперь отправь фото помещения.\n\n"
            "После фото я:\n"
            "— проанализирую пространство\n"
            "— соберу prompt по твоим ответам\n"
            "— сгенерирую интерьер"
        )
        return True

    set_user_fields(user_id, current_index=next_index, status=STATUS_ASKING)
    await update.message.reply_text(f"Вопрос {next_index + 1}/{len(QUESTIONS)}:\n{QUESTIONS[next_index][1]}")
    return True


# =========================================================
# РЕНДЕР ПО ФОТО
# =========================================================
async def handle_photo_and_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = get_user(user_id)

    if user["status"] != STATUS_WAITING_PHOTO:
        await update.message.reply_text(
            "Сейчас фото не ожидается.\n\nНажми /start, чтобы начать новый подбор."
        )
        return

    if not can_render(user):
        await update.message.reply_text(
            "У тебя закончились генерации.\n\n"
            "Пополнить баланс: /topup\n"
            f"{balance_text(user)}"
        )
        return

    if not update.message.photo:
        await update.message.reply_text("Не вижу фото.")
        return

    set_user_fields(user_id, status=STATUS_RENDERING)
    await send_typing(update, ChatAction.UPLOAD_PHOTO)

    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        temp_path = tmp.name

    await tg_file.download_to_drive(temp_path)

    try:
        await update.message.reply_text("Анализирую фото помещения...")
        photo_analysis = await asyncio.to_thread(analyze_image_blocking, temp_path)
        set_user_fields(user_id, photo_analysis=photo_analysis)

        answers = get_answers(user_id)

        await update.message.reply_text("Собираю интерьерный prompt...")
        render_prompt = await asyncio.to_thread(
            generate_render_prompt_blocking,
            answers,
            photo_analysis,
        )

        await update.message.reply_text("Генерирую интерьер...")
        image_bytes = await asyncio.to_thread(generate_image_blocking, render_prompt)

        source, free_left_after, paid_left_after = consume_one_render(user_id)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as out:
            out.write(image_bytes)
            out_path = out.name

        try:
            with open(out_path, "rb") as img:
                used_text = "Списана бесплатная генерация." if source == "free" else "Списана платная генерация."
                await update.message.reply_photo(
                    photo=img,
                    caption=(
                        "Готово ✅\n\n"
                        f"{used_text}\n"
                        f"Бесплатных осталось: {free_left_after}\n"
                        f"Платных осталось: {paid_left_after}\n\n"
                        "Чтобы сделать новый вариант, нажми /start"
                    ),
                )
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)

        set_user_fields(user_id, status=STATUS_IDLE, current_index=0)

    except Exception as e:
        logger.exception("Render flow failed: %s", e)
        set_user_fields(user_id, status=STATUS_WAITING_PHOTO)
        await update.message.reply_text(
            "Не удалось сгенерировать интерьер.\n"
            "Попробуй отправить фото еще раз."
        )
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


# =========================================================
# ОБЫЧНЫЕ ТЕКСТЫ
# =========================================================
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    user = get_user(user_id)
    text = update.message.text.strip()

    if not text:
        await update.message.reply_text("Сообщение пустое.")
        return

    handled = await handle_question_answer(update, user, text)
    if handled:
        return

    if user["status"] == STATUS_WAITING_PHOTO:
        await update.message.reply_text("Теперь нужно отправить фото помещения.")
        return

    if user["status"] == STATUS_RENDERING:
        await update.message.reply_text("Сейчас идет генерация, подожди немного.")
        return

    await update.message.reply_text(
        "Нажми /start, чтобы начать подбор интерьера.\n"
        "Баланс: /balance\n"
        "Пополнение: /topup"
    )


# =========================================================
# ОШИБКИ
# =========================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error: %s", context.error)


# =========================================================
# ЗАПУСК
# =========================================================
def main() -> None:
    init_db()
    logger.info("Building Telegram application")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(setup_bot_commands)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("topup", topup_command))

    application.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy_pack_"))
    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_and_render))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    application.add_error_handler(error_handler)

    logger.info("Bot is starting polling")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
