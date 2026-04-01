import os
import html
import asyncio
from typing import Dict, Any, List

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    ReplyKeyboardRemove,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage


# =========================
# CONFIG
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN").strip()

if not BOT_TOKEN:
    raise RuntimeError("В .env не найден BOT_TOKEN")


# =========================
# STORAGE
# =========================
# Для простоты храним в памяти.
# Для продакшена лучше Redis/PostgreSQL.
USER_PROJECTS: Dict[int, Dict[str, Any]] = {}


# =========================
# FSM STATES
# =========================
class DesignForm(StatesGroup):
    property_type = State()
    area = State()
    rooms = State()
    style = State()
    fireplace = State()
    furniture = State()
    second_floor = State()
    terrace = State()
    residents = State()
    budget = State()
    done = State()


# =========================
# INIT BOT
# =========================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
app = FastAPI(title="Telegram Home Design Mini App")


# =========================
# HELPERS
# =========================
def yes_no_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def property_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Дом"), KeyboardButton(text="Квартира")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def menu_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Создать дизайн")],
            [
                KeyboardButton(
                    text="Открыть Mini App",
                    web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/app/{user_id}")
                )
            ],
        ],
        resize_keyboard=True,
    )


def normalize_yes_no(text: str) -> str:
    text = text.strip().lower()
    if text == "да":
        return "Да"
    if text == "нет":
        return "Нет"
    return text


def is_yes_no(text: str) -> bool:
    return text.strip().lower() in {"да", "нет"}


def safe(value: Any) -> str:
    return html.escape(str(value))


def placeholder_images(data: Dict[str, Any]) -> List[str]:
    style = data.get("style", "modern")
    property_type = data.get("property_type", "home")
    prompt = f"{property_type} {style}".replace(" ", "+")
    return [
        f"https://placehold.co/900x600?text={prompt}+Exterior",
        f"https://placehold.co/900x600?text={prompt}+Living+Room",
        f"https://placehold.co/900x600?text={prompt}+Kitchen+Bedroom",
    ]


def build_design_concept(data: Dict[str, Any]) -> Dict[str, Any]:
    property_type = data["property_type"]
    area = data["area"]
    rooms = data["rooms"]
    style = data["style"]
    fireplace = data["fireplace"]
    furniture = data["furniture"]
    second_floor = data["second_floor"]
    terrace = data["terrace"]
    residents = data["residents"]
    budget = data["budget"]

    title = f"{property_type.capitalize()} — {style}, {area}"

    summary = (
        f"Проект: {property_type}, площадь {area}, помещения: {rooms}. "
        f"Стиль: {style}. Камин: {fireplace}. Мебель: {furniture}. "
        f"Второй этаж: {second_floor}. Терраса/балкон: {terrace}. "
        f"Для кого: {residents}. Бюджет: {budget}."
    )

    recommendations: List[str] = []

    style_lower = style.lower()

    if "миним" in style_lower:
        recommendations.append("Основа интерьера — чистые линии, спокойные формы и минимум визуального шума.")
        recommendations.append("Подойдут светлые стены, тёплое дерево, встроенные системы хранения и мягкий рассеянный свет.")
    elif "лофт" in style_lower:
        recommendations.append("Основной акцент — фактуры бетона, металла, стекла и более графичная мебель.")
        recommendations.append("Хорошо будут смотреться тёмные акценты, трековый свет и открытые композиции.")
    elif "сканди" in style_lower:
        recommendations.append("Лучший подход — светлая база, натуральные материалы, уютный текстиль и простая эргономика.")
        recommendations.append("Интерьер стоит делать тёплым и функциональным, без перегруза деталями.")
    elif "неокласс" in style_lower:
        recommendations.append("Подойдут симметрия, благородные оттенки, мягкие фактуры и аккуратные декоративные акценты.")
        recommendations.append("Важно сохранить баланс между элегантностью и современным удобством.")
    else:
        recommendations.append("Оптимальный путь — современный удобный интерьер с акцентом на комфорт и визуальную цельность.")
        recommendations.append("Лучше связать все зоны единым набором материалов, света и мебели.")

    if fireplace == "Да":
        recommendations.append("Камин можно сделать главным композиционным центром гостиной или общей зоны.")
    else:
        recommendations.append("Без камина стоит усилить атмосферу через декоративный свет, текстиль и акцентную стену.")

    if furniture == "Да":
        recommendations.append("Поскольку мебель предусмотрена сразу, интерьер стоит проектировать как готовую цельную сцену.")
    else:
        recommendations.append("Так как мебель не обязательна сразу, можно заложить более гибкую базу для постепенного наполнения.")

    if second_floor == "Да":
        recommendations.append("Если есть второй этаж, лестницу лучше сделать выразительным архитектурным элементом проекта.")
    else:
        recommendations.append("При одном уровне стоит уделить больше внимания зонированию и логике проходов.")

    if terrace == "Да":
        recommendations.append("Террасу или балкон хорошо связать с интерьером едиными материалами и общей атмосферой.")
    else:
        recommendations.append("Если внешней зоны нет, акцент можно перенести на более светлый и просторный интерьер внутри.")

    concept_text = (
        f"Этот проект предполагает создание {style.lower()} дизайна для объекта типа «{property_type}» "
        f"площадью {area}. Пространство должно ощущаться удобным для категории жильцов: {residents}. "
        f"Планировочное решение ориентируется на формат помещений «{rooms}», а визуальный язык строится "
        f"вокруг материалов и решений, которые соответствуют заявленному бюджету: {budget}. "
        f"Главная цель — сделать интерьер не просто красивым, а цельным, логичным и визуально убедительным."
    )

    visual_prompt = (
        f"High-quality realistic interior and exterior design concept for a {property_type}, "
        f"area {area}, rooms: {rooms}, style: {style}, fireplace: {fireplace}, "
        f"furnished: {furniture}, second floor: {second_floor}, terrace or balcony: {terrace}, "
        f"for residents: {residents}, budget level: {budget}. Architectural visualization, elegant, realistic lighting."
    )

    return {
        "title": title,
        "summary": summary,
        "concept_text": concept_text,
        "recommendations": recommendations,
        "visual_prompt": visual_prompt,
        "images": placeholder_images(data),
    }


