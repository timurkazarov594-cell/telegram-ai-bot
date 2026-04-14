import asyncio
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.utils.deep_linking import create_start_link
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8577551279:AAFlCR9UyEi1OzZ5jV6PWrHbtbC-3mJReRg").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "Agentanonimspecial_bot").strip().removeprefix("@")
DB_PATH = os.getenv("DB_PATH", "anon_hint_bot.db")
HINT_PRICE_STARS = int(os.getenv("HINT_PRICE_STARS", "50"))

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN")


class SendFlow(StatesGroup):
    waiting_recipient = State()
    waiting_initial = State()
    waiting_gender = State()
    waiting_zodiac = State()
    waiting_text = State()


@dataclass
class UserRec:
    id: int
    tg_id: int
    username: Optional[str]
    first_name: Optional[str]


class Database:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    @contextmanager
    def conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self.conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_id INTEGER NOT NULL UNIQUE,
                    username TEXT,
                    first_name TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS anon_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_user_id INTEGER NOT NULL,
                    recipient_user_id INTEGER NOT NULL,
                    initial TEXT NOT NULL,
                    gender TEXT NOT NULL,
                    zodiac TEXT NOT NULL,
                    body TEXT NOT NULL,
                    recipient_delivery_message_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(sender_user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY(recipient_user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS hint_unlocks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    anon_message_id INTEGER NOT NULL,
                    buyer_user_id INTEGER NOT NULL,
                    payment_charge_id TEXT,
                    payload TEXT NOT NULL UNIQUE,
                    price_stars INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    paid_at TEXT,
                    FOREIGN KEY(anon_message_id) REFERENCES anon_messages(id) ON DELETE CASCADE,
                    FOREIGN KEY(buyer_user_id) REFERENCES users(id) ON DELETE CASCADE,
                    UNIQUE(anon_message_id, buyer_user_id)
                );
                """
            )

    def upsert_user(self, tg_id: int, username: Optional[str], first_name: Optional[str]) -> int:
        with self.conn() as conn:
            row = conn.execute("SELECT id FROM users WHERE tg_id = ?", (tg_id,)).fetchone()
            now = datetime.utcnow().isoformat()
            if row:
                conn.execute(
                    "UPDATE users SET username = ?, first_name = ?, updated_at = ? WHERE tg_id = ?",
                    (username, first_name, now, tg_id),
                )
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO users (tg_id, username, first_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (tg_id, username, first_name, now, now),
            )
            return int(cur.lastrowid)

    def get_user_by_tg_id(self, tg_id: int) -> Optional[sqlite3.Row]:
        with self.conn() as conn:
            return conn.execute("SELECT * FROM users WHERE tg_id = ?", (tg_id,)).fetchone()

    def get_user_by_username(self, username: str) -> Optional[sqlite3.Row]:
        username = username.removeprefix("@").lower()
        with self.conn() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE lower(username) = ?",
                (username,),
            ).fetchone()

    def get_user_by_id(self, user_id: int) -> Optional[sqlite3.Row]:
        with self.conn() as conn:
            return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def create_message(self, sender_user_id: int, recipient_user_id: int, initial: str, gender: str, zodiac: str, body: str) -> int:
        with self.conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO anon_messages (sender_user_id, recipient_user_id, initial, gender, zodiac, body)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sender_user_id, recipient_user_id, initial, gender, zodiac, body),
            )
            return int(cur.lastrowid)

    def set_delivery_message_id(self, anon_message_id: int, message_id: int):
        with self.conn() as conn:
            conn.execute(
                "UPDATE anon_messages SET recipient_delivery_message_id = ? WHERE id = ?",
                (message_id, anon_message_id),
            )

    def get_message(self, anon_message_id: int) -> Optional[sqlite3.Row]:
        with self.conn() as conn:
            return conn.execute(
                """
                SELECT m.*, su.tg_id AS sender_tg_id, ru.tg_id AS recipient_tg_id
                FROM anon_messages m
                JOIN users su ON su.id = m.sender_user_id
                JOIN users ru ON ru.id = m.recipient_user_id
                WHERE m.id = ?
                """,
                (anon_message_id,),
            ).fetchone()

    def list_recent_inbox(self, recipient_user_id: int, limit: int = 10):
        with self.conn() as conn:
            return conn.execute(
                "SELECT * FROM anon_messages WHERE recipient_user_id = ? ORDER BY id DESC LIMIT ?",
                (recipient_user_id, limit),
            ).fetchall()

    def ensure_unlock_invoice(self, anon_message_id: int, buyer_user_id: int, price_stars: int) -> str:
        payload = f"hint:{anon_message_id}:{buyer_user_id}"
        with self.conn() as conn:
            row = conn.execute(
                "SELECT payload FROM hint_unlocks WHERE anon_message_id = ? AND buyer_user_id = ?",
                (anon_message_id, buyer_user_id),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE hint_unlocks SET price_stars = ?, payload = ? WHERE anon_message_id = ? AND buyer_user_id = ?",
                    (price_stars, payload, anon_message_id, buyer_user_id),
                )
                return payload
            conn.execute(
                """
                INSERT INTO hint_unlocks (anon_message_id, buyer_user_id, payload, price_stars, status)
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (anon_message_id, buyer_user_id, payload, price_stars),
            )
            return payload

    def get_unlock_by_payload(self, payload: str) -> Optional[sqlite3.Row]:
        with self.conn() as conn:
            return conn.execute("SELECT * FROM hint_unlocks WHERE payload = ?", (payload,)).fetchone()

    def is_unlocked(self, anon_message_id: int, buyer_user_id: int) -> bool:
        with self.conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM hint_unlocks WHERE anon_message_id = ? AND buyer_user_id = ? AND status = 'paid'",
                (anon_message_id, buyer_user_id),
            ).fetchone()
            return row is not None

    def mark_unlock_paid(self, payload: str, charge_id: str):
        with self.conn() as conn:
            row = conn.execute("SELECT status FROM hint_unlocks WHERE payload = ?", (payload,)).fetchone()
            if not row:
                raise ValueError("Платёж не найден")
            if row["status"] == "paid":
                return
            conn.execute(
                "UPDATE hint_unlocks SET status = 'paid', payment_charge_id = ?, paid_at = ? WHERE payload = ?",
                (charge_id, datetime.utcnow().isoformat(), payload),
            )


