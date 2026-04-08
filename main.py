import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)

# =========================
# НАСТРОЙКИ
# =========================
BOT_TOKEN = "8328109350:AAErabgJ8T3p2xwe7d4BUka40A_NMAPevAw"
MANAGER_CHAT_ID = 7135951470  # <-- сюда вставь chat_id менеджера после /myid
DB_PATH = "/var/data/manicure_bot.db"
MSK_TZ = ZoneInfo("Europe/Moscow")
SALON_ADDRESS = "Москва,Электролитный проезд 1б"

# Салон работает с 10:00 до 19:00
WORKING_HOURS = [f"{hour:02d}:00" for hour in range(10, 20)]

router = Router()


# =========================
# СОСТОЯНИЯ
# =========================
class BookingStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_name = State()
    waiting_for_date = State()
    waiting_for_time = State()
    waiting_for_confirmation = State()


# =========================
# БАЗА ДАННЫХ
# =========================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            phone TEXT,
            created_at TEXT NOT NULL
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            client_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            booking_date TEXT NOT NULL,
            booking_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            reminded INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            cancelled_at TEXT
        )
        """)
        await db.commit()


async def save_or_update_user(
    telegram_id: int,
    username: str | None,
    full_name: str | None,
    phone: str | None = None
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO users (telegram_id, username, full_name, phone, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name,
            phone = COALESCE(excluded.phone, users.phone)
        """, (
            telegram_id,
            username,
            full_name,
            phone,
            datetime.now(MSK_TZ).isoformat()
        ))
        await db.commit()


async def get_user_phone(telegram_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT phone FROM users WHERE telegram_id = ?",
            (telegram_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None


async def has_active_booking(telegram_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT 1
            FROM bookings
            WHERE telegram_id = ? AND status = 'active'
            LIMIT 1
        """, (telegram_id,)) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def create_booking(
    telegram_id: int,
    client_name: str,
    phone: str,
    booking_date: str,
    booking_time: str
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO bookings (
            telegram_id, client_name, phone,
            booking_date, booking_time,
            status, reminded, created_at
        )
        VALUES (?, ?, ?, ?, ?, 'active', 0, ?)
        """, (
            telegram_id,
            client_name,
            phone,
            booking_date,
            booking_time,
            datetime.now(MSK_TZ).isoformat()
        ))
        await db.commit()


async def get_active_booking(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, client_name, phone, booking_date, booking_time
            FROM bookings
            WHERE telegram_id = ? AND status = 'active'
            ORDER BY id DESC
            LIMIT 1
        """, (telegram_id,)) as cursor:
            return await cursor.fetchone()


async def cancel_booking(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, client_name, phone, booking_date, booking_time
            FROM bookings
            WHERE telegram_id = ? AND status = 'active'
            ORDER BY id DESC
            LIMIT 1
        """, (telegram_id,)) as cursor:
            booking = await cursor.fetchone()

        if not booking:
            return None

        booking_id = booking[0]
        await db.execute("""
            UPDATE bookings
            SET status = 'cancelled',
                cancelled_at = ?
            WHERE id = ?
        """, (
            datetime.now(MSK_TZ).isoformat(),
            booking_id
        ))
        await db.commit()
        return booking


async def is_slot_taken(booking_date: str, booking_time: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT 1
            FROM bookings
            WHERE booking_date = ?
              AND booking_time = ?
              AND status = 'active'
            LIMIT 1
        """, (booking_date, booking_time)) as cursor:
            row = await cursor.fetchone()
            return row is not None


async def get_taken_times_for_date(booking_date: str) -> set[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT booking_time
            FROM bookings
            WHERE booking_date = ?
              AND status = 'active'
        """, (booking_date,)) as cursor:
            rows = await cursor.fetchall()
            return {row[0] for row in rows}


async def get_bookings_for_reminders():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT id, telegram_id, client_name, booking_date, booking_time
            FROM bookings
            WHERE status = 'active' AND reminded = 0
        """) as cursor:
            return await cursor.fetchall()


async def mark_reminded(booking_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE bookings
            SET reminded = 1
            WHERE id = ?
        """, (booking_id,))
        await db.commit()


# =========================
# КЛАВИАТУРЫ
# =========================
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Записаться на сеанс", callback_data="book_session")],
        [InlineKeyboardButton(text="Моя запись", callback_data="my_booking")],
        [InlineKeyboardButton(text="Отменить сеанс", callback_data="cancel_session")],
    ])


def confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить запись", callback_data="confirm_booking")],
        [InlineKeyboardButton(text="Отменить", callback_data="cancel_flow")],
    ])


