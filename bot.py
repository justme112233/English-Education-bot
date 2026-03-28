import os
import asyncio
import json
from starlette.applications import Starlette
from starlette.responses import Response, PlainTextResponse
from starlette.requests import Request
from starlette.routing import Route
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# ========== НАСТРОЙКИ ==========
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
URL = os.environ.get("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")
PORT = int(os.getenv("PORT", 8000))

WORDS_FILE = "words.json"
PROGRESS_FILE = "progress.json"

# Загружаем слова (ожидаем структуру { "days": [ ... ] })
with open(WORDS_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)
DAYS = data["days"]
TOTAL_DAYS = len(DAYS)

# ========== ФУНКЦИИ ПРОГРЕССА ==========
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_progress(progress):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)

def get_user_day(user_id: str) -> int:
    progress = load_progress()
    return progress.get(user_id, 0)

def set_user_day(user_id: str, day: int):
    progress = load_progress()
    progress[user_id] = day
    save_progress(progress)

def reset_user_progress(user_id: str) -> bool:
    progress = load_progress()
    if user_id in progress:
        del progress[user_id]
        save_progress(progress)
        return True
    return False

def format_word(word_obj):
    return f"**{word_obj['word']}**    {word_obj['transcription']}    \"{word_obj['pronunciation']}\"    {word_obj['translation']}"

# ========== КЛАВИАТУРА ==========
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📚 Слова на сегодня"), KeyboardButton("🔄 Повторить")],
        [KeyboardButton("📊 Прогресс"), KeyboardButton("🗑 Сбросить прогресс")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ========== ОБРАБОТЧИКИ СООБЩЕНИЙ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    # Если пользователь новый, прогресс уже 0 (не сохраняем, просто проверим)
    await update.message.reply_text(
        "👋 Привет! Я помогу выучить 300 английских слов на тему путешествий.\n\n"
        "Просто нажимай кнопки:",
        reply_markup=get_main_keyboard()
    )

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = str(update.effective_user.id)

    if text == "📚 Слова на сегодня":
        day = get_user_day(user_id)
        if day >= TOTAL_DAYS:
            await update.message.reply_text(
                "🎉 Поздравляю! Ты прошёл все 60 дней! Можешь сбросить прогресс кнопкой ниже и начать заново.",
                reply_markup=get_main_keyboard()
            )
            return
        day_words = DAYS[day]
        msg = f"*День {day + 1}*\n\n" + "\n".join(f"{i+1}. {format_word(w)}" for i, w in enumerate(day_words))
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard())
        # Увеличиваем прогресс
        set_user_day(user_id, day + 1)

    elif text == "🔄 Повторить":
        day = get_user_day(user_id)
        if day == 0:
            await update.message.reply_text(
                "Ты ещё не получил ни одного дня. Нажми «Слова на сегодня», чтобы начать.",
                reply_markup=get_main_keyboard()
            )
            return
        day_index = day - 1
        day_words = DAYS[day_index]
        msg = f"*Повтор: День {day}*\n\n" + "\n".join(f"{i+1}. {format_word(w)}" for i, w in enumerate(day_words))
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard())

    elif text == "📊 Прогресс":
        day = get_user_day(user_id)
        await update.message.reply_text(
            f"📊 Твой прогресс: пройдено *{day}* из *{TOTAL_DAYS}* дней.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )

    elif text == "🗑 Сбросить прогресс":
        if reset_user_progress(user_id):
            await update.message.reply_text(
                "✅ Прогресс сброшен. Теперь можно начинать заново кнопкой «Слова на сегодня».",
                reply_markup=get_main_keyboard()
            )
        else:
            await update.message.reply_text(
                "ℹ️ У тебя не было сохранённого прогресса. Просто начинай!",
                reply_markup=get_main_keyboard()
            )

# ========== ЗАПУСК С ВЕБ-ХУКОМ ==========
async def main():
    app = Application.builder().token(TOKEN).updater(None).build()

    # Команды
    app.add_handler(CommandHandler("start", start))

    # Обработка нажатий на кнопки (reply keyboard)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))

    # Устанавливаем веб-хук
    webhook_url = f"{URL}/telegram"
    await app.bot.set_webhook(webhook_url, allowed_updates=Update.ALL_TYPES)
    print(f"Webhook set to {webhook_url}")

    # Starlette для веб-хуков
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
