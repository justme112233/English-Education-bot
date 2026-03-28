import os
import asyncio
import json
import random
import io
import logging
from starlette.applications import Starlette
from starlette.responses import Response, PlainTextResponse
from starlette.requests import Request
from starlette.routing import Route
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Попытка импортировать gTTS
try:
    from gtts import gTTS
    GTTS_AVAILABLE = True
    logger.info("gTTS successfully imported")
except ImportError as e:
    GTTS_AVAILABLE = False
    logger.warning(f"gTTS not available: {e}")

# ========== НАСТРОЙКИ ==========
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
URL = os.environ.get("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")
PORT = int(os.getenv("PORT", 8000))

WORDS_FILE = "words.json"
PROGRESS_FILE = "progress.json"

# Загружаем категории
with open(WORDS_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)
CATEGORIES = data["categories"]
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

# Форматирование слова в зависимости от порядка
def format_word_by_order(word_obj, order):
    if order == "en_ru":
        return f"**{word_obj['word']}**    {word_obj['transcription']}    \"{word_obj['pronunciation']}\"    {word_obj['translation']}"
    else:  # ru_en
        return f"**{word_obj['translation']}**    {word_obj['word']}    {word_obj['transcription']}    \"{word_obj['pronunciation']}\""

# ========== НАСТРОЙКИ КОЛИЧЕСТВА СЛОВ И ПОРЯДКА ==========
def get_user_words_per_day(user_id: str) -> int:
    up = get_user_progress(user_id)
    return up.get("words_per_day", 5)

def set_user_words_per_day(user_id: str, count: int):
    up = get_user_progress(user_id)
    up["words_per_day"] = count
    set_user_progress(user_id, up)

def get_user_word_order(user_id: str) -> str:
    up = get_user_progress(user_id)
    return up.get("word_order", "en_ru")  # "en_ru" или "ru_en"