db = Database(DB_PATH)
router = Router()
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)


# ---------- keyboards ----------
def main_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="🔗 Получить свою ссылку")
    kb.button(text="📨 Отправить анонимку")
    kb.button(text="📥 Мои сообщения")
    kb.button(text="💎 Premium")
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)


def inbox_actions_kb(anon_message_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"⭐ Купить подсказки — {HINT_PRICE_STARS}", callback_data=f"buy_hint:{anon_message_id}")
    return kb.as_markup()


def after_paid_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Открыть входящие", callback_data="open_inbox")
    return kb.as_markup()


# ---------- helpers ----------
async def register_user(message: Message) -> int:
    return db.upsert_user(
        tg_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )


async def make_personal_link(user_id: int) -> str:
    if not BOT_USERNAME:
        return "Сначала задай BOT_USERNAME в окружении, чтобы личные ссылки работали."
    payload = f"u{user_id}"
    return await create_start_link(bot, payload, encode=True)


def format_delivery_text(body: str) -> str:
    return (
        "<b>Тебе пришло новое анонимное сообщение 💌</b>\n\n"
        f"{body}"
    )


def format_hints(msg_row: sqlite3.Row) -> str:
    return (
        "<b>Подсказки открыты ✨</b>\n\n"
        f"• Первая буква имени: <b>{msg_row['initial']}</b>\n"
        f"• Пол: <b>{msg_row['gender']}</b>\n"
        f"• Знак зодиака: <b>{msg_row['zodiac']}</b>"
    )


async def safe_send_message(chat_id: int, text: str, reply_markup=None) -> Optional[Message]:
    try:
        return await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as e:
        logger.exception("Не удалось отправить сообщение chat_id=%s: %s", chat_id, e)
        return None


async def deliver_anon_message(anon_message_id: int):
    msg_row = db.get_message(anon_message_id)
    if not msg_row:
        return
    sent = await safe_send_message(
        msg_row["recipient_tg_id"],
        format_delivery_text(msg_row["body"]),
        reply_markup=inbox_actions_kb(anon_message_id),
    )
    if sent:
        db.set_delivery_message_id(anon_message_id, sent.message_id)


async def show_inbox(chat_id: int, recipient_db_user_id: int):
    rows = db.list_recent_inbox(recipient_db_user_id)
    if not rows:
        await bot.send_message(chat_id, "У тебя пока нет анонимных сообщений.", reply_markup=main_kb())
        return
    await bot.send_message(chat_id, f"<b>Последние сообщения: {len(rows)}</b>")
    for row in rows:
        text = format_delivery_text(row["body"])
        if db.is_unlocked(row["id"], recipient_db_user_id):
            text += "\n\n" + format_hints(row)
            await bot.send_message(chat_id, text)
        else:
            await bot.send_message(chat_id, text, reply_markup=inbox_actions_kb(row["id"]))


async def set_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="help", description="Помощь и как всё работает"),
        BotCommand(command="link", description="Получить свою ссылку"),
        BotCommand(command="send", description="Отправить анонимное сообщение"),
        BotCommand(command="inbox", description="Мои сообщения"),
        BotCommand(command="premium", description="Что даёт Premium"),
    ]
    await bot.set_my_commands(commands)


