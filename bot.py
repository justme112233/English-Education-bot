import os
import asyncio
import json
import random
import io
import logging
import time
import shutil
from datetime import datetime, time as dt_time
from starlette.applications import Starlette
from starlette.responses import Response, PlainTextResponse
from starlette.requests import Request
from starlette.routing import Route
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, ConversationHandler
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

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

# ========== КЭШ ПРОГРЕССА С ОТЛОЖЕННОЙ ЗАПИСЬЮ ==========
progress_cache = None
progress_dirty = set()

def load_progress():
    global progress_cache
    if progress_cache is None:
        if os.path.exists(PROGRESS_FILE):
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                progress_cache = json.load(f)
        else:
            progress_cache = {}
    return progress_cache

def flush_progress():
    global progress_cache, progress_dirty
    if not progress_dirty or progress_cache is None:
        return
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
    else:
        disk_data = {}
    for user_id in progress_dirty:
        if user_id in progress_cache:
            disk_data[user_id] = progress_cache[user_id]
        else:
            disk_data.pop(user_id, None)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(disk_data, f, ensure_ascii=False, indent=2)
    progress_dirty.clear()
    logger.debug("Progress flushed to disk")

def get_user_progress(user_id: str):
    prog = load_progress()
    return prog.get(user_id, {})

def set_user_progress(user_id: str, category_progress):
    global progress_cache, progress_dirty
    if progress_cache is None:
        load_progress()
    progress_cache[user_id] = category_progress
    progress_dirty.add(user_id)

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

def format_word_by_order(word_obj, order):
    if order == "en_ru":
        return f"**{word_obj['word']}**    {word_obj['transcription']}    \"{word_obj['pronunciation']}\"    {word_obj['translation']}"
    else:
        return f"**{word_obj['translation']}**    {word_obj['word']}    {word_obj['transcription']}    \"{word_obj['pronunciation']}\""

# ========== НАСТРОЙКИ КОЛИЧЕСТВА СЛОВ, ПОРЯДКА И РАССЫЛКИ ==========
def get_user_words_per_day(user_id: str) -> int:
    up = get_user_progress(user_id)
    return up.get("words_per_day", 5)

def set_user_words_per_day(user_id: str, count: int):
    up = get_user_progress(user_id)
    up["words_per_day"] = count
    set_user_progress(user_id, up)

def get_user_word_order(user_id: str) -> str:
    up = get_user_progress(user_id)
    return up.get("word_order", "en_ru")

def set_user_word_order(user_id: str, order: str):
    up = get_user_progress(user_id)
    up["word_order"] = order
    set_user_progress(user_id, up)

# Ежедневная рассылка
def get_daily_settings(user_id: str):
    up = get_user_progress(user_id)
    return up.get("daily", {"enabled": False, "time": None, "last_sent": None})

def set_daily_settings(user_id: str, enabled: bool = None, daily_time: str = None, last_sent: str = None):
    up = get_user_progress(user_id)
    if "daily" not in up:
        up["daily"] = {}
    if enabled is not None:
        up["daily"]["enabled"] = enabled
    if daily_time is not None:
        up["daily"]["time"] = daily_time
    if last_sent is not None:
        up["daily"]["last_sent"] = last_sent
    set_user_progress(user_id, up)

# ========== ЛИМИТЫ ЗАПРОСОВ ==========
user_last_call = {}
RATE_LIMIT_SECONDS = 1

async def check_rate_limit(update: Update) -> bool:
    user_id = str(update.effective_user.id)
    now = time.time()
    last = user_last_call.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        try:
            if update.callback_query:
                await update.callback_query.answer("Слишком быстро! Подождите секунду.", show_alert=True)
            else:
                await update.message.reply_text("⏳ Пожалуйста, не так быстро. Подождите секунду.")
        except:
            pass
        return False
    user_last_call[user_id] = now
    return True

