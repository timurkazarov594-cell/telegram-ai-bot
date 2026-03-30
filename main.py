import asyncio
import base64
import json
import logging
import os
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import httpx
from openai import OpenAI
from telegram import BotCommand, LabeledPrice, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
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
# ENV / НАСТРОЙКИ
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
AITUNNEL_API_KEY = os.getenv("AITUNNEL_API_KEY")
AITUNNEL_BASE_URL = os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1")

TEXT_MODEL = os.getenv("TEXT_MODEL", "gpt-4o-mini")
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-4o-mini")
IMAGE_MODEL = os.getenv("IMAGE_MODEL", "gpt-image-1")

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "300"))
DB_PATH = os.getenv("DB_PATH", "interior_bot.db")

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
# БРИФ
# =========================================================
QUESTIONS = [
    (
        "room_type",
        "1/12\nКакой тип помещения?\n"
        "Примеры: кухня, спальня, гостиная, детская, ванная, офис, студия.",
    ),
    (
        "style",
        "2/12\nКакой стиль вам нравится?\n"
        "Примеры: минимализм, современный, скандинавский, лофт, джапанди, неоклассика.",
    ),
    (
        "area",
        "3/12\nКакая площадь помещения?\n"
        "Пример: 18 м².",
    ),
    (
        "budget",
        "4/12\nКакой бюджет?\n"
        "Пример: до 200 000 ₽ / средний / премиум.",
    ),
    (
        "colors_like",
        "5/12\nКакие цвета нравятся?\n"
        "Примеры: бежевый, серый, тёплое дерево, белый, оливковый.",
    ),
    (
        "colors_dislike",
        "6/12\nКакие цвета не нравятся?",
    ),
    (
        "residents",
        "7/12\nКто будет пользоваться помещением?\n"
        "Пример: один человек, пара, семья с ребёнком.",
    ),
    (
        "kids_pets",
        "8/12\nЕсть ли дети или животные?\n"
        "Пример: кот, собака, маленький ребёнок, никого нет.",
    ),
    (
        "must_have",
        "9/12\nЧто обязательно должно быть в интерьере?\n"
        "Примеры: много хранения, рабочее место, большой диван, остров, туалетный столик.",
    ),
    (
        "avoid",
        "10/12\nЧто точно не хочется видеть?\n"
        "Примеры: глянец, тёмные стены, открытые полки, яркие цвета.",
    ),
    (
        "lighting",
        "11/12\nКакое нужно настроение и освещение?\n"
        "Примеры: уютно и тепло, много естественного света, мягкая вечерняя подсветка.",
    ),
    (
        "references",
        "12/12\nЕсть ли референсы, пожелания или примеры?\n"
        "Можно написать словами, даже если фото ещё не отправляли.",
    ),
]

QUESTION_KEYS = [item[0] for item in QUESTIONS]


# =========================================================
# БАЗА ДАННЫХ SQLITE
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
                current_index INTEGER NOT NULL DEFAULT -1,
                waiting_for_photo INTEGER NOT NULL DEFAULT 0,
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
                PRIMARY KEY (user_id, answer_key),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
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
            """
            INSERT OR IGNORE INTO users (user_id)
            VALUES (?)
            """,
            (user_id,),
        )
        conn.commit()


@dataclass
class UserData:
    user_id: int
    free_renders_used: int
    paid_renders_balance: int
    current_index: int
    waiting_for_photo: bool
    photo_analysis: Optional[str]
    answers: Dict[str, str]


def get_user_data(user_id: int) -> UserData:
    ensure_user_exists(user_id)

    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()

        cur.execute(
            "SELECT answer_key, answer_value FROM user_answers WHERE user_id = ?",
            (user_id,),
        )
        answer_rows = cur.fetchall()

    answers = {r["answer_key"]: (r["answer_value"] or "") for r in answer_rows}

    return UserData(
        user_id=user_id,
        free_renders_used=row["free_renders_used"],
        paid_renders_balance=row["paid_renders_balance"],
        current_index=row["current_index"],
        waiting_for_photo=bool(row["waiting_for_photo"]),
        photo_analysis=row["photo_analysis"],
        answers=answers,
    )