def times_keyboard(available_times: list[str]):
    rows = []
    for time_value in available_times:
        rows.append([InlineKeyboardButton(text=time_value, callback_data=f"time_{time_value}")])
    rows.append([InlineKeyboardButton(text="Отменить", callback_data="cancel_flow")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =========================
# УТИЛИТЫ
# =========================
def parse_date_input(text: str) -> str | None:
    text = text.strip()
    now = datetime.now(MSK_TZ)

    for fmt in ("%d.%m.%Y", "%d.%m"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%d.%m":
                parsed = parsed.replace(year=now.year)

            if parsed.date() < now.date() and fmt == "%d.%m":
                parsed = parsed.replace(year=now.year + 1)

            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return None


def booking_datetime_msk(booking_date: str, booking_time: str) -> datetime:
    dt = datetime.strptime(f"{booking_date} {booking_time}", "%Y-%m-%d %H:%M")
    return dt.replace(tzinfo=MSK_TZ)


def format_booking_date(booking_date: str) -> str:
    dt = datetime.strptime(booking_date, "%Y-%m-%d")
    return dt.strftime("%d.%m.%Y")


async def notify_manager(bot: Bot, text: str):
    try:
        await bot.send_message(MANAGER_CHAT_ID, text)
    except Exception as e:
        logging.exception("Не удалось отправить сообщение менеджеру: %s", e)


# =========================
# КОМАНДЫ
# =========================
@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    user = message.from_user
    await save_or_update_user(
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name
    )
    await state.clear()

    text = (
        "Здравствуйте! 👋\n\n"
        "Это бот для записи на маникюр.\n"
        "Вы можете записаться, посмотреть свою запись или отменить её."
    )
    await message.answer(text, reply_markup=main_menu())


@router.message(Command("myid"))
async def myid_handler(message: Message):
    await message.answer(
        "Ваш chat_id:\n"
        f"{message.chat.id}\n\n"
        "Скопируйте это число и вставьте его в код в переменную MANAGER_CHAT_ID."
    )


# =========================
# ОСНОВНОЕ МЕНЮ
# =========================
@router.callback_query(F.data == "book_session")
async def book_session_handler(callback: CallbackQuery, state: FSMContext):
    telegram_id = callback.from_user.id

    if await has_active_booking(telegram_id):
        await callback.message.answer(
            "У вас уже есть активная запись.\n"
            "Сначала отмените её, если хотите записаться заново.",
            reply_markup=main_menu()
        )
        await callback.answer()
        return

    saved_phone = await get_user_phone(telegram_id)
    await state.clear()

    if saved_phone:
        await state.update_data(phone=saved_phone)
        await state.set_state(BookingStates.waiting_for_name)
        await callback.message.answer(
            f"Ваш номер уже сохранён: {saved_phone}\n\n"
            "Теперь напишите ваше имя:"
        )
    else:
        await state.set_state(BookingStates.waiting_for_phone)
        await callback.message.answer("Пожалуйста, введите ваш номер телефона:")

    await callback.answer()


@router.callback_query(F.data == "my_booking")
async def my_booking_handler(callback: CallbackQuery):
    booking = await get_active_booking(callback.from_user.id)
    if not booking:
        await callback.message.answer("У вас нет активной записи.", reply_markup=main_menu())
        await callback.answer()
        return

    _, client_name, phone, booking_date, booking_time = booking
    text = (
        "Ваша текущая запись:\n\n"
        f"Клиент: {client_name}\n"
        f"Телефон: {phone}\n"
        f"Дата: {format_booking_date(booking_date)}\n"
        f"Время: {booking_time}"
    )
    await callback.message.answer(text, reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "cancel_session")
async def cancel_session_handler(callback: CallbackQuery):
    booking = await cancel_booking(callback.from_user.id)
    if not booking:
        await callback.message.answer("У вас нет активной записи для отмены.", reply_markup=main_menu())
        await callback.answer()
        return

    _, client_name, phone, booking_date, booking_time = booking

    cancel_text = (
        "❌ Отмена заказа\n\n"
        f"Клиент: {client_name}\n"
        f"Телефон: {phone}\n"
        f"Дата: {format_booking_date(booking_date)}\n"
        f"Время: {booking_time}"
    )

    await notify_manager(callback.bot, cancel_text)

    await callback.message.answer(
        "Ваша запись отменена. Слот снова свободен.",
        reply_markup=main_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_flow")
async def cancel_flow_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Действие отменено.", reply_markup=main_menu())
    await callback.answer()


# =========================
# ШАГИ ЗАПИСИ
# =========================
@router.message(BookingStates.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip()

    if len(phone) < 6:
        await message.answer("Пожалуйста, введите корректный номер телефона.")
        return

    user = message.from_user
    await save_or_update_user(
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name,
        phone=phone
    )

    await state.update_data(phone=phone)
    await state.set_state(BookingStates.waiting_for_name)
    await message.answer("Теперь напишите ваше имя:")


@router.message(BookingStates.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    client_name = message.text.strip()

    if len(client_name) < 2:
        await message.answer("Пожалуйста, введите корректное имя.")
        return

    await state.update_data(client_name=client_name)
    await state.set_state(BookingStates.waiting_for_date)
    await message.answer(
        "Введите дату записи в формате ДД.ММ или ДД.ММ.ГГГГ\n"
        "Например: 12.04 или 12.04.2026"
    )


@router.message(BookingStates.waiting_for_date)
async def process_date(message: Message, state: FSMContext):
    booking_date = parse_date_input(message.text)

    if not booking_date:
        await message.answer("Неверный формат даты. Пример: 12.04 или 12.04.2026")
        return

    now = datetime.now(MSK_TZ)
    date_obj = datetime.strptime(booking_date, "%Y-%m-%d").date()

    if date_obj < now.date():
        await message.answer("Нельзя выбрать дату в прошлом.")
        return

    taken_times = await get_taken_times_for_date(booking_date)

    available_times = []
    for time_value in WORKING_HOURS:
        slot_dt = booking_datetime_msk(booking_date, time_value)
        if slot_dt <= now:
            continue
        if time_value not in taken_times:
            available_times.append(time_value)

    if not available_times:
        await message.answer(
            "На эту дату свободного времени нет.\n"
            "Пожалуйста, введите другую дату."
        )
        return

    await state.update_data(booking_date=booking_date)
    await state.set_state(BookingStates.waiting_for_time)

    await message.answer(
        f"Свободное время на {format_booking_date(booking_date)}:",
        reply_markup=times_keyboard(available_times)
    )


@router.callback_query(BookingStates.waiting_for_time, F.data.startswith("time_"))
async def process_time(callback: CallbackQuery, state: FSMContext):
    booking_time = callback.data.replace("time_", "", 1)
    data = await state.get_data()
    booking_date = data["booking_date"]

    if await is_slot_taken(booking_date, booking_time):
        await callback.message.answer(
            "К сожалению, это время только что заняли.\n"
            "Пожалуйста, выберите другую дату или время."
        )
        await callback.answer()
        return

    now = datetime.now(MSK_TZ)
    slot_dt = booking_datetime_msk(booking_date, booking_time)
    if slot_dt <= now:
        await callback.message.answer("Нельзя выбрать время в прошлом.")
        await callback.answer()
        return

    await state.update_data(booking_time=booking_time)
    await state.set_state(BookingStates.waiting_for_confirmation)

    text = (
        "Проверьте вашу запись:\n\n"
        f"Услуга: Маникюр\n"
        f"Имя: {data['client_name']}\n"
        f"Телефон: {data['phone']}\n"
        f"Дата: {format_booking_date(booking_date)}\n"
        f"Время: {booking_time}\n\n"
        "Подтвердить запись?"
    )
    await callback.message.answer(text, reply_markup=confirm_keyboard())
    await callback.answer()


@router.callback_query(BookingStates.waiting_for_confirmation, F.data == "confirm_booking")
async def confirm_booking_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    booking_date = data["booking_date"]
    booking_time = data["booking_time"]

    if await is_slot_taken(booking_date, booking_time):
        await callback.message.answer(
            "К сожалению, это время уже занято.\n"
            "Пожалуйста, начните запись заново."
        )
        await state.clear()
        await callback.message.answer("Выберите действие:", reply_markup=main_menu())
        await callback.answer()
        return

    await create_booking(
        telegram_id=callback.from_user.id,
        client_name=data["client_name"],
        phone=data["phone"],
        booking_date=booking_date,
        booking_time=booking_time
    )

    manager_text = (
        "✅ Новая запись\n\n"
        f"Клиент: {data['client_name']}\n"
        f"Телефон: {data['phone']}\n"
        f"Дата: {format_booking_date(booking_date)}\n"
        f"Время: {booking_time}"
    )

    await notify_manager(callback.bot, manager_text)

    await callback.message.answer(
        "Ваша запись подтверждена ✅\n\n"
        f"Дата: {format_booking_date(booking_date)}\n"
        f"Время: {booking_time}\n\n"
        f" Адрес салона: {SALON_ADDRESS}\n\n"
        "Напоминание придёт за 2 часа до начала сеанса.",
        reply_markup=main_menu()
    )
    await state.clear()
    await callback.answer()


# =========================
# НАПОМИНАНИЯ
# =========================
async def reminder_worker(bot: Bot):
    while True:
        try:
            now = datetime.now(MSK_TZ)
            bookings = await get_bookings_for_reminders()

            for booking_id, telegram_id, client_name, booking_date, booking_time in bookings:
                slot_dt = booking_datetime_msk(booking_date, booking_time)
                diff = slot_dt - now

                if timedelta(hours=1, minutes=59) <= diff <= timedelta(hours=2, minutes=1):
                    text = (
                        "🔔 Напоминание о записи\n\n"
                        f"{client_name}, напоминаем, что у вас запись на маникюр через 2 часа.\n"
                        f"Дата: {format_booking_date(booking_date)}\n"
                        f"Время: {booking_time}\n"
                        f" Адрес салона: {SALON_ADDRESS}\n\n"
                    )
                    try:
                        await bot.send_message(telegram_id, text)
                        await mark_reminded(booking_id)
                    except Exception as e:
                        logging.exception("Ошибка отправки напоминания: %s", e)

        except Exception as e:
            logging.exception("Ошибка в reminder_worker: %s", e)

        await asyncio.sleep(30)


# =========================
# ЗАПУСК
# =========================
async def main():
    logging.basicConfig(level=logging.INFO)

    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(router)

    asyncio.create_task(reminder_worker(bot))

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
