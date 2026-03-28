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
        [KeyboardButton("📚 Слова на сегодня"), KeyboardButton("🎮 Викторина")],
        [KeyboardButton("📊 Прогресс"), KeyboardButton("🗑 Сбросить прогресс")],
        [KeyboardButton("🎯 Выбрать тему"), KeyboardButton("⚙️ Настройки")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_category_buttons(user_id: str):
    buttons = []
    user_progress = get_user_progress(user_id)
    for key, info in CATEGORIES.items():
        cat_prog = user_progress.get(key, {"used": []})
        done = len(cat_prog["used"])
        total = len(info["words"])
        text = f"{info['name']} ({done}/{total})"
        buttons.append([InlineKeyboardButton(text, callback_data=f"cat_{key}")])
    return InlineKeyboardMarkup(buttons)

def get_after_words_buttons():
    """Инлайн-кнопки после выдачи слов"""
    keyboard = [
        [InlineKeyboardButton("➕ Ещё слова", callback_data="more_words")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_quiz_buttons(correct_translation, wrong_translations, word_id):
    """Генерирует кнопки для викторины: перемешанные варианты"""
    options = wrong_translations + [correct_translation]
    random.shuffle(options)
    keyboard = []
    for opt in options:
        keyboard.append([InlineKeyboardButton(opt, callback_data=f"quiz_{word_id}_{opt}")])
    # Добавляем кнопку выхода
    keyboard.append([InlineKeyboardButton("🔙 Выйти в меню", callback_data="exit_quiz")])
    return InlineKeyboardMarkup(keyboard)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_studied_indices(user_id: str, cat_key: str):
    cat_prog = get_category_progress(user_id, cat_key)
    return cat_prog.get("used", [])

def get_random_studied_word(user_id: str, cat_key: str):
    """Возвращает объект слова, которое уже изучено (если есть)"""
    studied = get_studied_indices(user_id, cat_key)
    if not studied:
        return None
    idx = random.choice(studied)
    return CATEGORIES[cat_key]["words"][idx]

def get_random_words_for_quiz(cat_key: str, correct_word_obj, count=3):
    """Возвращает список из count случайных переводов из категории (не совпадающих с правильным)"""
    words = CATEGORIES[cat_key]["words"]
    # Все переводы
    translations = [w["translation"] for w in words]
    # Убираем правильный перевод
    correct_trans = correct_word_obj["translation"]
    possible = [t for t in translations if t != correct_trans]
    # Если недостаточно, добавляем повторяющиеся (редко)
    if len(possible) < count:
        possible = [t for t in translations if t != correct_trans] * 3
    return random.sample(possible, min(count, len(possible)))

# ========== ОБРАБОТЧИКИ СООБЩЕНИЙ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if "current_category" not in context.user_data:
        context.user_data["current_category"] = DEFAULT_CATEGORY
    # Сохраняем текущую категорию в прогресс
    up = get_user_progress(user_id)
    if "current_category" not in up:
        up["current_category"] = DEFAULT_CATEGORY
        set_user_progress(user_id, up)
    # Инициализируем список выданных сегодня слов (будет храниться в user_data)
    if "today_words" not in context.user_data:
        context.user_data["today_words"] = []  # список индексов, выданных сегодня
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

    # Получаем текущую категорию
    cat_key = context.user_data.get("current_category", DEFAULT_CATEGORY)
    cat = CATEGORIES[cat_key]
    words = cat["words"]
    total = len(words)

    if text == "📚 Слова на сегодня":
        # Если уже есть выданные сегодня слова, используем их, иначе создаём пустой список
        today_indices = context.user_data.get("today_words", [])
        # Получаем изученные индексы из прогресса
        cat_prog = get_category_progress(user_id, cat_key)
        used = cat_prog["used"]
        # Свободные слова: те, что ещё не изучены
        unused = get_unused_indices(used, total)
        # Из них исключаем уже выданные сегодня
        available = [i for i in unused if i not in today_indices]
        if not available:
            # Если все доступные слова уже были сегодня, либо все изучены
            if not unused:
                # Все слова изучены в категории, сбрасываем
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
                available = [i for i in unused if i not in today_indices]
            else:
                # Есть неизученные, но все они уже были сегодня – дадим возможность получить ещё, но сбрасываем список today_indices
                today_indices = []
                context.user_data["today_words"] = []
                available = [i for i in unused if i not in today_indices]

        words_per_day = get_user_words_per_day(user_id)
        count = min(words_per_day, len(available))
        chosen_indices = random.sample(available, count)
        chosen_words = [words[i] for i in chosen_indices]

        # Обновляем прогресс
        new_used = used + chosen_indices
        cat_prog["used"] = new_used
        cat_prog["last"] = chosen_words
        set_category_progress(user_id, cat_key, cat_prog)

        # Добавляем в today_indices
        today_indices.extend(chosen_indices)
        context.user_data["today_words"] = today_indices

        msg = "*Сегодняшние слова:*\n\n" + "\n".join(f"{i+1}. {format_word(w)}" for i, w in enumerate(chosen_words))
        await update.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=get_after_words_buttons()  # инлайн-кнопки
        )

    elif text == "🎮 Викторина":
        # Запускаем викторину
        studied = get_studied_indices(user_id, cat_key)
        if not studied:
            await update.message.reply_text(
                "❓ Вы ещё не выучили ни одного слова в этой категории.\n"
                "Нажмите «Слова на сегодня», чтобы начать.",
                reply_markup=get_main_keyboard()
            )
            return
        # Выбираем случайное изученное слово
        word_idx = random.choice(studied)
        word_obj = words[word_idx]
        correct_trans = word_obj["translation"]
        wrong = get_random_words_for_quiz(cat_key, word_obj, 3)
        # Отправляем сообщение с кнопками
        await update.message.reply_text(
            f"*Викторина*\n\nСлово: **{word_obj['word']}**\n\nВыберите правильный перевод:",
            parse_mode="Markdown",
            reply_markup=get_quiz_buttons(correct_trans, wrong, word_idx)
        )
        # Здесь мы не меняем клавиатуру, пользователь остаётся в основном меню

    elif text == "📊 Прогресс":
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

# ========== ОБРАБОТЧИКИ ИНЛАЙН-КНОПОК ==========
async def inline_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = str(query.from_user.id)
    cat_key = context.user_data.get("current_category", DEFAULT_CATEGORY)
    cat = CATEGORIES[cat_key]
    words = cat["words"]

    if data == "more_words":
        # Пользователь хочет ещё слова
        today_indices = context.user_data.get("today_words", [])
        cat_prog = get_category_progress(user_id, cat_key)
        used = cat_prog["used"]
        total = len(words)
        unused = get_unused_indices(used, total)
        available = [i for i in unused if i not in today_indices]
        if not available:
            if not unused:
                # Все изучены, сбрасываем
                reset_category_progress(user_id, cat_key)
                await query.edit_message_text(
                    f"🎉 Поздравляю! Ты изучил все {total} слов в теме *{cat['name']}*!\n"
                    f"Начинаю заново: вот новые слова.",
                    parse_mode="Markdown"
                )
                cat_prog = get_category_progress(user_id, cat_key)
                used = cat_prog["used"]
                unused = get_unused_indices(used, total)
                available = [i for i in unused if i not in today_indices]
            else:
                await query.edit_message_text(
                    "📚 Сегодня вы уже получили все доступные новые слова. Завтра будут новые.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]])
                )
                return

        words_per_day = get_user_words_per_day(user_id)
        count = min(words_per_day, len(available))
        chosen_indices = random.sample(available, count)
        chosen_words = [words[i] for i in chosen_indices]

        new_used = used + chosen_indices
        cat_prog["used"] = new_used
        cat_prog["last"] = chosen_words
        set_category_progress(user_id, cat_key, cat_prog)

        today_indices.extend(chosen_indices)
        context.user_data["today_words"] = today_indices

        msg = "*Ещё слова:*\n\n" + "\n".join(f"{i+1}. {format_word(w)}" for i, w in enumerate(chosen_words))
        await query.edit_message_text(
            msg,
            parse_mode="Markdown",
            reply_markup=get_after_words_buttons()
        )

    elif data == "back_to_menu":
        # Возвращаем в основное меню
        context.user_data["today_words"] = []  # очищаем список выданных сегодня
        await query.edit_message_text("Возвращаюсь в главное меню.")
        # Отправляем клавиатуру отдельным сообщением, потому что инлайн-сообщение уже отредактировано
        await context.bot.send_message(
            chat_id=user_id,
            text="Клавиатура активна.",
            reply_markup=get_main_keyboard()
        )

    elif data == "exit_quiz":
        # Выход из викторины
        await query.edit_message_text("Викторина завершена. Возвращаюсь в меню.")
        await context.bot.send_message(
            chat_id=user_id,
            text="Клавиатура активна.",
            reply_markup=get_main_keyboard()
        )

    elif data.startswith("quiz_"):
        # Обработка ответа викторины: формат quiz_<word_idx>_<выбранный перевод>
        parts = data.split("_", 2)
        if len(parts) < 3:
            return
        _, word_idx_str, chosen_trans = parts
        word_idx = int(word_idx_str)
        word_obj = words[word_idx]
        correct_trans = word_obj["translation"]
        if chosen_trans == correct_trans:
            result = "✅ Правильно!"
        else:
            result = f"❌ Неправильно. Правильный ответ: *{correct_trans}*"

        # Предлагаем следующий вопрос
        studied = get_studied_indices(user_id, cat_key)
        if studied:
            next_word_idx = random.choice(studied)
            next_word_obj = words[next_word_idx]
            next_correct = next_word_obj["translation"]
            next_wrong = get_random_words_for_quiz(cat_key, next_word_obj, 3)
            next_text = (
                f"{result}\n\n"
                f"Следующее слово: **{next_word_obj['word']}**\n\n"
                f"Выберите перевод:"
            )
            await query.edit_message_text(
                next_text,
                parse_mode="Markdown",
                reply_markup=get_quiz_buttons(next_correct, next_wrong, next_word_idx)
            )
        else:
            await query.edit_message_text(
                f"{result}\n\nВикторина завершена, так как в этой категории нет изученных слов.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="exit_quiz")]])
            )

# ========== КОМАНДА /set_count (альтернатива) ==========
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

# ========== ОБРАБОТЧИК ВЫБОРА КАТЕГОРИИ ==========
async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    cat_key = query.data.split("_", 1)[1]
    if cat_key not in CATEGORIES:
        await query.edit_message_text("Неизвестная категория.")
        return
    context.user_data["current_category"] = cat_key
    up = get_user_progress(user_id)
    up["current_category"] = cat_key
    set_user_progress(user_id, up)
    await query.edit_message_text(
        f"✅ Выбрана тема: *{CATEGORIES[cat_key]['name']}*.\n"
        f"Теперь используй кнопки для изучения.",
        parse_mode="Markdown"
    )
    await context.bot.send_message(
        chat_id=user_id,
        text="Клавиатура активна.",
        reply_markup=get_main_keyboard()
    )

# ========== ЗАПУСК С ВЕБ-ХУКОМ ==========
async def main():
    app = Application.builder().token(TOKEN).updater(None).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
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
    app.add_handler(CallbackQueryHandler(inline_buttons_callback, pattern="^(more_words|back_to_menu|exit_quiz|quiz_)"))

    # Отдельный обработчик для выбора категории
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
