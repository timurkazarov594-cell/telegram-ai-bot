import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo
from aiogram.filters import CommandStart
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI()


# ===== MINI APP HTML =====
@app.get("/")
async def index():
    return HTMLResponse("""
   
   
   
   
   
       
🏡 Дизайн готов

       
Современный стиль, светлые тона, минимализм

   
   
    """)


# ===== FSM =====
class Form(StatesGroup):
    type = State()
    area = State()
    style = State()


def webapp_kb(url):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть дизайн", web_app=WebAppInfo(url=url))]],
        resize_keyboard=True
    )


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.set_state(Form.type)
    await message.answer("Дом или квартира?")


@dp.message(Form.type)
async def step1(message: Message, state: FSMContext):
    await state.update_data(type=message.text)
    await state.set_state(Form.area)
    await message.answer("Площадь?")


@dp.message(Form.area)
async def step2(message: Message, state: FSMContext):
    await state.update_data(area=message.text)
    await state.set_state(Form.style)
    await message.answer("Стиль?")


@dp.message(Form.style)
async def finish(message: Message, state: FSMContext):
    data = await state.get_data()

    text = f"""
Тип: {data['type']}
Площадь: {data['area']}
Стиль: {message.text}

Генерация дизайна...
"""

    # 👇 ВАЖНО: вставишь сюда Render URL позже
    url = "https://https://telegram-ai-bot-cwqi.onrender.com.onrender.com"

    await message.answer(text)
    await message.answer("Открыть дизайн:", reply_markup=webapp_kb(url))
    await state.clear()


# ===== RUN =====
async def start_bot():
    await dp.start_polling(bot)


def run():
    import threading
    threading.Thread(target=lambda: asyncio.run(start_bot())).start()
    uvicorn.run(app, host="0.0.0.0", port=10000)


if __name__ == "__main__":
    run()