# ========== СТРИКИ И ДОСТИЖЕНИЯ ==========
def update_streak(user_id: str):
    up = get_user_progress(user_id)
    today = datetime.now().date().isoformat()
    last_active = up.get("last_active")
    streak = up.get("streak", 0)
    if last_active == today:
        return streak, False
    if last_active is None:
        new_streak = 1
    else:
        last_date = datetime.fromisoformat(last_active).date()
        today_date = datetime.now().date()
        if (today_date - last_date).days == 1:
            new_streak = streak + 1
        else:
            new_streak = 1
    up["last_active"] = today
    up["streak"] = new_streak
    set_user_progress(user_id, up)
    return new_streak, new_streak > streak

def check_achievements(user_id: str, total_studied: int, cat_key: str = None):
    up = get_user_progress(user_id)
    earned = set(up.get("achievements", []))
    new_ones = []
    achievements_def = {
        "first_50": (total_studied >= 50, "🎉 Первые 50 слов", "Выучить 50 слов"),
        "first_200": (total_studied >= 200, "🏅 200 слов", "Достичь 200 изученных слов"),
        "first_500": (total_studied >= 500, "⭐ 500 слов", "Полтысячи слов!"),
        "first_1000": (total_studied >= 1000, "🏆 1000 слов", "Тысяча слов – отлично!"),
        "streak_7": (up.get("streak", 0) >= 7, "📅 Неделя", "Заниматься 7 дней подряд"),
        "streak_30": (up.get("streak", 0) >= 30, "🔥 Месяц", "30 дней непрерывных занятий"),
        "category_travel": (cat_key == "travel" and len(get_category_progress(user_id, "travel")["used"]) == len(CATEGORIES["travel"]["words"]), "🌍 Мастер путешествий", "Завершить категорию «Путешествия»"),
        "category_food": (cat_key == "food" and len(get_category_progress(user_id, "food")["used"]) == len(CATEGORIES["food"]["words"]), "🍕 Гурман", "Завершить категорию «Еда»"),
        "category_verbs": (cat_key == "verbs" and len(get_category_progress(user_id, "verbs")["used"]) == len(CATEGORIES["verbs"]["words"]), "🏃‍♂️ Повелитель глаголов", "Завершить категорию «Глаголы»"),
    }
    for key, (condition, name, desc) in achievements_def.items():
        if condition and key not in earned:
            earned.add(key)
            new_ones.append((name, desc))
    if new_ones:
        up["achievements"] = list(earned)
        set_user_progress(user_id, up)
    return new_ones

def get_total_progress(user_id: str):
    up = get_user_progress(user_id)
    total_studied = 0
    total_words = 0
    for key, info in CATEGORIES.items():
        cat_prog = up.get(key, {"used": []})
        total_studied += len(cat_prog["used"])
        total_words += len(info["words"])
    return total_studied, total_words

def progress_bar(studied, total, length=10):
    if total == 0:
        return "[░░░░░░░░░░] 0%"
    percent = studied / total
    filled = int(length * percent)
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {percent:.0%}"

# ========== ФУНКЦИЯ ОТПРАВКИ СЛОВ (для рассылки) ==========
async def send_daily_words_to_user(bot, user_id: str):
    up = get_user_progress(user_id)
    cat_key = up.get("current_category", DEFAULT_CATEGORY)
    if cat_key not in CATEGORIES:
        cat_key = DEFAULT_CATEGORY
    cat = CATEGORIES[cat_key]
    words = cat["words"]
    total = len(words)
    cat_prog = get_category_progress(user_id, cat_key)
    used = cat_prog["used"]
    unused = get_unused_indices(used, total)
    if not unused:
        reset_category_progress(user_id, cat_key)
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
    order = get_user_word_order(user_id)
    msg = "*Ежедневная порция слов:*\n\n" + "\n".join(f"{i+1}. {format_word_by_order(w, order)}" for i, w in enumerate(chosen_words))
    await bot.send_message(chat_id=user_id, text=msg, parse_mode="Markdown")

