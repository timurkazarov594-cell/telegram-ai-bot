if uid not in db["users"]:
        db["users"][uid] = {
            "paid_generations": 0,
            "daily_text_count": 0,
            "last_reset": today_msk_str(),
        }

    current = int(db["users"][uid].get("paid_generations", 0))
    if current <= 0:
        save_db(db)
        return False

    db["users"][uid]["paid_generations"] = current - 1
    save_db(db)
    return True


def get_daily_text_left(user_id: int) -> int:
    user = reset_daily_if_needed(user_id)
    used = int(user.get("daily_text_count", 0))
    left = DAILY_TEXT_LIMIT - used
    return max(0, left)


def can_send_text(user_id: int) -> bool:
    return get_daily_text_left(user_id) > 0


def increment_text_count(user_id: int) -> int:
    db = load_db()
    uid = str(user_id)

    if uid not in db["users"]:
        db["users"][uid] = {
            "paid_generations": 0,
            "daily_text_count": 0,
            "last_reset": today_msk_str(),
        }

    # Перед увеличением проверяем, не наступил ли новый день по МСК
    today = today_msk_str()
    if db["users"][uid].get("last_reset") != today:
        db["users"][uid]["daily_text_count"] = 0
        db["users"][uid]["last_reset"] = today

    db["users"][uid]["daily_text_count"] += 1
    save_db(db)
    return int(db["users"][uid]["daily_text_count"])


# --------------------------------
# УТИЛИТЫ
# --------------------------------
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

TOPUP_TEXT_TRIGGERS = [
    "пополнить",
    "купить",
    "купить генерации",
    "тариф",
    "тарифы",
]


def is_image_request(text: str) -> bool:
    text = text.lower().strip()
    return any(trigger in text for trigger in IMAGE_TRIGGERS)


def is_topup_text_request(text: str) -> bool:
    text = text.lower().strip()
    return text in TOPUP_TEXT_TRIGGERS


async def send_typing(update: Update, action=ChatAction.TYPING):
    if update.effective_chat:
        await update.effective_chat.send_action(action=action)


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
            logger.exception("Ошибка при разборе ответа изображения")

    return None


def build_buy_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("150 ⭐ — 4 генерации", callback_data="buy_4")],
            [InlineKeyboardButton("300 ⭐ — 10 генераций", callback_data="buy_10")],
            [InlineKeyboardButton("800 ⭐ — 25 генераций", callback_data="buy_25")],
        ]
    )


# --------------------------------
# OPENAI
# --------------------------------
def generate_text_blocking(user_text: str) -> str:
    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты полезный Telegram-бот. "
                    "Отвечай кратко, ясно и по делу."
                ),
            },
            {"role": "user", "content": user_text},
        ],
    )

    answer = response.choices[0].message.content
    if not answer:
        return "Не удалось получить ответ."
    return answer.strip()


def generate_image_blocking(prompt: str) -> bytes:
    response = client.images.generate(
        model=IMAGE_MODEL,
        prompt=prompt,
        size="1024x1024",
    )

    image_b64 = extract_image_b64(response)
    if not image_b64:raise RuntimeError("API не вернул изображение")

    return base64.b64decode(image_b64)


# --------------------------------
# КОМАНДЫ В МЕНЮ
# --------------------------------
async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Запуск бота"),
            BotCommand("popolnit", "Купить генерации"),
            BotCommand("balance", "Мой баланс"),
            BotCommand("limit", "Лимит на сегодня"),
            BotCommand("help", "Помощь"),
        ]
    )
    logger.info("Команды бота установлены")


# --------------------------------
# UI
# --------------------------------
async def send_topup_menu(update: Update, balance: int) -> None:
    text = (
        f"На вашем балансе сейчас {balance} генераций.\n\n"
        "Выберите пакет ниже 👇"
    )
    await update.message.reply_text(
        text,
        reply_markup=build_buy_keyboard(),
    )


# --------------------------------
# КОМАНДЫ
# --------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = get_balance(user_id)
    daily_left = get_daily_text_left(user_id)

    text = (
        "Привет!\n\n"
        "Я умею:\n"
        "- отвечать на текстовые вопросы\n"
        "- генерировать изображения\n\n"
        "Для генерации изображений нужны генерации на балансе.\n"
        f"Сейчас у вас: {balance} генераций.\n"
        f"Осталось текстовых запросов сегодня: {daily_left}/{DAILY_TEXT_LIMIT}\n\n"
        "Команды:\n"
        "/popolnit — купить генерации\n"
        "/balance — показать баланс генераций\n"
        "/limit — показать дневной лимит\n"
        "/help — помощь"
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = get_balance(user_id)
    daily_left = get_daily_text_left(user_id)

    text = (
        "Команды:\n"
        "/start — запуск\n"
        "/help — помощь\n"
        "/balance — показать баланс генераций\n"
        "/limit — показать лимит на сегодня\n"
        "/popolnit — купить генерации\n\n"
        f"Сейчас на балансе: {balance} генераций.\n"
        f"Осталось текстовых запросов сегодня: {daily_left}/{DAILY_TEXT_LIMIT}\n\n"
        "Пример для картинки:\n"
        '"Сгенерируй картинку города будущего ночью"'
    )
    await update.message.reply_text(text)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balance = get_balance(update.effective_user.id)
    await update.message.reply_text(
        f"На вашем балансе {balance} генераций."
    )


async def limit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    daily_left = get_daily_text_left(user_id)
    await update.message.reply_text(
        "Лимит текстовых запросов обновляется каждый день по МСК.\n"
        f"Сегодня осталось: {daily_left}/{DAILY_TEXT_LIMIT}."
    )


async def popolnit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    balance = get_balance(update.effective_user.id)
    await send_topup_menu(update, balance)


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
        description = "Покупка 4 генераций изображений"
    elif query.data == "buy_10":
        title = "10 генераций"
        payload = "buy_10"
        price = 300
        description = "Покупка 10 генераций изображений"
    elif query.data == "buy_25":
        title = "25 генераций"
        payload = "buy_25"
        price = 800
        description = "Покупка 25 генераций изображений"
    else:
        await query.message.reply_text("Неизвестный пакет.")
        return

    logger.info(user_text = update.message.text.strip()
    if not user_text:
        return

    logger.info(
        "Message from user_id=%s text=%s",
        update.effective_user.id if update.effective_user else "unknown",
        user_text[:200],
    )

    if is_topup_text_request(user_text):
        balance = get_balance(update.effective_user.id)
        await send_topup_menu(update, balance)
        return

    if is_image_request(user_text):
        await handle_image_request(update, user_text)
        return

    await handle_text_request(update, user_text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled error: %s", context.error)


# --------------------------------
# MAIN
# --------------------------------
def main():
    logger.info("Building Telegram application")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(CommandHandler("limit", limit_command))
    app.add_handler(CommandHandler("popolnit", popolnit_command))

    app.add_handler(CallbackQueryHandler(buy_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)

    logger.info("Bot is starting polling")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
