import os
import asyncio
import json
import random
from starlette.applications import Starlette
from starlette.responses import Response, PlainTextResponse
from starlette.requests import Request
from starlette.routing import Route
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler

# ========== НАСТРОЙКИ ==========
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
URL = os.environ.get("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")
PORT = int(os.getenv("PORT", 8000))

WORDS_FILE = "words.json"
PROGRESS_FILE = "progress.json"

# Загружаем категории
with open(WORDS_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)
CATEGORIES = data["categories"]          # dict: key -> {"name": str, "words": list}
CATEGORY_KEYS = list(CATEGORIES.keys())
DEFAULT_CATEGORY = "travel"

# Состояния для ConversationHandler (оставлен для /set_count, но можно не использовать)
COUNT_INPUT = 1

# ========== ФУНКЦИИ ПРОГРЕССА ==========
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_progress(progress):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)

def get_user_progress(user_id: str):
    """Возвращает прогресс пользователя: { category_key: {"used": [индексы], "last": [объекты слов]}, "current_category": str, "words_per_day": int }"""
    prog = load_progress()
    return prog.get(user_id, {})

def set_user_progress(user_id: str, category_progress):
    prog = load_progress()
    prog[user_id] = category_progress
    save_progress(prog)

def get_category_progress(user_id: str, cat_key: str):
    up = get_user_progress(user_id)
    return up.get(cat_key, {"used": [], "last": []})

def set_category_progress(user_id: str, cat_key: str, cat_prog):
    up = get_user_progress(user_id)
    up[cat_key] = cat_prog
    set_user_progress(user_id, up)

def reset_category_progress(user_id: str, cat_key: str):
    set_category_progress(user_id, cat_key, {"used": [], "last": []})

def reset_all_progress(user_id: str):
    set_user_progress(user_id, {})

def get_unused_indices(used, total):
    all_indices = set(range(total))
    used_set = set(used)
    return list(all_indices - used_set)

def format_word(word_obj):
    return f"**{word_obj['word']}**    {word_obj['transcription']}    \"{word_obj['pronunciation']}\"    {word_obj['translation']}"

# ========== НАСТРОЙКИ КОЛИЧЕСТВА СЛОВ ==========
def get_user_words_per_day(user_id: str) -> int:
    up = get_user_progress(user_id)
    return up.get("words_per_day", 5)