# ========== РЕЗЕРВНОЕ КОПИРОВАНИЕ ==========
def backup_progress():
    if not os.path.exists(PROGRESS_FILE):
        return
    os.makedirs("backups", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"backups/progress_{timestamp}.json"
    try:
        shutil.copy2(PROGRESS_FILE, backup_path)
        backups = sorted([f for f in os.listdir("backups") if f.startswith("progress_")])
        while len(backups) > 30:
            os.remove(os.path.join("backups", backups.pop(0)))
        logger.info(f"Progress backed up to {backup_path}")
    except Exception as e:
        logger.error(f"Backup failed: {e}")
		
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
        [InlineKeyboardButton("🔊 Произношение", callback_data="pronounce")],
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

def get_quiz_buttons(correct_translation, wrong_translations):
    options = wrong_translations + [correct_translation]
    random.shuffle(options)
    keyboard = []
    for opt in options:
        keyboard.append([InlineKeyboardButton(opt, callback_data=f"quiz_ans_{opt}")])
    keyboard.append([InlineKeyboardButton("🔙 Выйти в меню", callback_data="exit_quiz")])
    return InlineKeyboardMarkup(keyboard)

def get_confirm_reset_buttons(cat_key):
    keyboard = [
        [InlineKeyboardButton("✅ Да, сбросить", callback_data=f"confirm_reset_{cat_key}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_reset")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_order_settings_buttons(current_order):
    keyboard = []
    if current_order != "en_ru":
        keyboard.append([InlineKeyboardButton("🇬🇧 Английский → Русский", callback_data="order_en_ru")])
    if current_order != "ru_en":
        keyboard.append([InlineKeyboardButton("🇷🇺 Русский → Английский", callback_data="order_ru_en")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_count_settings_buttons(current_count):
    buttons = []
    row = []
    for i in range(1, 11):
        row.append(InlineKeyboardButton(str(i), callback_data=f"count_{i}"))
        if i % 5 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(buttons)

def get_daily_settings_buttons(enabled, current_time):
    keyboard = []
    status = "✅ Включена" if enabled else "❌ Выключена"
    keyboard.append([InlineKeyboardButton(f"Статус: {status}", callback_data="daily_toggle")])
    if enabled:
        keyboard.append([InlineKeyboardButton(f"⏰ Время: {current_time or 'не выбрано'}", callback_data="daily_set_time")])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_time_selection_buttons():
    keyboard = []
    row = []
    for h in range(0, 24):
        row.append(InlineKeyboardButton(f"{h:02d}:00", callback_data=f"daily_time_{h:02d}_00"))
        if (h+1) % 4 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="daily_settings")])
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

# ========== ЕЖЕДНЕВНАЯ РАССЫЛКА ==========
async def check_and_send_daily(app):
    now = datetime.now()
    current_date = now.date().isoformat()
    current_time_str = now.strftime("%H:%M")
    progress = load_progress()
    for user_id, user_data in progress.items():
        daily = user_data.get("daily", {})
        if not daily.get("enabled", False):
            continue
        daily_time = daily.get("time")
        last_sent = daily.get("last_sent")
        if not daily_time:
            continue
        if last_sent == current_date:
            continue
        if daily_time == current_time_str:
            try:
                await send_daily_words_to_user(app.bot, user_id)
                daily["last_sent"] = current_date
                set_user_progress(user_id, user_data)
                logger.info(f"Sent daily words to {user_id} at {current_time_str}")
            except Exception as e:
                logger.error(f"Failed to send daily to {user_id}: {e}")

# ========== ОБРАБОТЧИКИ СООБЩЕНИЙ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_rate_limit(update):
        return
    user_id = str(update.effective_user.id)
    if "current_category" not in context.user_data:
        context.user_data["current_category"] = DEFAULT_CATEGORY
    up = get_user_progress(user_id)
    if "current_category" not in up:
        up["current_category"] = DEFAULT_CATEGORY
        set_user_progress(user_id, up)
    if "today_words" not in context.user_data:
        context.user_data["today_words"] = []
    total_studied, total_words = get_total_progress(user_id)
    streak = up.get("streak", 0)
    await update.message.reply_text(
        f"👋 Привет! Я помогу выучить английские слова по темам.\n\n"
        f"Текущая тема: *{CATEGORIES[context.user_data['current_category']]['name']}*\n\n"
        f"Количество слов в день: *{get_user_words_per_day(user_id)}*\n\n"
        f"Порядок слов: {'Английский→Русский' if get_user_word_order(user_id) == 'en_ru' else 'Русский→Английский'}\n\n"
        f"Ежедневная рассылка: {'Включена' if get_daily_settings(user_id).get('enabled') else 'Выключена'}\n\n"
        f"📊 Общий прогресс: {progress_bar(total_studied, total_words)}\n"
        f"🔥 Стрик: {streak} дней\n\n"
        f"Нажимай кнопки:",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_rate_limit(update):
        return
    text = update.message.text
    user_id = str(update.effective_user.id)

    if text == "🎯 Выбрать тему":
        await update.message.reply_text("Выберите тему:", reply_markup=get_category_buttons(user_id))
        return

    if text == "⚙️ Настройки":
        current_order = get_user_word_order(user_id)
        order_text = "🇬🇧 Английский → Русский" if current_order == "en_ru" else "🇷🇺 Русский → Английский"
        current_count = get_user_words_per_day(user_id)
        daily = get_daily_settings(user_id)
        daily_status = "✅ Включена" if daily.get("enabled") else "❌ Выключена"
        daily_time = daily.get("time") or "не выбрано"
        await update.message.reply_text(
            f"⚙️ *Настройки*\n\n"
            f"• Количество слов в день: *{current_count}*\n"
            f"• Порядок слов: *{order_text}*\n"
            f"• Ежедневная рассылка: *{daily_status}* ({daily_time})\n\n"
            f"Выберите, что хотите изменить:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔢 Количество слов", callback_data="show_count_settings")],
                [InlineKeyboardButton("🔄 Порядок слов", callback_data="show_order_settings")],
                [InlineKeyboardButton("📅 Ежедневная рассылка", callback_data="show_daily_settings")]
            ])
        )
        return

    if text.isdigit():
        num = int(text)
        if 1 <= num <= 10:
            set_user_words_per_day(user_id, num)
            await update.message.reply_text(f"✅ Количество слов установлено: {num} в день.", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("Пожалуйста, введите число от 1 до 10.", reply_markup=get_main_keyboard())
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
                await update.message.reply_text(f"🎉 Поздравляю! Ты изучил все {total} слов в теме *{cat['name']}*!\nНачинаю заново: вот новые слова.", parse_mode="Markdown", reply_markup=get_main_keyboard())
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
        context.user_data["pronounce_remaining"] = chosen_words.copy()
        context.user_data["current_batch_words"] = chosen_words

        streak, is_new_streak = update_streak(user_id)
        total_studied, _ = get_total_progress(user_id)
        new_achievements = check_achievements(user_id, total_studied, cat_key)

        order = get_user_word_order(user_id)
        msg = "*Сегодняшние слова:*\n\n" + "\n".join(f"{i+1}. {format_word_by_order(w, order)}" for i, w in enumerate(chosen_words))
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_after_words_buttons())
        if is_new_streak:
            await update.message.reply_text(f"🔥 Твой стрик: {streak} дней! Так держать!")
        for ach_name, ach_desc in new_achievements:
            await update.message.reply_text(f"🏆 *Новое достижение:* {ach_name}\n_{ach_desc}_", parse_mode="Markdown")

    elif text == "🎮 Викторина":
        await update.message.reply_text("Выберите категорию для викторины:", reply_markup=get_quiz_category_buttons(user_id))

    elif text == "📊 Прогресс":
        user_progress = get_user_progress(user_id)
        total_words_all = 0
        total_studied_all = 0
        categories_info = []
        for key, info in CATEGORIES.items():
            cat_prog = user_progress.get(key, {"used": []})
            studied = len(cat_prog["used"])
            total_cat = len(info["words"])
            total_words_all += total_cat
            total_studied_all += studied
            bar = progress_bar(studied, total_cat)
            categories_info.append(f"• *{info['name']}*: {bar} ({studied}/{total_cat})")
        overall_bar = progress_bar(total_studied_all, total_words_all)
        streak = user_progress.get("streak", 0)
        achievements = user_progress.get("achievements", [])
        msg = (
            f"📊 *Общий прогресс*: {overall_bar}\n"
            f"🔥 Стрик: *{streak}* дней\n"
            f"🏆 Достижений: *{len(achievements)}*\n\n"
            f"*По категориям:*\n" + "\n".join(categories_info)
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard())

    elif text == "🗑 Сбросить прогресс":
        await update.message.reply_text(f"⚠️ Вы уверены, что хотите сбросить прогресс в теме *{cat['name']}*?", parse_mode="Markdown", reply_markup=get_confirm_reset_buttons(cat_key))

