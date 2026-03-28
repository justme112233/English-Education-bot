import os
import asyncio
import json
import logging
from starlette.applications import Starlette
from starlette.responses import Response, PlainTextResponse
from starlette.requests import Request
from starlette.routing import Route
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ========== НАСТРОЙКИ ==========
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
URL = os.environ.get("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")
PORT = int(os.getenv("PORT", 8000))

# Файлы с данными (они будут в репозитории)
WORDS_FILE = "words.json"
PROGRESS_FILE = "progress.json"

# ========== ЗАГРУЗКА СЛОВ ==========
with open(WORDS_FILE, "r", encoding="utf-8") as f:
    words_data = json.load(f)
DAYS = words_data["days"]
TOTAL_DAYS = len(DAYS)

# ========== ФУНКЦИИ ДЛЯ ПРОГРЕССА ==========
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_progress(progress):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)

def reset_progress_for_user(user_id: str):
    progress = load_progress()
    if user_id in progress:
        del progress[user_id]
        save_progress(progress)
        return True
    return False

def format_word(word_obj):
    return f"**{word_obj['word']}**    {word_obj['transcription']}    \"{word_obj['pronunciation']}\"    {word_obj['translation']}"

# ========== КОМАНДЫ БОТА ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    progress = load_progress()
    if user_id not in progress:
        progress[user_id] = 0
        save_progress(progress)

    # Создаём инлайн-кнопку
    keyboard = [[InlineKeyboardButton("🔄 Сбросить прогресс", callback_data="reset_progress")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "👋 Привет! Я буду присылать тебе 5 новых слов каждый день.\n\n"
        "Используй команду /today, чтобы получить слова текущего дня.\n"
        "Используй /repeat, чтобы повторить прошлый день.\n"
        "Используй /stats, чтобы посмотреть прогресс.\n\n"
        "Всего в курсе 60 дней. Удачи!",
        reply_markup=reply_markup
    )

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    progress = load_progress()
    current_day = progress.get(user_id, 0)
    if current_day >= TOTAL_DAYS:
        await update.message.reply_text("🎉 Поздравляю! Ты прошёл все 60 дней! Молодец!")
        return
    day_words = DAYS[current_day]
    msg = f"*День {current_day + 1}*\n\n" + "\n".join(f"{i+1}. {format_word(w)}" for i, w in enumerate(day_words))
    await update.message.reply_text(msg, parse_mode="Markdown")
    # Увеличиваем прогресс
    progress[user_id] = current_day + 1
    save_progress(progress)

async def repeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    progress = load_progress()
    last_day = progress.get(user_id, 0)
    if last_day == 0:
        await update.message.reply_text("Ты ещё не получил ни одного дня. Используй /today, чтобы начать.")
        return
    if last_day > TOTAL_DAYS:
        last_day = TOTAL_DAYS
    day_index = last_day - 1
    day_words = DAYS[day_index]
    msg = f"*Повтор: День {last_day}*\n\n" + "\n".join(f"{i+1}. {format_word(w)}" for i, w in enumerate(day_words))
    await update.message.reply_text(msg, parse_mode="Markdown")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    progress = load_progress()
    current_day = progress.get(user_id, 0)
    await update.message.reply_text(f"📊 Твой прогресс: пройдено {current_day} из {TOTAL_DAYS} дней.")

async def reset_progress_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if reset_progress_for_user(user_id):
        await update.message.reply_text("✅ Прогресс сброшен. Ты можешь начать заново с помощью /today.")
    else:
        await update.message.reply_text("ℹ️ У тебя не было сохранённого прогресса. Начинай с /today.")

async def reset_progress_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    if reset_progress_for_user(user_id):
        await query.edit_message_text(
            "✅ Прогресс сброшен. Ты можешь начать заново с помощью /today.\n\n"
            "Нажми /start, чтобы вернуться в главное меню."
        )
    else:
        await query.edit_message_text(
            "ℹ️ У тебя не было сохранённого прогресса. Начинай с /today.\n\n"
            "Нажми /start, чтобы вернуться в главное меню."
        )

# ========== ЗАПУСК С ВЕБ-ХУКОМ ==========
async def main():
    # Создаём приложение бота
    app = Application.builder().token(TOKEN).updater(None).build()

    # Регистрируем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today))
    app.add_handler(CommandHandler("repeat", repeat))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reset_progress", reset_progress_command))
    # Обработчик инлайн-кнопки
    app.add_handler(CallbackQueryHandler(reset_progress_callback, pattern="^reset_progress$"))

    # Устанавливаем веб-хук
    webhook_url = f"{URL}/telegram"
    await app.bot.set_webhook(webhook_url, allowed_updates=Update.ALL_TYPES)
    print(f"Webhook set to {webhook_url}")

    # Создаём Starlette-приложение для обработки веб-хуков
    async def telegram_webhook(request: Request) -> Response:
        await app.update_queue.put(Update.de_json(await request.json(), app.bot))
        return Response()

    async def healthcheck(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    starlette_app = Starlette(routes=[
        Route("/telegram", telegram_webhook, methods=["POST"]),
        Route("/healthcheck", healthcheck, methods=["GET"]),
    ])

    # Запускаем сервер
    import uvicorn
    config = uvicorn.Config(app=starlette_app, host="0.0.0.0", port=PORT)
    server = uvicorn.Server(config)

    async with app:
        await app.start()
        await server.serve()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