def get_project(user_id: int) -> Dict[str, Any] | None:
    return USER_PROJECTS.get(user_id)


def save_project(user_id: int, answers: Dict[str, Any]) -> Dict[str, Any]:
    result = build_design_concept(answers)
    project = {
        "user_id": user_id,
        "answers": answers,
        "result": result,
    }
    USER_PROJECTS[user_id] = project
    return project


# =========================
# BOT FLOW
# =========================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = (
        "Привет. Я бот для создания дизайн-концепта дома или квартиры.\n\n"
        "Я буду задавать вопросы по одному, а потом соберу результат и открою его в Mini App внутри Telegram.\n\n"
        "Нажми «Создать дизайн»."
    )
    await message.answer(text, reply_markup=menu_keyboard(message.from_user.id))


@dp.message(F.text == "Создать дизайн")
async def start_design(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(DesignForm.property_type)
    await message.answer(
        "Что нужно спроектировать?",
        reply_markup=property_keyboard()
    )


@dp.message(DesignForm.property_type)
async def process_property_type(message: Message, state: FSMContext) -> None:
    text = message.text.strip().lower()
    if text not in {"дом", "квартира"}:
        await message.answer("Пожалуйста, выбери: Дом или Квартира.", reply_markup=property_keyboard())
        return

    await state.update_data(property_type=message.text.strip().capitalize())
    await state.set_state(DesignForm.area)
    await message.answer("Какая площадь объекта?", reply_markup=ReplyKeyboardRemove())


@dp.message(DesignForm.area)
async def process_area(message: Message, state: FSMContext) -> None:
    value = message.text.strip()
    await state.update_data(area=value)
    await state.set_state(DesignForm.rooms)
    await message.answer("Сколько комнат или какое распределение помещений нужно? Например: 2 комнаты / 3 спальни + кухня-гостиная")


@dp.message(DesignForm.rooms)
async def process_rooms(message: Message, state: FSMContext) -> None:
    await state.update_data(rooms=message.text.strip())
    await state.set_state(DesignForm.style)
    await message.answer("Какой стиль нравится? Например: минимализм, современный, лофт, скандинавский, неоклассика")


@dp.message(DesignForm.style)
async def process_style(message: Message, state: FSMContext) -> None:
    await state.update_data(style=message.text.strip())
    await state.set_state(DesignForm.fireplace)
    await message.answer("Нужен ли камин?", reply_markup=yes_no_keyboard())


@dp.message(DesignForm.fireplace)
async def process_fireplace(message: Message, state: FSMContext) -> None:
    if not is_yes_no(message.text):
        await message.answer("Ответь, пожалуйста: Да или Нет.", reply_markup=yes_no_keyboard())
        return

    await state.update_data(fireplace=normalize_yes_no(message.text))
    await state.set_state(DesignForm.furniture)
    await message.answer("Нужно ли сразу учитывать мебель в дизайне?", reply_markup=yes_no_keyboard())


@dp.message(DesignForm.furniture)
async def process_furniture(message: Message, state: FSMContext) -> None:
    if not is_yes_no(message.text):
        await message.answer("Ответь, пожалуйста: Да или Нет.", reply_markup=yes_no_keyboard())
        return

    await state.update_data(furniture=normalize_yes_no(message.text))
    await state.set_state(DesignForm.second_floor)
    await message.answer("Нужен ли второй этаж?", reply_markup=yes_no_keyboard())


@dp.message(DesignForm.second_floor)
async def process_second_floor(message: Message, state: FSMContext) -> None:
    if not is_yes_no(message.text):
        await message.answer("Ответь, пожалуйста: Да или Нет.", reply_markup=yes_no_keyboard())
        return

    await state.update_data(second_floor=normalize_yes_no(message.text))
    await state.set_state(DesignForm.terrace)
    await message.answer("Нужна ли терраса или балкон?", reply_markup=yes_no_keyboard())


@dp.message(DesignForm.terrace)
async def process_terrace(message: Message, state: FSMContext) -> None:
    if not is_yes_no(message.text):
        await message.answer("Ответь, пожалуйста: Да или Нет.", reply_markup=yes_no_keyboard())
        return

    await state.update_data(terrace=normalize_yes_no(message.text))
    await state.set_state(DesignForm.residents)
    await message.answer("Для кого это жильё? Например: один человек, пара, семья с детьми")


@dp.message(DesignForm.residents)
async def process_residents(message: Message, state: FSMContext) -> None:
    await state.update_data(residents=message.text.strip())
    await state.set_state(DesignForm.budget)
    await message.answer("Какой бюджет или уровень бюджета? Например: низкий / средний / высокий / премиум")


@dp.message(DesignForm.budget)
async def process_budget(message: Message, state: FSMContext) -> None:
    await state.update_data(budget=message.text.strip())
    data = await state.get_data()

    project = save_project(message.from_user.id, data)
    await state.set_state(DesignForm.done)

    result = project["result"]
    text = (
        "Готово. Я собрал дизайн-концепт.\n\n"
        f"Название проекта: {result['title']}\n\n"
        "Теперь нажми кнопку ниже, чтобы открыть результат в Mini App."
    )

    await message.answer(
        text,
        reply_markup=menu_keyboard(message.from_user.id)
    )


@dp.message(F.text == "Открыть Mini App")
async def open_app_hint(message: Message) -> None:
    await message.answer(
        "Нажми именно на кнопку «Открыть Mini App» в клавиатуре ниже.",
        reply_markup=menu_keyboard(message.from_user.id)
    )


# =========================
# FASTAPI ROUTES
# =========================
@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return """
   
     
     
       
Telegram Home Design Mini App

       
Откройте приложение через Telegram-бота.

     
   
    """


@app.get("/api/project/{user_id}", response_class=JSONResponse)
async def api_project(user_id: int):
    project = get_project(user_id)
    if not project:
        return JSONResponse(
            {
                "ok": False,
                "error": "Проект не найден. Сначала пройдите опрос в боте."
            },
            status_code=404
        )

    return {
        "ok": True,
        "project": project
    }


@app.get("/app/{user_id}", response_class=HTMLResponse)
async def mini_app(user_id: int) -> str:
    project = get_project(user_id)

    if not project:
        return f"""
       
         
           
           
           
           
         
         
           
             
Проект не найден

             
Сначала пройдите опрос в боте, потом снова откройте Mini App.

           
         
       
        """

    answers = project["answers"]
    result = project["result"]

    recommendations_html = "".join(
        f"
{safe(item)}
" for item in result["recommendations"]
    )

    images_html = "".join(
        f"""
       
            design image
       
        """
        for url in result["images"]
    )

    answers_html = f"""
   
     
Тип: {safe(answers["property_type"])}
     
Площадь: {safe(answers["area"])}
     
Комнаты: {safe(answers["rooms"])}
     
Стиль: {safe(answers["style"])}
     
Камин: {safe(answers["fireplace"])}
     
Мебель: {safe(answers["furniture"])}
     
Второй этаж: {safe(answers["second_floor"])}
     
Терраса/балкон: {safe(answers["terrace"])}
     
Для кого: {safe(answers["residents"])}
     
Бюджет: {safe(answers["budget"])}
   
    """

    return f"""
   
   
   
     
     
     
     
     
   
   
     
       
         
{safe(result["title"])}
         
{safe(result["summary"])}

         
            Показать сообщение в Telegram
            Обновить
         
       

       
         
Параметры проекта

          {answers_html}
       

       
         
Дизайн-концепция

         
{safe(result["concept_text"])}
       

       
         
Рекомендации

         
            {recommendations_html}
         
       

       
         
Изображения проекта

         
            {images_html}
         
       

       
         
AI prompt для дальнейшей генерации

         
{safe(result["visual_prompt"])}
       

       
          Mini App открыт внутри Telegram. Позже сюда можно подключить реальную AI-генерацию картинок и 3D.
       
     

     
   
   
    """


# =========================
# RUNNERS
# =========================
async def run_bot() -> None:
    await dp.start_polling(bot)


async def run_web() -> None:
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8080,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    await asyncio.gather(
        run_web(),
        run_bot(),
    )


if __name__ == "__main__":
    asyncio.run(main())