async def achievements_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_rate_limit(update):
        return
    user_id = str(update.effective_user.id)
    up = get_user_progress(user_id)
    earned = up.get("achievements", [])
    if not earned:
        await update.message.reply_text("Пока нет достижений. Учите слова, чтобы получать бейджи!")
        return
    ach_names = {
        "first_50": "🎉 Первые 50 слов",
        "first_200": "🏅 200 слов",
        "first_500": "⭐ 500 слов",
        "first_1000": "🏆 1000 слов",
        "streak_7": "📅 Неделя",
        "streak_30": "🔥 Месяц",
        "category_travel": "🌍 Мастер путешествий",
        "category_food": "🍕 Гурман",
        "category_verbs": "🏃‍♂️ Повелитель глаголов",
    }
    msg = "🏆 *Ваши достижения:*\n" + "\n".join(f"• {ach_names.get(a, a)}" for a in earned)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def streak_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_rate_limit(update):
        return
    user_id = str(update.effective_user.id)
    up = get_user_progress(user_id)
    streak = up.get("streak", 0)
    await update.message.reply_text(f"🔥 Ваш текущий стрик: *{streak}* дней подряд!", parse_mode="Markdown")

async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_rate_limit(update):
        return
    backup_progress()
    await update.message.reply_text("✅ Резервная копия прогресса создана.")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Заглушка, вы можете реализовать позже
    await update.message.reply_text("Статистика пока не реализована.")