def update_user_fields(
    user_id: int,
    *,
    free_renders_used: Optional[int] = None,
    paid_renders_balance: Optional[int] = None,
    current_index: Optional[int] = None,
    waiting_for_photo: Optional[bool] = None,
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

    if waiting_for_photo is not None:
        fields.append("waiting_for_photo = ?")
        values.append(1 if waiting_for_photo else 0)

    if reset_photo_analysis:
        fields.append("photo_analysis = NULL")
    elif photo_analysis is not None:
        fields.append("photo_analysis = ?")
        values.append(photo_analysis)

    if not fields:
        return

    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(user_id)

    query = f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?"

    with db_lock:
        cur = conn.cursor()
        cur.execute(query, values)
        conn.commit()


def save_answer(user_id: int, answer_key: str, answer_value: str) -> None:
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
            (user_id, answer_key, answer_value),
        )
        cur.execute(
            "UPDATE users SET updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()


def clear_user_data(user_id: int) -> None:
    ensure_user_exists(user_id)

    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM user_answers WHERE user_id = ?", (user_id,))
        cur.execute(
            """
            UPDATE users
            SET free_renders_used = 0,
                paid_renders_balance = 0,
                current_index = -1,
                waiting_for_photo = 0,
                photo_analysis = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (user_id,),
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


def get_free_renders_left(user: UserData) -> int:
    return max(0, FREE_RENDER_LIMIT - user.free_renders_used)


def get_total_renders_left(user: UserData) -> int:
    return get_free_renders_left(user) + user.paid_renders_balance


def can_render(user: UserData) -> bool:
    return get_total_renders_left(user) > 0


def consume_one_render(user_id: int) -> Tuple[str, int, int]:
    """
    Возвращает:
    (source, free_left_after, paid_left_after)
    source = 'free' или 'paid'
    """
    ensure_user_exists(user_id)

    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT free_renders_used, paid_renders_balance FROM users WHERE user_id = ?", (user_id,))
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


def build_render_balance_text(user: UserData) -> str:
    return (
        f"Баланс генераций:\n"
        f"— Бесплатных осталось: {get_free_renders_left(user)}\n"
        f"— Платных осталось: {user.paid_renders_balance}\n"
        f"— Всего доступно: {get_total_renders_left(user)}"
    )


def build_brief_summary(user: UserData) -> str:
    lines = []

    for key, question in QUESTIONS:
        title_lines = question.split("\n")
        title = title_lines[1] if len(title_lines) > 1 else question
        value = user.answers.get(key, "—")
        lines.append(f"{title}\nОтвет: {value}\n")

    lines.append("Анализ фото:")
    lines.append(user.photo_analysis or "—")
    lines.append("")
    lines.append(build_render_balance_text(user))

    return "\n".join(lines)


def build_interior_prompt(user: UserData) -> str:
    data = user.answers

    return f"""
Ты — профессиональный дизайнер интерьера и визуализатор.

Ниже бриф клиента. На его основе создай продуманную интерьерную концепцию.

ДАННЫЕ КЛИЕНТА:
- Тип помещения: {data.get("room_type", "не указано")}
- Стиль: {data.get("style", "не указано")}
- Площадь: {data.get("area", "не указано")}
- Бюджет: {data.get("budget", "не указано")}
- Нравящиеся цвета: {data.get("colors_like", "не указано")}
- Нежелательные цвета: {data.get("colors_dislike", "не указано")}
- Кто пользуется помещением: {data.get("residents", "не указано")}
- Дети / животные: {data.get("kids_pets", "не указано")}
- Что обязательно должно быть: {data.get("must_have", "не указано")}
- Чего избегать: {data.get("avoid", "не указано")}
- Освещение / настроение: {data.get("lighting", "не указано")}
- Референсы / пожелания: {data.get("references", "не указано")}

АНАЛИЗ ФОТО:
{user.photo_analysis or "Фото пока не анализировалось."}

Сделай ответ строго в 4 частях:

1. КОНЦЕПЦИЯ
Кратко опиши идею интерьера.

2. РЕКОМЕНДАЦИИ
Дай практические рекомендации по:
- палитре
- материалам
- мебели
- освещению
- декору
- хранению
- зонированию

3. КРАТКОЕ ТЗ ДЛЯ ДИЗАЙНЕРА
Сделай сжатое техническое описание проекта.

4. PROMPT FOR IMAGE GENERATION
Напиши один сильный подробный prompt на английском языке для генерации интерьерного рендера.
Только один цельный prompt, без пояснений внутри.
""".strip()


def build_render_prompt_request(idea_text: str) -> str:
    return (
        "Извлеки из текста ниже только раздел 'PROMPT FOR IMAGE GENERATION'. "
        "Верни только готовый английский prompt без заголовков, без комментариев, без кавычек.\n\n"
        f"{idea_text}"
    )


# =========================================================
# OPENAI / AITUNNEL
# =========================================================
def generate_text_blocking(prompt: str, system_prompt: Optional[str] = None) -> str:
    messages = []

    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    messages.append({"role": "user", "content": prompt})

    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
    )

    answer = response.choices[0].message.content
    if not answer:
        return "Не удалось получить ответ."
    return answer.strip()


def analyze_image_blocking(image_path: str) -> str:
    image_data_url = encode_file_to_data_url(image_path)

    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты профессиональный дизайнер интерьера. "
                    "Проанализируй присланное изображение помещения, комнаты или интерьерного референса. "
                    "Определи предполагаемый стиль, цветовую палитру, материалы, мебель, освещение, "
                    "настроение пространства, удачные решения и проблемы. "
                    "Если это не реальное помещение, а референс — тоже напиши это. "
                    "Отвечай по-русски, структурно, кратко и полезно."
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Проанализируй это изображение как дизайнер интерьера и дай выводы для будущего проекта.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": image_data_url},
                    },
                ],
            },
        ],
    )

    answer = response.choices[0].message.content
    if not answer:
        return "Не удалось проанализировать изображение."
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
# МЕНЮ КОМАНД
# =========================================================
async def setup_bot_commands(application: Application) -> None:
    commands = [
        BotCommand("start", "Запуск бота"),
        BotCommand("brief", "Заполнить интерьерный бриф"),
        BotCommand("photo", "Отправить фото для анализа"),
        BotCommand("idea", "Получить интерьерную концепцию"),
        BotCommand("render", "Сгенерировать визуализацию"),
        BotCommand("balance", "Проверить баланс генераций"),
        BotCommand("plans", "Тарифы и покупка генераций"),
        BotCommand("buy4", "Купить 4 генерации за 300 ⭐"),
        BotCommand("buy10", "Купить 10 генераций за 600 ⭐"),
        BotCommand("showbrief", "Показать сохранённый бриф"),
        BotCommand("reset", "Очистить бриф"),
        BotCommand("help", "Помощь"),
    ]
    await application.bot.set_my_commands(commands)


# =========================================================
# STARS / ОПЛАТА
# =========================================================
async def send_stars_invoice(
    update: Update,
    title: str,
    description: str,
    payload: str,
    stars_amount: int,
    price_label: str,
) -> None:
    await update.message.reply_invoice(
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(price_label, stars_amount)],
    )


async def plans_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user_data(update.effective_user.id)

    text = (
        "Тарифы на рендеры:\n\n"
        "Бесплатно:\n"
        f"— первые {FREE_RENDER_LIMIT} генерации\n\n"
        "Платные пакеты:\n"
        f"— {PACK_4_PRICE_STARS} ⭐ = {PACK_4_CREDITS} генерации (/buy4)\n"
        f"— {PACK_10_PRICE_STARS} ⭐ = {PACK_10_CREDITS} генераций (/buy10)\n\n"
        f"{build_render_balance_text(user)}"
    )
    await update.message.reply_text(text)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user_data(update.effective_user.id)
    await update.message.reply_text(build_render_balance_text(user))


async def buy4_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_stars_invoice(
        update=update,
        title="Пакет 4 генерации",
        description="4 дополнительных интерьерных рендера",
        payload=PAYLOAD_PACK_4,
        stars_amount=PACK_4_PRICE_STARS,
        price_label="4 генерации",
    )


async def buy10_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_stars_invoice(
        update=update,
        title="Пакет 10 генераций",
        description="10 дополнительных интерьерных рендеров",
        payload=PAYLOAD_PACK_10,
        stars_amount=PACK_10_PRICE_STARS,
        price_label="10 генераций",
    )


async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query

    if query.invoice_payload not in {PAYLOAD_PACK_4, PAYLOAD_PACK_10}:
        await query.answer(ok=False, error_message="Неизвестный платёжный пакет.")
        return

    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    payload = payment.invoice_payload

    if payload == PAYLOAD_PACK_4:
        credits = PACK_4_CREDITS
        stars_amount = PACK_4_PRICE_STARS
    elif payload == PAYLOAD_PACK_10:
        credits = PACK_10_CREDITS
        stars_amount = PACK_10_PRICE_STARS
    else:
        await update.message.reply_text("Платёж получен, но пакет не распознан.")
        return

    add_paid_credits(user_id, credits)

    register_payment(
        user_id=user_id,
        invoice_payload=payload,
        stars_amount=stars_amount,
        credits_added=credits,
        telegram_payment_charge_id=getattr(payment, "telegram_payment_charge_id", None),
        provider_payment_charge_id=getattr(payment, "provider_payment_charge_id", None),
    )

    user = get_user_data(user_id)

    await update.message.reply_text(
        "Оплата прошла успешно ✅\n\n"
        f"Начислено генераций: {credits}\n\n"
        f"{build_render_balance_text(user)}"
    )


# =========================================================
# КОМАНДЫ
# =========================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user_data(update.effective_user.id)

    text = (
        "Привет! Я AI-бот дизайнер интерьера.\n\n"
        "Я умею:\n"
        "— собрать бриф по интерьеру\n"
        "— анализировать фото комнаты или референса\n"
        "— делать интерьерную концепцию\n"
        "— собирать prompt для визуализации\n"
        "— генерировать интерьерный рендер\n\n"
        "Лимиты:\n"
        f"— {FREE_RENDER_LIMIT} рендера бесплатно\n"
        f"— {PACK_4_PRICE_STARS} ⭐ → {PACK_4_CREDITS} генерации\n"
        f"— {PACK_10_PRICE_STARS} ⭐ → {PACK_10_CREDITS} генераций\n\n"
        "Команды:\n"
        "/brief — начать анкету\n"
        "/photo — отправить фото\n"
        "/idea — получить концепцию\n"
        "/render — сгенерировать интерьер\n"
        "/balance — баланс генераций\n"
        "/plans — тарифы\n"
        "/buy4 — купить 4 генерации\n"
        "/buy10 — купить 10 генераций\n"
        "/showbrief — показать бриф\n"
        "/reset — очистить бриф\n"
        "/help — помощь\n\n"
        f"{build_render_balance_text(user)}"
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "Как пользоваться ботом:\n\n"
        "1. Нажми /brief и ответь на вопросы.\n"
        "2. По желанию нажми /photo и отправь фото помещения или референса.\n"
        "3. Нажми /idea — я подготовлю интерьерную концепцию.\n"
        "4. Нажми /render — я сгенерирую визуализацию.\n\n"
        "По оплате:\n"
        f"— первые {FREE_RENDER_LIMIT} рендера бесплатно\n"
        f"— {PACK_4_PRICE_STARS} ⭐ → {PACK_4_CREDITS} генерации\n"
        f"— {PACK_10_PRICE_STARS} ⭐ → {PACK_10_CREDITS} генераций"
    )
    await update.message.reply_text(text)


async def brief_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    update_user_fields(user_id, current_index=0)

    await update.message.reply_text(
        "Начинаем бриф по интерьеру.\n\n"
        f"{QUESTIONS[0][1]}"
    )


async def photo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    update_user_fields(user_id, waiting_for_photo=True)

    await update.message.reply_text(
        "Отправь фото помещения, планировки или интерьерного референса.\n"
        "Я проанализирую его и сохраню результат в бриф."
    )


async def showbrief_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user_data(update.effective_user.id)

    has_any_answers = any(v.strip() for v in user.answers.values()) if user.answers else False
    if not has_any_answers and not user.photo_analysis:
        await update.message.reply_text(
            "Пока бриф пустой.\n\n"
            f"{build_render_balance_text(user)}"
        )
        return

    text = "Текущий бриф:\n\n" + build_brief_summary(user)

    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    clear_user_data(user_id)
    await update.message.reply_text(
        "Бриф и баланс очищены.\n"
        "Можно начать заново: /brief"
    )


async def idea_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user_data(update.effective_user.id)

    await send_typing(update, ChatAction.TYPING)

    try:
        answer = await asyncio.to_thread(
            generate_text_blocking,
            build_interior_prompt(user),
            "Ты профессиональный дизайнер интерьера. Пиши понятно, структурно и по делу.",
        )

        for i in range(0, len(answer), 4000):
            await update.message.reply_text(answer[i:i + 4000])

    except Exception as e:
        logger.exception("Idea generation failed: %s", e)
        await update.message.reply_text(
            "Не удалось собрать интерьерную концепцию. Проверь API и модель."
        )


async def render_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = get_user_data(user_id)

    if not can_render(user):
        await update.message.reply_text(
            "У тебя закончились генерации.\n\n"
            f"— {PACK_4_PRICE_STARS} ⭐ → {PACK_4_CREDITS} генерации (/buy4)\n"
            f"— {PACK_10_PRICE_STARS} ⭐ → {PACK_10_CREDITS} генераций (/buy10)\n\n"
            "Посмотреть тарифы: /plans"
        )
        return

    await send_typing(update, ChatAction.TYPING)

    try:
        idea_text = await asyncio.to_thread(
            generate_text_blocking,
            build_interior_prompt(user),
            "Ты профессиональный дизайнер интерьера. Пиши структурно.",
        )

        render_prompt = await asyncio.to_thread(
            generate_text_blocking,
            build_render_prompt_request(idea_text),
            "Ты умеешь извлекать только итоговый промпт для генерации изображений.",
        )

        await update.message.reply_text("Генерирую визуализацию интерьера...")
        await send_typing(update, ChatAction.UPLOAD_PHOTO)

        image_bytes = await asyncio.to_thread(generate_image_blocking, render_prompt)

        source, free_left_after, paid_left_after = consume_one_render(user_id)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(image_bytes)
            tmp_path = tmp.name

        try:
            with open(tmp_path, "rb") as img_file:
                used_text = "Списана бесплатная генерация." if source == "free" else "Списана платная генерация."
                await update.message.reply_photo(
                    photo=img_file,
                    caption=(
                        "Визуализация по вашему интерьерному брифу.\n\n"
                        f"{used_text}\n"
                        f"Бесплатных осталось: {free_left_after}\n"
                        f"Платных осталось: {paid_left_after}"
                    ),
                )
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    except Exception as e:
        logger.exception("Render generation failed: %s", e)
        await update.message.reply_text(
            "Не удалось сгенерировать визуализацию. Проверь API, модель изображений и доступность сервиса."
        )


# =========================================================
# БРИФ
# =========================================================
async def handle_brief_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_text: str,
) -> bool:
    user_id = update.effective_user.id
    user = get_user_data(user_id)

    if user.current_index < 0 or user.current_index >= len(QUESTIONS):
        return False

    key, _question = QUESTIONS[user.current_index]
    save_answer(user_id, key, user_text)

    next_index = user.current_index + 1

    if next_index >= len(QUESTIONS):
        update_user_fields(user_id, current_index=-1)
        await update.message.reply_text(
            "Бриф заполнен ✅\n\n"
            "Теперь можешь:\n"
            "/photo — отправить фото для анализа\n"
            "/idea — получить интерьерную концепцию\n"
            "/render — сгенерировать визуализацию\n"
            "/showbrief — посмотреть все ответы"
        )
        return True

    update_user_fields(user_id, current_index=next_index)
    await update.message.reply_text(QUESTIONS[next_index][1])
    return True


# =========================================================
# ФОТО
# =========================================================
async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not update.message.photo:
        await update.message.reply_text("Фото не найдено.")
        return

    await send_typing(update, ChatAction.UPLOAD_PHOTO)

    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
        temp_path = tmp.name

    await tg_file.download_to_drive(temp_path)

    try:
        analysis = await asyncio.to_thread(analyze_image_blocking, temp_path)
        update_user_fields(
            user_id,
            photo_analysis=analysis,
            waiting_for_photo=False,
        )

        await update.message.reply_text(
            "Фото проанализировано ✅\n\n"
            f"{analysis}"
        )

    except Exception as e:
        logger.exception("Photo analysis failed: %s", e)
        await update.message.reply_text(
            "Не удалось проанализировать фото. Проверь vision-модель и API."
        )
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


# =========================================================
# ОБЫЧНЫЙ ЧАТ
# =========================================================
async def handle_free_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_text: str,
) -> None:
    user = get_user_data(update.effective_user.id)

    system_prompt = f"""
Ты — AI-ассистент дизайнер интерьера в Telegram.

Твоя задача:
- помогать по интерьеру,
- учитывать уже заполненный бриф,
- учитывать анализ присланного изображения,
- отвечать по-русски,
- быть полезным, конкретным и понятным.

Сохранённый бриф:
{json.dumps(user.answers, ensure_ascii=False, indent=2)}

Анализ фото:
{user.photo_analysis or "нет"}
""".strip()

    await send_typing(update, ChatAction.TYPING)

    try:
        answer = await asyncio.to_thread(
            generate_text_blocking,
            user_text,
            system_prompt,
        )

        for i in range(0, len(answer), 4000):
            await update.message.reply_text(answer[i:i + 4000])

    except Exception as e:
        logger.exception("Free text generation failed: %s", e)
        await update.message.reply_text("Ошибка при обработке сообщения.")


# =========================================================
# ГЛАВНЫЙ ОБРАБОТЧИК
# =========================================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user_id = update.effective_user.id
    user = get_user_data(user_id)

    if update.message.successful_payment:
        return

    if update.message.photo:
        await handle_photo_message(update, context)
        return

    if not update.message.text:
        await update.message.reply_text("Пожалуйста, отправь текст или изображение.")
        return

    user_text = update.message.text.strip()
    if not user_text:
        await update.message.reply_text("Сообщение пустое.")
        return

    logger.info("Message from user_id=%s text=%s", user_id, user_text[:200])

    handled = await handle_brief_answer(update, context, user_text)
    if handled:
        return

    if user.waiting_for_photo:
        await update.message.reply_text(
            "Я жду изображение. Пожалуйста, отправь фото помещения или референса."
        )
        return

    await handle_free_text(update, context, user_text)


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
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("brief", brief_command))
    application.add_handler(CommandHandler("photo", photo_command))
    application.add_handler(CommandHandler("idea", idea_command))
    application.add_handler(CommandHandler("render", render_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("plans", plans_command))
    application.add_handler(CommandHandler("buy4", buy4_command))
    application.add_handler(CommandHandler("buy10", buy10_command))
    application.add_handler(CommandHandler("showbrief", showbrief_command))
    application.add_handler(CommandHandler("reset", reset_command))

    application.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    application.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback)
    )

    application.add_handler(
        MessageHandler(filters.TEXT | filters.PHOTO, handle_message)
    )

    application.add_error_handler(error_handler)

    logger.info("Bot is starting polling")
    application.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