def set_user_word_order(user_id: str, order: str):
    up = get_user_progress(user_id)
    up["word_order"] = order
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
    keyboard = [
        [InlineKeyboardButton("➕ Ещё слова", callback_data="more_words"),
         InlineKeyboardButton("🔄 Обратный порядок", callback_data="reverse_order")],
        [InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_quiz_category_buttons(user_id: str):
    buttons = []
    total_studied = 0
    for key, info in CATEGORIES.items():
        studied = len(get_user_progress(user_id).get(key, {"used": []})["used"])
        total_studied += studied
    if total_studied > 0:
        buttons.append([InlineKeyboardButton("📚 Все категории", callback_data="quiz_all")])
    for key, info in CATEGORIES.items():
        studied = len(get_user_progress(user_id).get(key, {"used": []})["used"])
        if studied > 0:
            buttons.append([InlineKeyboardButton(f"{info['name']} ({studied})", callback_data=f"quiz_cat_{key}")])
    if not buttons:
        buttons.append([InlineKeyboardButton("😢 Нет изученных слов", callback_data="noop")])
    return InlineKeyboardMarkup(buttons)

def get_quiz_buttons(correct_translation, wrong_translations, word_id):
    options = wrong_translations + [correct_translation]
    random.shuffle(options)
    keyboard = []
    for opt in options:
        keyboard.append([InlineKeyboardButton(opt, callback_data=f"quiz_answer_{word_id}_{opt}")])
    keyboard.append([InlineKeyboardButton("🔙 Выйти в меню", callback_data="exit_quiz")])
    return InlineKeyboardMarkup(keyboard)

def get_confirm_reset_buttons(cat_key):
    keyboard = [
        [InlineKeyboardButton("✅ Да, сбросить", callback_data=f"confirm_reset_{cat_key}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_reset")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_order_settings_buttons(current_order):
    """Кнопки для выбора порядка слов в настройках"""
    keyboard = []
    if current_order != "en_ru":
        keyboard.append([InlineKeyboardButton("🇬🇧 Английский → Русский", callback_data="order_en_ru")])
    if current_order != "ru_en":
        keyboard.append([InlineKeyboardButton("🇷🇺 Русский → Английский", callback_data="order_ru_en")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(keyboard)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ВИКТОРИНЫ ==========
def get_studied_indices(user_id: str, cat_key: str):
    cat_prog = get_category_progress(user_id, cat_key)
    return cat_prog.get("used", [])

def get_all_studied_words(user_id: str):
    all_words = []
    for key, info in CATEGORIES.items():
        studied = get_studied_indices(user_id, key)
        for idx in studied:
            all_words.append(info["words"][idx])
    return all_words

def get_random_studied_word(user_id: str, cat_key: str = None):
    if cat_key is None or cat_key == "all":
        all_words = get_all_studied_words(user_id)
        if not all_words:
            return None
        return random.choice(all_words)
    else:
        studied = get_studied_indices(user_id, cat_key)
        if not studied:
            return None
        idx = random.choice(studied)
        return CATEGORIES[cat_key]["words"][idx]

def get_random_translations_for_quiz(cat_key: str, correct_word_obj, count=3):
    if cat_key == "all":
        all_translations = []
        for key, info in CATEGORIES.items():
            all_translations.extend([w["translation"] for w in info["words"]])
        correct_trans = correct_word_obj["translation"]
        possible = [t for t in all_translations if t != correct_trans]
        if len(possible) < count:
            possible = [t for t in all_translations if t != correct_trans] * 3
        return random.sample(possible, min(count, len(possible)))
    else:
        words = CATEGORIES[cat_key]["words"]
        translations = [w["translation"] for w in words]
        correct_trans = correct_word_obj["translation"]
        possible = [t for t in translations if t != correct_trans]
        if len(possible) < count:
            possible = [t for t in translations if t != correct_trans] * 3
        return random.sample(possible, min(count, len(possible)))

# ========== ОБРАБОТЧИКИ СООБЩЕНИЙ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if "current_category" not in context.user_data:
        context.user_data["current_category"] = DEFAULT_CATEGORY
    up = get_user_progress(user_id)
    if "current_category" not in up:
        up["current_category"] = DEFAULT_CATEGORY
        set_user_progress(user_id, up)
    if "today_words" not in context.user_data:
        context.user_data["today_words"] = []
    await update.message.reply_text(
        f"👋 Привет! Я помогу выучить английские слова по темам.\n\n"
        f"Текущая тема: *{CATEGORIES[context.user_data['current_category']]['name']}*\n\n"
        f"Количество слов в день: *{get_user_words_per_day(user_id)}*\n\n"
        f"Порядок слов: {'Английский→Русский' if get_user_word_order(user_id) == 'en_ru' else 'Русский→Английский'}\n\n"
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
        current_order = get_user_word_order(user_id)
        order_text = "🇬🇧 Английский → Русский" if current_order == "en_ru" else "🇷🇺 Русский → Английский"
        await update.message.reply_text(
            f"⚙️ *Настройки*\n\n"
            f"• Количество слов в день: *{get_user_words_per_day(user_id)}*\n"
            f"• Порядок слов: *{order_text}*\n\n"
            f"Изменить порядок можно ниже:",
            parse_mode="Markdown",
            reply_markup=get_order_settings_buttons(current_order)
        )
        return

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

    cat_key = context.user_data.get("current_category", DEFAULT_CATEGORY)
    cat = CATEGORIES[cat_key]
    words = cat["words"]
    total = len(words)

    if text == "📚 Слова на сегодня":
        today_indices = context.user_data.get("today_words", [])
        cat_prog = get_category_progress(user_id, cat_key)
        used = cat_prog["used"]
        unused = get_unused_indices(used, total)
        available = [i for i in unused if i not in today_indices]
        if not available:
            if not unused:
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
                today_indices = []
                context.user_data["today_words"] = []
                available = [i for i in unused if i not in today_indices]

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
        context.user_data["last_pronounce_words"] = chosen_words
        context.user_data["current_batch_words"] = chosen_words  # для обратного порядка

        order = get_user_word_order(user_id)
        msg = "*Сегодняшние слова:*\n\n" + "\n".join(f"{i+1}. {format_word_by_order(w, order)}" for i, w in enumerate(chosen_words))
        await update.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=get_after_words_buttons()
        )

    elif text == "🎮 Викторина":
        await update.message.reply_text(
            "Выберите категорию для викторины:",
            reply_markup=get_quiz_category_buttons(user_id)
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
        await update.message.reply_text(
            f"⚠️ Вы уверены, что хотите сбросить прогресс в теме *{cat['name']}*?",
            parse_mode="Markdown",
            reply_markup=get_confirm_reset_buttons(cat_key)
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
        today_indices = context.user_data.get("today_words", [])
        cat_prog = get_category_progress(user_id, cat_key)
        used = cat_prog["used"]
        total = len(words)
        unused = get_unused_indices(used, total)
        available = [i for i in unused if i not in today_indices]
        if not available:
            if not unused:
                reset_category_progress(user_id, cat_key)
                await query.message.reply_text(
                    f"🎉 Поздравляю! Ты изучил все {total} слов в теме *{cat['name']}*!\n"
                    f"Начинаю заново: вот новые слова.",
                    parse_mode="Markdown",
                    reply_markup=get_after_words_buttons()
                )
                cat_prog = get_category_progress(user_id, cat_key)
                used = cat_prog["used"]
                unused = get_unused_indices(used, total)
                available = [i for i in unused if i not in today_indices]
            else:
                await query.message.reply_text(
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
        context.user_data["last_pronounce_words"] = chosen_words
        context.user_data["current_batch_words"] = chosen_words

        order = get_user_word_order(user_id)
        msg = "*Ещё слова:*\n\n" + "\n".join(f"{i+1}. {format_word_by_order(w, order)}" for i, w in enumerate(chosen_words))
        await query.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=get_after_words_buttons()
        )

    elif data == "back_to_menu":
        # Возвращаем основную клавиатуру, НЕ очищая today_words
        await query.message.reply_text("Возвращаюсь в главное меню.")
        await context.bot.send_message(
            chat_id=user_id,
            text="Клавиатура активна.",
            reply_markup=get_main_keyboard()
        )

    elif data == "reverse_order":
        # Берём последнюю порцию слов из контекста
        batch = context.user_data.get("current_batch_words")
        if not batch:
            await query.answer("Нет слов для переворота.", show_alert=True)
            return
        # Получаем текущий глобальный порядок, но для обратного используем противоположный
        current_order = get_user_word_order(user_id)
        reverse_order = "ru_en" if current_order == "en_ru" else "en_ru"
        msg = "*Слова (обратный порядок):*\n\n" + "\n".join(f"{i+1}. {format_word_by_order(w, reverse_order)}" for i, w in enumerate(batch))
        await query.message.reply_text(
            msg,
            parse_mode="Markdown",
            reply_markup=get_after_words_buttons()
        )

    elif data == "pronounce":
        if not GTTS_AVAILABLE:
            await query.answer("Функция произношения временно недоступна.", show_alert=True)
            return
        last_words = context.user_data.get("last_pronounce_words", [])
        if not last_words:
            await query.answer("Нет слов для озвучивания. Сначала получите слова на сегодня.", show_alert=True)
            return
        word_obj = random.choice(last_words)
        text_to_speak = word_obj["word"]
        try:
            tts = gTTS(text=text_to_speak, lang='en')
            audio_bytes = io.BytesIO()
            tts.write_to_fp(audio_bytes)
            audio_bytes.seek(0)
            # Отправляем как аудиофайл (не голосовое)
            await query.message.reply_audio(
                audio=audio_bytes,
                filename=f"{text_to_speak}.mp3",
                caption=f"🔊 {text_to_speak}",
                title=text_to_speak,
                performer="English Bot"
            )
            logger.info(f"Sent audio for {text_to_speak}")
        except Exception as e:
            logger.error(f"gTTS error: {e}")
            await query.answer("Не удалось сгенерировать произношение.", show_alert=True)

    elif data.startswith("order_"):
        # Изменение порядка слов в настройках
        new_order = data.split("_", 1)[1]  # "en_ru" или "ru_en"
        if new_order in ("en_ru", "ru_en"):
            set_user_word_order(user_id, new_order)
            order_text = "🇬🇧 Английский → Русский" if new_order == "en_ru" else "🇷🇺 Русский → Английский"
            await query.edit_message_text(
                f"✅ Порядок слов изменён на *{order_text}*.\n\n"
                f"Теперь новые слова будут выводиться в этом формате.",
                parse_mode="Markdown",
                reply_markup=get_order_settings_buttons(new_order)
            )
        else:
            await query.answer("Неверный порядок.")

    elif data.startswith("confirm_reset_"):
        cat_to_reset = data.split("_", 2)[2]
        reset_category_progress(user_id, cat_to_reset)
        if cat_to_reset == context.user_data.get("current_category"):
            context.user_data["today_words"] = []
        await query.edit_message_text(
            f"✅ Прогресс в теме *{CATEGORIES[cat_to_reset]['name']}* сброшен.",
            parse_mode="Markdown"
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="Клавиатура активна.",
            reply_markup=get_main_keyboard()
        )

    elif data == "cancel_reset":
        await query.edit_message_text("❌ Сброс отменён.")
        await context.bot.send_message(
            chat_id=user_id,
            text="Клавиатура активна.",
            reply_markup=get_main_keyboard()
        )

    elif data == "exit_quiz":
        await query.edit_message_text("Викторина завершена. Возвращаюсь в меню.")
        await context.bot.send_message(
            chat_id=user_id,
            text="Клавиатура активна.",
            reply_markup=get_main_keyboard()
        )

    elif data == "quiz_all":
        context.user_data["quiz_category"] = "all"
        studied_words = get_all_studied_words(user_id)
        if not studied_words:
            await query.edit_message_text(
                "❌ У вас ещё нет изученных слов. Сначала выучите несколько слов через «Слова на сегодня».",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]])
            )
            return
        word_obj = random.choice(studied_words)
        cat_for_options = None
        for key, info in CATEGORIES.items():
            if word_obj in info["words"]:
                cat_for_options = key
                break
        context.user_data["last_quiz_word"] = word_obj
        context.user_data["last_quiz_cat"] = cat_for_options if cat_for_options else "all"
        wrong = get_random_translations_for_quiz(cat_for_options if cat_for_options else "all", word_obj, 3)
        await query.edit_message_text(
            f"*Викторина (все категории)*\n\nСлово: **{word_obj['word']}**\n\nВыберите правильный перевод:",
            parse_mode="Markdown",
            reply_markup=get_quiz_buttons(word_obj["translation"], wrong, id(word_obj))
        )

    elif data.startswith("quiz_cat_"):
        cat_for_quiz = data.split("_", 2)[2]
        context.user_data["quiz_category"] = cat_for_quiz
        word_obj = get_random_studied_word(user_id, cat_for_quiz)
        if not word_obj:
            await query.edit_message_text(
                f"❌ В категории *{CATEGORIES[cat_for_quiz]['name']}* нет изученных слов. Сначала выучите несколько слов.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]])
            )
            return
        context.user_data["last_quiz_word"] = word_obj
        context.user_data["last_quiz_cat"] = cat_for_quiz
        wrong = get_random_translations_for_quiz(cat_for_quiz, word_obj, 3)
        await query.edit_message_text(
            f"*Викторина* ({CATEGORIES[cat_for_quiz]['name']})\n\nСлово: **{word_obj['word']}**\n\nВыберите правильный перевод:",
            parse_mode="Markdown",
            reply_markup=get_quiz_buttons(word_obj["translation"], wrong, id(word_obj))
        )

    elif data.startswith("quiz_answer_"):
        parts = data.split("_", 2)
        if len(parts) < 3:
            return
        _, word_id_str, chosen_trans = parts
        last_word = context.user_data.get("last_quiz_word")
        last_cat = context.user_data.get("last_quiz_cat")
        if not last_word:
            await query.edit_message_text("Ошибка. Попробуйте начать викторину заново.")
            return
        correct_trans = last_word["translation"]
        if chosen_trans == correct_trans:
            result = f"✅ *Правильно!*\n\n*Слово:* {last_word['word']}\n*Перевод:* {last_word['translation']}"
        else:
            result = f"❌ *Неправильно.*\n\n*Слово:* {last_word['word']}\n*Правильный перевод:* {last_word['translation']}"

        # Следующий вопрос
        if last_cat == "all":
            studied_words = get_all_studied_words(user_id)
            if studied_words:
                next_word = random.choice(studied_words)
                next_cat = None
                for key, info in CATEGORIES.items():
                    if next_word in info["words"]:
                        next_cat = key
                        break
                context.user_data["last_quiz_word"] = next_word
                context.user_data["last_quiz_cat"] = next_cat if next_cat else "all"
                wrong = get_random_translations_for_quiz(next_cat if next_cat else "all", next_word, 3)
                next_text = (
                    f"{result}\n\n"
                    f"Следующее слово: **{next_word['word']}**\n\n"
                    f"Выберите перевод:"
                )
                await query.edit_message_text(
                    next_text,
                    parse_mode="Markdown",
                    reply_markup=get_quiz_buttons(next_word["translation"], wrong, id(next_word))
                )
            else:
                await query.edit_message_text(
                    f"{result}\n\nВикторина завершена, так как нет больше изученных слов.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="exit_quiz")]])
                )
        else:
            studied = get_studied_indices(user_id, last_cat)
            if studied:
                next_idx = random.choice(studied)
                next_word = CATEGORIES[last_cat]["words"][next_idx]
                context.user_data["last_quiz_word"] = next_word
                wrong = get_random_translations_for_quiz(last_cat, next_word, 3)
                next_text = (
                    f"{result}\n\n"
                    f"Следующее слово: **{next_word['word']}**\n\n"
                    f"Выберите перевод:"
                )
                await query.edit_message_text(
                    next_text,
                    parse_mode="Markdown",
                    reply_markup=get_quiz_buttons(next_word["translation"], wrong, id(next_word))
                )
            else:
                await query.edit_message_text(
                    f"{result}\n\nВикторина завершена, так как в этой категории нет больше изученных слов.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="exit_quiz")]])
                )

    elif data == "noop":
        await query.answer("Пока нет изученных слов.", show_alert=True)

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
    context.user_data["today_words"] = []  # при смене категории сбрасываем список выданных сегодня
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
    app.add_handler(CallbackQueryHandler(inline_buttons_callback, pattern="^(more_words|back_to_menu|reverse_order|pronounce|order_|confirm_reset_|cancel_reset|exit_quiz|quiz_all|quiz_cat_|quiz_answer_|noop)"))
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