# ========== ОБРАБОТЧИКИ ИНЛАЙН-КНОПОК ==========
async def inline_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_rate_limit(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = str(query.from_user.id)
    cat_key = context.user_data.get("current_category", DEFAULT_CATEGORY)
    cat = CATEGORIES[cat_key]
    words = cat["words"]

    # НАСТРОЙКИ
    if data == "show_count_settings":
        current = get_user_words_per_day(user_id)
        await query.edit_message_text(f"⚙️ *Выбор количества слов*\n\nТекущее: {current}\n\nВыберите новое количество:", parse_mode="Markdown", reply_markup=get_count_settings_buttons(current))
        return
    if data == "show_order_settings":
        current_order = get_user_word_order(user_id)
        await query.edit_message_text("⚙️ *Выбор порядка слов*\n\nВыберите желаемый порядок:", parse_mode="Markdown", reply_markup=get_order_settings_buttons(current_order))
        return
    if data == "show_daily_settings":
        daily = get_daily_settings(user_id)
        enabled = daily.get("enabled", False)
        daily_time = daily.get("time", "")
        await query.edit_message_text("⚙️ *Настройка ежедневной рассылки*\n\nВы можете включить или выключить рассылку, а также выбрать время.", parse_mode="Markdown", reply_markup=get_daily_settings_buttons(enabled, daily_time or "не выбрано"))
        return
    if data == "daily_toggle":
        daily = get_daily_settings(user_id)
        enabled = not daily.get("enabled", False)
        set_daily_settings(user_id, enabled=enabled)
        daily_time = daily.get("time", "")
        await query.edit_message_text(f"⚙️ *Настройка ежедневной рассылки*\n\nСтатус: {'✅ Включена' if enabled else '❌ Выключена'}", parse_mode="Markdown", reply_markup=get_daily_settings_buttons(enabled, daily_time or "не выбрано"))
        return
    if data == "daily_set_time":
        await query.edit_message_text("Выберите час для ежедневной рассылки (в UTC):", reply_markup=get_time_selection_buttons())
        return
    if data.startswith("daily_time_"):
        parts = data.split("_")
        time_str = parts[2] + ":" + parts[3]
        set_daily_settings(user_id, daily_time=time_str)
        daily = get_daily_settings(user_id)
        enabled = daily.get("enabled", False)
        await query.edit_message_text(f"✅ Время рассылки установлено: {time_str} UTC.\nНе забудьте включить рассылку в настройках, если ещё не сделали.", reply_markup=get_daily_settings_buttons(enabled, time_str))
        return
    if data.startswith("count_"):
        new_count = int(data.split("_")[1])
        set_user_words_per_day(user_id, new_count)
        await query.edit_message_text(f"✅ Количество слов установлено: {new_count} в день.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]]))
        return
    if data.startswith("order_"):
        new_order = data.split("_")[1]
        if new_order in ("en_ru", "ru_en"):
            set_user_word_order(user_id, new_order)
            order_text = "🇬🇧 Английский → Русский" if new_order == "en_ru" else "🇷🇺 Русский → Английский"
            await query.edit_message_text(f"✅ Порядок слов изменён на *{order_text}*.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]]))
        else:
            await query.answer("Неверный порядок.")
        return

    # ОСНОВНЫЕ ДЕЙСТВИЯ
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
                await query.message.reply_text(f"🎉 Поздравляю! Ты изучил все {total} слов в теме *{cat['name']}*!\nНачинаю заново: вот новые слова.", parse_mode="Markdown", reply_markup=get_after_words_buttons())
                cat_prog = get_category_progress(user_id, cat_key)
                used = cat_prog["used"]
                unused = get_unused_indices(used, total)
                available = [i for i in unused if i not in today_indices]
            else:
                await query.message.reply_text("📚 Сегодня вы уже получили все доступные новые слова. Завтра будут новые.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="back_to_menu")]]))
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
        context.user_data["pronounce_remaining"] = chosen_words.copy()
        context.user_data["current_batch_words"] = chosen_words

        streak, is_new_streak = update_streak(user_id)
        total_studied, _ = get_total_progress(user_id)
        new_achievements = check_achievements(user_id, total_studied, cat_key)

        order = get_user_word_order(user_id)
        msg = "*Ещё слова:*\n\n" + "\n".join(f"{i+1}. {format_word_by_order(w, order)}" for i, w in enumerate(chosen_words))
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_after_words_buttons())
        if is_new_streak:
            await query.message.reply_text(f"🔥 Твой стрик: {streak} дней! Так держать!")
        for ach_name, ach_desc in new_achievements:
            await query.message.reply_text(f"🏆 *Новое достижение:* {ach_name}\n_{ach_desc}_", parse_mode="Markdown")
        return

    if data == "back_to_menu":
        await query.message.reply_text("Возвращаюсь в главное меню.")
        await context.bot.send_message(chat_id=user_id, text="Клавиатура активна.", reply_markup=get_main_keyboard())
        return

    if data == "reverse_order":
        batch = context.user_data.get("current_batch_words")
        if not batch:
            await query.answer("Нет слов для переворота.", show_alert=True)
            return
        current_order = get_user_word_order(user_id)
        reverse_order = "ru_en" if current_order == "en_ru" else "en_ru"
        msg = "*Слова (обратный порядок):*\n\n" + "\n".join(f"{i+1}. {format_word_by_order(w, reverse_order)}" for i, w in enumerate(batch))
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_after_words_buttons())
        return

    if data == "pronounce":
        if not GTTS_AVAILABLE:
            await query.answer("Функция произношения временно недоступна.", show_alert=True)
            return
        last_words = context.user_data.get("last_pronounce_words", [])
        if not last_words:
            await query.answer("Нет слов для озвучивания. Сначала получите слова на сегодня.", show_alert=True)
            return
        remaining = context.user_data.get("pronounce_remaining", [])
        if not remaining:
            remaining = last_words.copy()
            context.user_data["pronounce_remaining"] = remaining
        if not remaining:
            await query.answer("Все слова из этой порции уже озвучены. Нажмите «Ещё слова» для новой порции.", show_alert=True)
            return
        word_obj = random.choice(remaining)
        remaining.remove(word_obj)
        context.user_data["pronounce_remaining"] = remaining
        text_to_speak = word_obj["word"]
        try:
            tts = gTTS(text=text_to_speak, lang='en')
            audio_bytes = io.BytesIO()
            tts.write_to_fp(audio_bytes)
            audio_bytes.seek(0)
            await query.message.reply_audio(audio=audio_bytes, filename=f"{text_to_speak}.mp3", caption=f"🔊 {text_to_speak}", title=text_to_speak, performer="English Bot")
            logger.info(f"Sent audio for {text_to_speak}")
        except Exception as e:
            logger.error(f"gTTS error: {e}")
            await query.answer("Не удалось сгенерировать произношение.", show_alert=True)
        return

    if data.startswith("confirm_reset_"):
        cat_to_reset = data.split("_", 2)[2]
        reset_category_progress(user_id, cat_to_reset)
        if cat_to_reset == context.user_data.get("current_category"):
            context.user_data["today_words"] = []
        await query.edit_message_text(f"✅ Прогресс в теме *{CATEGORIES[cat_to_reset]['name']}* сброшен.", parse_mode="Markdown")
        await context.bot.send_message(chat_id=user_id, text="Клавиатура активна.", reply_markup=get_main_keyboard())
        return

    if data == "cancel_reset":
        await query.edit_message_text("❌ Сброс отменён.")
        await context.bot.send_message(chat_id=user_id, text="Клавиатура активна.", reply_markup=get_main_keyboard())
        return

    if data == "exit_quiz":
        await query.edit_message_text("Викторина завершена. Возвращаюсь в меню.")
        await context.bot.send_message(chat_id=user_id, text="Клавиатура активна.", reply_markup=get_main_keyboard())
        return

    # ВИКТОРИНА
    if data == "quiz_all":
        context.user_data["quiz_category"] = "all"
        studied_words = get_all_studied_words(user_id)
        if not studied_words:
            await query.edit_message_text("❌ У вас ещё нет изученных слов. Сначала выучите несколько слов через «Слова на сегодня».", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]]))
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
        await query.edit_message_text(f"*Викторина (все категории)*\n\nСлово: **{word_obj['word']}**\n\nВыберите правильный перевод:", parse_mode="Markdown", reply_markup=get_quiz_buttons(word_obj["translation"], wrong))
        return

    if data.startswith("quiz_cat_"):
        cat_for_quiz = data.split("_", 2)[2]
        context.user_data["quiz_category"] = cat_for_quiz
        word_obj = get_random_studied_word(user_id, cat_for_quiz)
        if not word_obj:
            await query.edit_message_text(f"❌ В категории *{CATEGORIES[cat_for_quiz]['name']}* нет изученных слов. Сначала выучите несколько слов.", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]]))
            return
        context.user_data["last_quiz_word"] = word_obj
        context.user_data["last_quiz_cat"] = cat_for_quiz
        wrong = get_random_translations_for_quiz(cat_for_quiz, word_obj, 3)
        await query.edit_message_text(f"*Викторина* ({CATEGORIES[cat_for_quiz]['name']})\n\nСлово: **{word_obj['word']}**\n\nВыберите правильный перевод:", parse_mode="Markdown", reply_markup=get_quiz_buttons(word_obj["translation"], wrong))
        return

    if data.startswith("quiz_ans_"):
        chosen_trans = data.split("_", 2)[2]
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
                next_text = f"{result}\n\nСледующее слово: **{next_word['word']}**\n\nВыберите перевод:"
                await query.edit_message_text(next_text, parse_mode="Markdown", reply_markup=get_quiz_buttons(next_word["translation"], wrong))
            else:
                await query.edit_message_text(f"{result}\n\nВикторина завершена, так как нет больше изученных слов.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="exit_quiz")]]))
        else:
            studied = get_studied_indices(user_id, last_cat)
            if studied:
                next_idx = random.choice(studied)
                next_word = CATEGORIES[last_cat]["words"][next_idx]
                context.user_data["last_quiz_word"] = next_word
                wrong = get_random_translations_for_quiz(last_cat, next_word, 3)
                next_text = f"{result}\n\nСледующее слово: **{next_word['word']}**\n\nВыберите перевод:"
                await query.edit_message_text(next_text, parse_mode="Markdown", reply_markup=get_quiz_buttons(next_word["translation"], wrong))
            else:
                await query.edit_message_text(f"{result}\n\nВикторина завершена, так как в этой категории нет больше изученных слов.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 В меню", callback_data="exit_quiz")]]))
        return

    if data == "noop":
        await query.answer("Пока нет изученных слов.", show_alert=True)
        return