def set_user_words_per_day(user_id: str, count: int):
    up = get_user_progress(user_id)
    up["words_per_day"] = count
    set_user_progress(user_id, up)

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📚 Слова на сегодня"), KeyboardButton("🔄 Повторить")],
        [KeyboardButton("📊 Прогресс"), KeyboardButton("🗑 Сбросить прогресс")],
        [KeyboardButton("🎯 Выбрать тему"), KeyboardButton("⚙️ Настройки")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_category_buttons(user_id: str):
    """Создаёт инлайн-кнопки с названиями категорий и прогрессом в каждой."""
    buttons = []
    user_progress = get_user_progress(user_id)
    for key, info in CATEGORIES.items():
        cat_prog = user_progress.get(key, {"used": []})
        done = len(cat_prog["used"])
        total = len(info["words"])
        text = f"{info['name']} ({done}/{total})"
        buttons.append([InlineKeyboardButton(text, callback_data=f"cat_{key}")])
    return InlineKeyboardMarkup(buttons)

# ========== ОБРАБОТЧИКИ СООБЩЕНИЙ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Устанавливаем текущую категорию в контексте (храним в памяти)
    if "current_category" not in context.user_data:
        context.user_data["current_category"] = DEFAULT_CATEGORY
    # Также сохраняем текущую категорию в прогресс (для рассылки, если добавим)
    up = get_user_progress(user_id)
    if "current_category" not in up:
        up["current_category"] = DEFAULT_CATEGORY
        set_user_progress(user_id, up)
    await update.message.reply_text(
        f"👋 Привет! Я помогу выучить английские слова по темам.\n\n"
        f"Текущая тема: *{CATEGORIES[context.user_data['current_category']]['name']}*\n\n"
        f"Количество слов в день: *{get_user_words_per_day(user_id)}*\n\n"
        f"Нажимай кнопки:",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = str(update.effective_user.id)

    if text == "🎯 Выбрать тему":
        await update.message.reply_text(
            "Выберите тему:",
            reply_markup=get_category_buttons(user_id)
        )
        return

    if text == "⚙️ Настройки":
        await update.message.reply_text(
            "⚙️ *Настройки*\n\n"
            f"• Количество слов в день: *{get_user_words_per_day(user_id)}*\n\n"
            "Чтобы изменить, просто напишите число от 1 до 10.\n"
            "Например: `5` или `10`.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
        return

    # Проверка на ввод числа для изменения количества слов
    if text.isdigit():
        num = int(text)
        if 1 <= num <= 10:
            set_user_words_per_day(user_id, num)
            await update.message.reply_text(
                f"✅ Количество слов установлено: {num} в день.",
                reply_markup=get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "Пожалуйста, введите число от 1 до 10.",
                reply_markup=get_main_keyboard()
            )
        return

    # Получаем текущую категорию из user_data
    cat_key = context.user_data.get("current_category", DEFAULT_CATEGORY)
    cat = CATEGORIES[cat_key]
    words = cat["words"]
    total = len(words)

    if text == "📚 Слова на сегодня":
        cat_prog = get_category_progress(user_id, cat_key)
        used = cat_prog["used"]
        unused = get_unused_indices(used, total)
        if not unused:
            # Все слова изучены — автоматически сбрасываем
            reset_category_progress(user_id, cat_key)
            await update.message.reply_text(
                f"🎉 Поздравляю! Ты изучил все {total} слов в теме *{cat['name']}*!\n"
                f"Начинаю заново: вот новые слова.",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            cat_prog = get_category_progress(user_id, cat_key)
            used = cat_prog["used"]
            unused = get_unused_indices(used, total)

        words_per_day = get_user_words_per_day(user_id)
        count = min(words_per_day, len(unused))
        chosen_indices = random.sample(unused, count)
        chosen_words = [words[i] for i in chosen_indices]

        new_used = used + chosen_indices
        cat_prog["used"] = new_used
        cat_prog["last"] = chosen_words
        set_category_progress(user_id, cat_key, cat_prog)

        msg = "*Сегодняшние слова:*\n\n" + "\n".join(f"{i+1}. {format_word(w)}" for i, w in enumerate(chosen_words))
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard())

    elif text == "🔄 Повторить":
        cat_prog = get_category_progress(user_id, cat_key)
        last = cat_prog.get("last", [])
        if not last:
            await update.message.reply_text(
                "Ты ещё не получал слова сегодня. Нажми «Слова на сегодня».",
                reply_markup=get_main_keyboard()
            )
            return
        msg = "*Последние слова:*\n\n" + "\n".join(f"{i+1}. {format_word(w)}" for i, w in enumerate(last))
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard())

    elif text == "📊 Прогресс":
        # Получаем прогресс по всем категориям
        user_progress = get_user_progress(user_id)
        total_words = 0
        total_studied = 0
        categories_info = []
        for key, info in CATEGORIES.items():
            cat_prog = user_progress.get(key, {"used": []})
            studied = len(cat_prog["used"])
            total_cat = len(info["words"])
            total_words += total_cat
            total_studied += studied
            categories_info.append(f"• *{info['name']}*: {studied}/{total_cat}")

        # Формируем сообщение
        overall_percent = (total_studied / total_words * 100) if total_words > 0 else 0
        msg = (
            f"📊 *Общий прогресс*\n"
            f"Изучено: *{total_studied}* из *{total_words}* слов ({overall_percent:.1f}%)\n\n"
            f"*По категориям:*\n" + "\n".join(categories_info)
        )
        await update.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

    elif text == "🗑 Сбросить прогресс":
        reset_category_progress(user_id, cat_key)
        await update.message.reply_text(
            f"✅ Прогресс в теме *{cat['name']}* сброшен.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    cat_key = query.data.split("_", 1)[1]
    if cat_key not in CATEGORIES:
        await query.edit_message_text("Неизвестная категория.")
        return
    context.user_data["current_category"] = cat_key
    # Сохраняем текущую категорию в прогресс
    up = get_user_progress(user_id)
    up["current_category"] = cat_key
    set_user_progress(user_id, up)
    await query.edit_message_text(
        f"✅ Выбрана тема: *{CATEGORIES[cat_key]['name']}*.\n"
        f"Теперь используй кнопки для изучения.",
        parse_mode="Markdown"
    )
    # Отправляем клавиатуру отдельным сообщением, потому что инлайн-редактирование не поддерживает reply_markup
    await context.bot.send_message(
        chat_id=user_id,
        text="Клавиатура активна.",
        reply_markup=get_main_keyboard()
    )

# ========== КОМАНДА /set_count (оставлена как альтернатива) ==========
async def set_count_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    current = get_user_words_per_day(user_id)
    await update.message.reply_text(
        f"Сколько слов вы хотите получать за раз? Введите число от 1 до 10.\n"
        f"Текущее значение: {current}",
        reply_markup=get_main_keyboard()
    )
    return COUNT_INPUT

async def set_count_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    try:
        count = int(update.message.text)
        if 1 <= count <= 10:
            set_user_words_per_day(user_id, count)
            await update.message.reply_text(
                f"✅ Количество слов установлено: {count} в день.",
                reply_markup=get_main_keyboard()
            )
            return ConversationHandler.END
        else:
            await update.message.reply_text(
                "Пожалуйста, введите число от 1 до 10.",
                reply_markup=get_main_keyboard()
            )
            return COUNT_INPUT
    except ValueError:
        await update.message.reply_text(
            "Пожалуйста, введите целое число.",
            reply_markup=get_main_keyboard()
        )
        return COUNT_INPUT

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Настройка отменена.", reply_markup=get_main_keyboard())
    return ConversationHandler.END

# ========== ЗАПУСК С ВЕБ-ХУКОМ ==========
async def main():
    app = Application.builder().token(TOKEN).updater(None).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    # ConversationHandler для /set_count (оставлен как альтернатива)
    count_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("set_count", set_count_start)],
        states={
            COUNT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_count_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(count_conv_handler)

    # Обработчики сообщений и кнопок
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_handler(CallbackQueryHandler(category_callback, pattern="^cat_"))

    # Устанавливаем веб-хук
    webhook_url = f"{URL}/telegram"
    await app.bot.set_webhook(webhook_url, allowed_updates=Update.ALL_TYPES)
    print(f"Webhook set to {webhook_url}")

    # Starlette для приёма веб-хуков
    async def telegram_webhook(request: Request) -> Response:
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return Response()

    async def healthcheck(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    starlette_app = Starlette(routes=[
        Route("/telegram", telegram_webhook, methods=["POST"]),
        Route("/healthcheck", healthcheck, methods=["GET"]),
    ])

    import uvicorn
    config = uvicorn.Config(app=starlette_app, host="0.0.0.0", port=PORT)
    server = uvicorn.Server(config)

    async with app:
        await app.start()
        await server.serve()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
