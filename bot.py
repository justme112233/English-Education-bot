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

def get_quiz_category_buttons():
    """Инлайн-кнопки для выбора категории викторины (включая 'Все категории')"""
    buttons = []
    for key, info in CATEGORIES.items():
        buttons.append([InlineKeyboardButton(info['name'], callback_data=f"quiz_cat_{key}")])
    buttons.append([InlineKeyboardButton("🌍 Все категории", callback_data="quiz_cat_all")])
    buttons.append([InlineKeyboardButton("🔙 Отмена", callback_data="quiz_cancel")])
    return InlineKeyboardMarkup(buttons)

def get_after_words_buttons(message_id: int):
    """Инлайн-кнопки после выдачи слов. Передаём message_id, чтобы при нажатии 'ещё' знать, какое сообщение было."""
    keyboard = [
        [InlineKeyboardButton("➕ Ещё слова", callback_data=f"more_words_{message_id}")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_quiz_buttons(correct_translation, wrong_translations, word_id, source):
    """Генерирует кнопки для викторины: перемешанные варианты.
       source = ('cat', cat_key) или ('all', None) для сохранения контекста."""
    options = wrong_translations + [correct_translation]
    random.shuffle(options)
    keyboard = []
    for opt in options:
        # В callback сохраняем: quiz_answer_<source>_<word_id>_<opt>
        if source[0] == 'cat':
            cb_data = f"quiz_answer_cat_{source[1]}_{word_id}_{opt}"
        else:
            cb_data = f"quiz_answer_all_{word_id}_{opt}"
        keyboard.append([InlineKeyboardButton(opt, callback_data=cb_data)])
    # Добавляем кнопку выхода
    keyboard.append([InlineKeyboardButton("🔙 Выйти в меню", callback_data="exit_quiz")])
    return InlineKeyboardMarkup(keyboard)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def get_studied_indices(user_id: str, cat_key: str):
    cat_prog = get_category_progress(user_id, cat_key)
    return cat_prog.get("used", [])

def get_studied_words_all_categories(user_id: str):
    """Возвращает список всех изученных слов (объектов) из всех категорий."""
    all_words = []
    for key, info in CATEGORIES.items():
        studied = get_studied_indices(user_id, key)
        for idx in studied:
            all_words.append((key, info["words"][idx]))  # сохраняем и категорию, и слово
    return all_words

def get_random_studied_word(user_id: str, cat_key: str = None):
    """Возвращает (cat_key, word_obj) для случайного изученного слова.
       Если cat_key == None, выбирает из всех категорий."""
    if cat_key:
        studied = get_studied_indices(user_id, cat_key)
        if not studied:
            return None
        idx = random.choice(studied)
        return cat_key, CATEGORIES[cat_key]["words"][idx]
    else:
        all_studied = get_studied_words_all_categories(user_id)
        if not all_studied:
            return None
        return random.choice(all_studied)

def get_random_words_for_quiz(source, correct_word_obj, count=3):
    """Возвращает список из count случайных переводов из указанного источника.
       source: ('cat', cat_key) или ('all', None)"""
    if source[0] == 'cat':
        cat_key = source[1]
        words = CATEGORIES[cat_key]["words"]
        translations = [w["translation"] for w in words]
    else:
        translations = []
        for info in CATEGORIES.values():
            translations.extend([w["translation"] for w in info["words"]])
    correct_trans = correct_word_obj["translation"]
    possible = [t for t in translations if t != correct_trans]
    if len(possible) < count:
        possible = possible * 3
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
        context.user_data["today_words"] = []  # список индексов, выданных сегодня (глобально для категории)
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
        # Получаем список уже выданных сегодня индексов
        today_indices = context.user_data.get("today_words", [])
        cat_prog = get_category_progress(user_id, cat_key)
        used = cat_prog["used"]
        unused = get_unused_indices(used, total)
        # Доступные для выдачи: неизученные и не выданные сегодня
        available = [i for i in unused if i not in today_indices]
        if not available:
            if not unused:
                # Все слова изучены — сбрасываем категорию
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
                # Есть неизученные, но все уже выданы сегодня — сбрасываем today_indices
                context.user_data["today_words"] = []
                today_indices = []
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
        # Отправляем сообщение с инлайн-кнопками. В callback будем передавать message_id этого сообщения.
        sent_msg = await update.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=get_after_words_buttons(0)  # временно message_id 0, потом обновим при колбэке
        )
        # Сохраняем message_id в user_data для этого сообщения
        if "words_messages" not in context.user_data:
            context.user_data["words_messages"] = []
        context.user_data["words_messages"].append(sent_msg.message_id)

    elif text == "🎮 Викторина":
        # Показываем выбор категории для викторины
        await update.message.reply_text(
            "Выберите категорию для викторины:",
            reply_markup=get_quiz_category_buttons()
        )

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

    if data == "back_to_menu":
        # Очищаем сохранённые сообщения и today_words
        context.user_data["today_words"] = []
        context.user_data["words_messages"] = []
        await query.edit_message_text("Возвращаюсь в главное меню.")
        await context.bot.send_message(
            chat_id=user_id,
            text="Клавиатура активна.",
            reply_markup=get_main_keyboard()
        )
        return

    if data == "exit_quiz":
        await query.edit_message_text("Викторина завершена. Возвращаюсь в меню.")
        await context.bot.send_message(
            chat_id=user_id,
            text="Клавиатура активна.",
            reply_markup=get_main_keyboard()
        )
        return

    # Обработка выбора категории для викторины
    if data.startswith("quiz_cat_"):
        if data == "quiz_cat_all":
            source = ('all', None)
        elif data == "quiz_cancel":
            await query.edit_message_text("Викторина отменена.")
            await context.bot.send_message(
                chat_id=user_id,
                text="Клавиатура активна.",
                reply_markup=get_main_keyboard()
            )
            return
        else:
            cat_key = data.split("_", 2)[2]
            source = ('cat', cat_key)

        # Проверяем, есть ли изученные слова в выбранной категории/во всех
        if source[0] == 'cat':
            studied = get_studied_indices(user_id, source[1])
            if not studied:
                await query.edit_message_text(
                    f"❌ В категории {CATEGORIES[source[1]]['name']} ещё нет изученных слов.\n"
                    f"Нажмите «Слова на сегодня», чтобы начать изучать."
                )
                return
            # Выбираем случайное слово
            idx = random.choice(studied)
            word_obj = CATEGORIES[source[1]]["words"][idx]
            correct_trans = word_obj["translation"]
            wrong = get_random_words_for_quiz(source, word_obj, 3)
            await query.edit_message_text(
                f"*Викторина* – {CATEGORIES[source[1]]['name']}\n\nСлово: **{word_obj['word']}**\n\nВыберите правильный перевод:",
                parse_mode="Markdown",
                reply_markup=get_quiz_buttons(correct_trans, wrong, idx, source)
            )
        else:
            # Все категории
            all_studied = get_studied_words_all_categories(user_id)
            if not all_studied:
                await query.edit_message_text(
                    "❌ У вас ещё нет изученных слов ни в одной категории.\n"
                    "Нажмите «Слова на сегодня», чтобы начать изучать."
                )
                return
            cat_key, word_obj = random.choice(all_studied)
            source = ('cat', cat_key)  # для формирования кнопок указываем конкретную категорию
            correct_trans = word_obj["translation"]
            wrong = get_random_words_for_quiz(source, word_obj, 3)
            await query.edit_message_text(
                f"*Викторина* – все категории\n\nСлово: **{word_obj['word']}**\n\nВыберите правильный перевод:",
                parse_mode="Markdown",
                reply_markup=get_quiz_buttons(correct_trans, wrong, word_obj, source)
            )
        return

    # Обработка ответов викторины: формат quiz_answer_... (два варианта)
    if data.startswith("quiz_answer_"):
        # Формат: quiz_answer_cat_<cat_key>_<word_id>_<translation> или quiz_answer_all_<word_id>_<translation>
        parts = data.split("_", 3)  # ["quiz", "answer", "cat/cat_key...", остальное]
        if len(parts) < 4:
            return
        # В зависимости от типа
        if parts[2] == "cat":
            # quiz_answer_cat_<cat_key>_<word_id>_<translation>
            sub = parts[3].split("_", 2)
            cat_key = sub[0]
            word_id = int(sub[1])
            chosen_trans = sub[2]
            source = ('cat', cat_key)
        else:  # "all"
            # quiz_answer_all_<word_id>_<translation>
            sub = parts[3].split("_", 1)
            word_id = int(sub[0])
            chosen_trans = sub[1]
            # для всех категорий нам не нужен cat_key, но в get_random_words_for_quiz нужно передать source
            source = ('all', None)
            # но для определения правильного слова нужно найти его в какой-то категории
            # найдём слово по индексу в общей структуре? У нас нет глобального индекса.
            # Будем искать по всем категориям
            found_word = None
            for k, info in CATEGORIES.items():
                if word_id < len(info["words"]):
                    found_word = info["words"][word_id]
                    cat_key = k
                    break
            if not found_word:
                await query.edit_message_text("Ошибка: слово не найдено.")
                return
            word_obj = found_word
            correct_trans = word_obj["translation"]
            # для продолжения используем категорию найденного слова, чтобы подбирать варианты из той же категории
            source = ('cat', cat_key)
        # Определяем правильный ответ
        if chosen_trans == correct_trans:
            result = "✅ Правильно!"
        else:
            result = f"❌ Неправильно. Правильный ответ: *{correct_trans}*"

        # Подготовка следующего вопроса
        if source[0] == 'cat':
            studied = get_studied_indices(user_id, cat_key)
            if studied:
                next_idx = random.choice(studied)
                next_word_obj = CATEGORIES[cat_key]["words"][next_idx]
                next_correct = next_word_obj["translation"]
                next_wrong = get_random_words_for_quiz(('cat', cat_key), next_word_obj, 3)
                next_text = (
                    f"{result}\n\n"
                    f"Следующее слово: **{next_word_obj['word']}**\n\n"
                    f"Выберите перевод:"
                )
                await query.edit_message_text(
                    next_text,
                    parse_mode="Markdown",
                    reply_markup=get_quiz_buttons(next_correct, next_wrong, next_idx, ('cat', cat_key))
                )
            else:
                await query.edit_message_text(
                    f"{result}\n\nВикторина завершена, так как в этой категории больше нет изученных слов.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="exit_quiz")]])
                )
        else:
            # все категории
            all_studied = get_studied_words_all_categories(user_id)
            if all_studied:
                next_cat, next_word_obj = random.choice(all_studied)
                next_correct = next_word_obj["translation"]
                next_wrong = get_random_words_for_quiz(('cat', next_cat), next_word_obj, 3)
                next_text = (
                    f"{result}\n\n"
                    f"Следующее слово: **{next_word_obj['word']}**\n\n"
                    f"Выберите перевод:"
                )
                await query.edit_message_text(
                    next_text,
                    parse_mode="Markdown",
                    reply_markup=get_quiz_buttons(next_correct, next_wrong, next_word_obj, ('cat', next_cat))
                )
            else:
                await query.edit_message_text(
                    f"{result}\n\nВикторина завершена, так как нет изученных слов.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="exit_quiz")]])
                )
        return

    # Обработка кнопки "Ещё слова"
    if data.startswith("more_words_"):
        # Извлекаем message_id исходного сообщения
        msg_id = int(data.split("_")[2])
        cat_key = context.user_data.get("current_category", DEFAULT_CATEGORY)
        words = CATEGORIES[cat_key]["words"]
        total = len(words)

        today_indices = context.user_data.get("today_words", [])
        cat_prog = get_category_progress(user_id, cat_key)
        used = cat_prog["used"]
        unused = get_unused_indices(used, total)
        available = [i for i in unused if i not in today_indices]
        if not available:
            if not unused:
                reset_category_progress(user_id, cat_key)
                await query.edit_message_text(
                    f"🎉 Поздравляю! Ты изучил все {total} слов в теме *{CATEGORIES[cat_key]['name']}*!\n"
                    f"Начинаю заново: вот новые слова.",
                    parse_mode="Markdown"
                )
                cat_prog = get_category_progress(user_id, cat_key)
                used = cat_prog["used"]
                unused = get_unused_indices(used, total)
                available = [i for i in unused if i not in today_indices]
            else:
                await query.answer("Сегодня вы уже получили все доступные новые слова. Завтра будут новые.", show_alert=True)
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
        # Отправляем новое сообщение, а не редактируем старое
        sent_msg = await context.bot.send_message(
            chat_id=user_id,
            text=msg,
            parse_mode="Markdown",
            reply_markup=get_after_words_buttons(0)  # кнопки тоже отправляем для нового сообщения
        )
        if "words_messages" not in context.user_data:
            context.user_data["words_messages"] = []
        context.user_data["words_messages"].append(sent_msg.message_id)
        # Закрываем колбэк
        await query.answer()
        return

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

# ========== ОБРАБОТЧИК ВЫБОРА КАТЕГОРИИ ДЛЯ СМЕНЫ ТЕМЫ ==========
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

    app.add_handler(CommandHandler("start", start))
    count_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("set_count", set_count_start)],
        states={
            COUNT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_count_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(count_conv_handler)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_handler(CallbackQueryHandler(inline_buttons_callback, pattern="^(more_words_|quiz_|back_to_menu|exit_quiz)"))
    app.add_handler(CallbackQueryHandler(category_callback, pattern="^cat_"))

    webhook_url = f"{URL}/telegram"
    await app.bot.set_webhook(webhook_url, allowed_updates=Update.ALL_TYPES)
    print(f"Webhook set to {webhook_url}")

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