# ========== КОМАНДА /set_count ==========
async def set_count_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_rate_limit(update):
        return
    user_id = str(update.effective_user.id)
    current = get_user_words_per_day(user_id)
    await update.message.reply_text(f"Сколько слов вы хотите получать за раз? Введите число от 1 до 10.\nТекущее значение: {current}", reply_markup=get_main_keyboard())
    return COUNT_INPUT

async def set_count_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_rate_limit(update):
        return
    user_id = str(update.effective_user.id)
    try:
        count = int(update.message.text)
        if 1 <= count <= 10:
            set_user_words_per_day(user_id, count)
            await update.message.reply_text(f"✅ Количество слов установлено: {count} в день.", reply_markup=get_main_keyboard())
            return ConversationHandler.END
        else:
            await update.message.reply_text("Пожалуйста, введите число от 1 до 10.", reply_markup=get_main_keyboard())
            return COUNT_INPUT
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите целое число.", reply_markup=get_main_keyboard())
        return COUNT_INPUT

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_rate_limit(update):
        return
    await update.message.reply_text("Настройка отменена.", reply_markup=get_main_keyboard())
    return ConversationHandler.END

# ========== ОБРАБОТЧИК ВЫБОРА КАТЕГОРИИ ==========
async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_rate_limit(update):
        return
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    cat_key = query.data.split("_", 1)[1]
    if cat_key not in CATEGORIES:
        await query.edit_message_text("Неизвестная категория.")
        return
    context.user_data["current_category"] = cat_key
    context.user_data["today_words"] = []
    up = get_user_progress(user_id)
    up["current_category"] = cat_key
    set_user_progress(user_id, up)
    await query.edit_message_text(f"✅ Выбрана тема: *{CATEGORIES[cat_key]['name']}*.\nТеперь используй кнопки для изучения.", parse_mode="Markdown")
    await context.bot.send_message(chat_id=user_id, text="Клавиатура активна.", reply_markup=get_main_keyboard())

