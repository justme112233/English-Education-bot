import os
import asyncio
import json
import random
from starlette.applications import Starlette
from starlette.responses import Response, PlainTextResponse
from starlette.requests import Request
from starlette.routing import Route
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

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
    """Возвращает прогресс пользователя: { category_key: {"used": [индексы], "last": [объекты слов]} }"""
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

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📚 Слова на сегодня"), KeyboardButton("🔄 Повторить")],
        [KeyboardButton("📊 Прогресс"), KeyboardButton("🗑 Сбросить прогресс")],
        [KeyboardButton("🎯 Выбрать тему")]
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

# ========== ОБРАБОТЧИКИ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Устанавливаем текущую категорию в контексте (храним в памяти)
    if "current_category" not in context.user_data:
        context.user_data["current_category"] = DEFAULT_CATEGORY
    await update.message.reply_text(
        f"👋 Привет! Я помогу выучить английские слова по темам.\n\n"
        f"Текущая тема: *{CATEGORIES[context.user_data['current_category']]['name']}*\n\n"
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
                f"Начинаю заново: вот 5 новых слов.",
                parse_mode="Markdown",
                reply_markup=get_main_keyboard()
            )
            cat_prog = get_category_progress(user_id, cat_key)
            used = cat_prog["used"]
            unused = get_unused_indices(used, total)

        count = min(5, len(unused))
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
        cat_prog = get_category_progress(user_id, cat_key)
        done = len(cat_prog["used"])
        await update.message.reply_text(
            f"📊 Тема: *{cat['name']}*\nИзучено слов: *{done}* из *{total}*.",
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
    await query.edit_message_text(
        f"✅ Выбрана тема: *{CATEGORIES[cat_key]['name']}*.\n"
        f"Теперь используй кнопки для изучения.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

# ========== ЗАПУСК С ВЕБ-ХУКОМ ==========
async def main():
    app = Application.builder().token(TOKEN).updater(None).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
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