# ---------- handlers ----------
@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    db_user_id = await register_user(message)
    await state.clear()

    payload = command.args or ""
    if payload.startswith("u") and payload[1:].isdigit():
        recipient_id = int(payload[1:])
        if recipient_id == db_user_id:
            await message.answer("Это твоя ссылка. Отправь её друзьям, чтобы они писали тебе анонимно 👇", reply_markup=main_kb())
            link = await make_personal_link(db_user_id)
            await message.answer(link)
            return
        recipient = db.get_user_by_id(recipient_id)
        if not recipient:
            await message.answer(
                "Эта ссылка пока неактивна. Попроси человека сначала запустить бота.",
                reply_markup=main_kb(),
            )
            return
        await state.update_data(recipient_user_id=recipient_id)
        await state.set_state(SendFlow.waiting_initial)
        await message.answer(
            "Ты отправляешь <b>анонимное сообщение</b>.\n\n"
            "Напиши <b>первую букву своего имени</b>.",
        )
        return

    await message.answer(
        "<b>Анонимка с подсказками</b>\n\n"
        "• Получи свою ссылку\n"
        "• Делись ею с друзьями\n"
        "• Получай анонимные сообщения\n"
        f"• Открывай подсказки за <b>{HINT_PRICE_STARS} ⭐</b>\n\n"
        "Выбери действие ниже.",
        reply_markup=main_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await register_user(message)
    await message.answer(
        "<b>Как это работает</b>\n\n"
        "1. Нажми «Получить свою ссылку».\n"
        "2. Отправь её друзьям или в сторис.\n"
        "3. Тебе будут приходить анонимные сообщения.\n"
        f"4. Подсказки к каждому сообщению открываются за <b>{HINT_PRICE_STARS} ⭐</b>.\n\n"
        "Есть и ручная отправка по username, но ссылка работает лучше.",
        reply_markup=main_kb(),
    )


@router.message(Command("link"))
@router.message(F.text == "🔗 Получить свою ссылку")
async def cmd_link(message: Message):
    db_user_id = await register_user(message)
    link = await make_personal_link(db_user_id)
    await message.answer(
        "<b>Твоя ссылка для анонимных сообщений:</b>\n\n"
        f"{link}\n\n"
        "Отправь её друзьям, чтобы они могли писать тебе анонимно.",
        reply_markup=main_kb(),
    )


@router.message(Command("premium"))
@router.message(F.text == "💎 Premium")
async def cmd_premium(message: Message):
    await register_user(message)
    await message.answer(
        "<b>Premium</b>\n\n"
        f"Сейчас основная платная функция — открыть подсказки к сообщению за <b>{HINT_PRICE_STARS} ⭐</b>.\n"
        "Позже сюда можно добавить подписку, скидки и пакетное открытие подсказок.",
        reply_markup=main_kb(),
    )


@router.message(Command("inbox"))
@router.message(F.text == "📥 Мои сообщения")
async def cmd_inbox(message: Message):
    db_user_id = await register_user(message)
    await show_inbox(message.chat.id, db_user_id)


@router.callback_query(F.data == "open_inbox")
async def cb_open_inbox(callback: CallbackQuery):
    db_user_id = db.upsert_user(
        tg_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
    )
    await callback.answer()
    await show_inbox(callback.message.chat.id, db_user_id)


@router.message(Command("send"))
@router.message(F.text == "📨 Отправить анонимку")
async def cmd_send(message: Message, state: FSMContext):
    await register_user(message)
    await state.clear()
    await state.set_state(SendFlow.waiting_recipient)
    await message.answer(
        "Введи <b>@username</b> получателя.\n\n"
        "Важно: получатель должен хотя бы один раз запустить этого бота.\n"
        "Либо используй его личную ссылку — это надёжнее.",
    )


@router.message(SendFlow.waiting_recipient)
async def flow_recipient(message: Message, state: FSMContext):
    await register_user(message)
    text = (message.text or "").strip()
    if not text.startswith("@"):
        await message.answer("Нужен именно @username, например: <code>@ivan_123</code>")
        return
    recipient = db.get_user_by_username(text)
    if not recipient:
        await message.answer(
            "Я не нашёл этого пользователя среди тех, кто уже запускал бота.\n"
            "Попроси его сначала нажать /start или отправь ему свою ссылку.",
        )
        return
    sender = db.get_user_by_tg_id(message.from_user.id)
    if sender and int(recipient["id"]) == int(sender["id"]):
        await message.answer("Нельзя отправить анонимку самому себе.")
        return

    await state.update_data(recipient_user_id=int(recipient["id"]))
    await state.set_state(SendFlow.waiting_initial)
    await message.answer("Напиши <b>первую букву своего имени</b>.")


@router.message(SendFlow.waiting_initial)
async def flow_initial(message: Message, state: FSMContext):
    await register_user(message)
    text = (message.text or "").strip()
    if len(text) != 1:
        await message.answer("Нужна только <b>одна буква</b>.")
        return
    await state.update_data(initial=text.upper())
    await state.set_state(SendFlow.waiting_gender)
    await message.answer("Напиши свой <b>пол</b>. Например: мужской / женский.")


@router.message(SendFlow.waiting_gender)
async def flow_gender(message: Message, state: FSMContext):
    await register_user(message)
    text = (message.text or "").strip()
    if len(text) < 2:
        await message.answer("Напиши пол нормально, например: <b>мужской</b> или <b>женский</b>.")
        return
    await state.update_data(gender=text)
    await state.set_state(SendFlow.waiting_zodiac)
    await message.answer("Теперь напиши свой <b>знак зодиака</b>.")


@router.message(SendFlow.waiting_zodiac)
async def flow_zodiac(message: Message, state: FSMContext):
    await register_user(message)
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.answer("Напиши знак зодиака нормально, например: <b>Лев</b>.")
        return
    await state.update_data(zodiac=text)
    await state.set_state(SendFlow.waiting_text)
    await message.answer("И теперь отправь <b>само анонимное сообщение</b>.")


@router.message(SendFlow.waiting_text)
async def flow_text(message: Message, state: FSMContext):
    sender_id = await register_user(message)
    text = (message.text or "").strip()
    if len(text) < 1:
        await message.answer("Сообщение не может быть пустым.")
        return

    data = await state.get_data()
    recipient_user_id = data.get("recipient_user_id")
    if not recipient_user_id:
        await state.clear()
        await message.answer("Сессия сбилась. Начни заново: /send", reply_markup=main_kb())
        return

    anon_message_id = db.create_message(
        sender_user_id=sender_id,
        recipient_user_id=int(recipient_user_id),
        initial=data["initial"],
        gender=data["gender"],
        zodiac=data["zodiac"],
        body=text,
    )
    await state.clear()
    await deliver_anon_message(anon_message_id)
    await message.answer("Анонимное сообщение отправлено ✅", reply_markup=main_kb())


@router.callback_query(F.data.startswith("buy_hint:"))
async def cb_buy_hint(callback: CallbackQuery):
    db_user_id = db.upsert_user(
        tg_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
    )
    anon_message_id = int(callback.data.split(":", 1)[1])
    msg_row = db.get_message(anon_message_id)
    if not msg_row:
        await callback.answer("Сообщение не найдено", show_alert=True)
        return
    if int(msg_row["recipient_user_id"]) != db_user_id:
        await callback.answer("Это не твоё сообщение", show_alert=True)
        return
    if db.is_unlocked(anon_message_id, db_user_id):
        await callback.answer("Подсказки уже открыты")
        await callback.message.answer(format_hints(msg_row), reply_markup=after_paid_kb())
        return

    payload = db.ensure_unlock_invoice(anon_message_id, db_user_id, HINT_PRICE_STARS)
    prices = [LabeledPrice(label="Подсказки к анонимному сообщению", amount=HINT_PRICE_STARS)]
    await callback.answer()
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="Подсказки к анонимному сообщению",
        description="Откроются: первая буква имени, пол и знак зодиака отправителя.",
        payload=payload,
        currency="XTR",
        prices=prices,
    )