# ========== ЗАПУСК С ВЕБ-ХУКОМ ==========
async def main():
    app = Application.builder().token(TOKEN).updater(None).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("achievements", achievements_command))
    app.add_handler(CommandHandler("streak", streak_command))
    app.add_handler(CommandHandler("admin_stats", admin_stats))

    # ConversationHandler для /set_count
    count_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("set_count", set_count_start)],
        states={COUNT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_count_input)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(count_conv_handler)

    # Обработчики сообщений и колбэков
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_handler(CallbackQueryHandler(inline_buttons_callback, pattern="^(more_words|back_to_menu|reverse_order|pronounce|show_count_settings|show_order_settings|show_daily_settings|daily_toggle|daily_set_time|daily_time_\\d{2}_\\d{2}|count_\\d+|order_|confirm_reset_|cancel_reset|exit_quiz|quiz_all|quiz_cat_|quiz_ans_|noop)"))
    app.add_handler(CallbackQueryHandler(category_callback, pattern="^cat_"))

    # Планировщик
    scheduler = BackgroundScheduler()
    scheduler.add_job(flush_progress, 'interval', seconds=10)
    scheduler.add_job(lambda: asyncio.create_task(check_and_send_daily(app)), 'interval', minutes=1)
    scheduler.add_job(backup_progress, 'interval', hours=24)
    scheduler.start()
    atexit.register(flush_progress)

    # Вебхук
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