@router.pre_checkout_query()
async def pre_checkout(pre_checkout_query: PreCheckoutQuery):
    payload = pre_checkout_query.invoice_payload
    unlock = db.get_unlock_by_payload(payload)
    if not unlock:
        await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=False, error_message="Платёж не найден")
        return
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    await register_user(message)
    payload = message.successful_payment.invoice_payload
    unlock = db.get_unlock_by_payload(payload)
    if not unlock:
        await message.answer("Платёж прошёл, но запись не найдена. Напиши в поддержку.")
        return

    charge_id = message.successful_payment.telegram_payment_charge_id
    try:
        db.mark_unlock_paid(payload, charge_id)
    except Exception as e:
        logger.exception("Ошибка при сохранении платежа: %s", e)
        await message.answer(
            "Платёж прошёл, но произошла ошибка при открытии подсказок. Ничего не потеряется — просто нажми ещё раз «Купить подсказки», повторно деньги не спишутся за уже открытый доступ.",
        )
        return

    msg_row = db.get_message(int(unlock["anon_message_id"]))
    if not msg_row:
        await message.answer("Платёж принят, но сообщение не найдено.")
        return

    await message.answer(
        format_hints(msg_row),
        reply_markup=after_paid_kb(),
    )


async def on_startup():
    await set_commands(bot)
    logger.info("Бот запущен")


async def main():
    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
