import logging
import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import aiohttp

# --- Настройки ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
GOOGLE_SHEET_NAME = "wb_prices"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.environ.get("PORT", 10000))

# --- Логгинг ---
logging.basicConfig(level=logging.INFO)

# --- Инициализация бота и диспетчера ---
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(bot)

# --- Google Sheets ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name("google_credentials.json", scope)
client = gspread.authorize(credentials)
sheet = client.open(GOOGLE_SHEET_NAME).sheet1

# --- Хранилище задач пользователей ---
user_tasks = {}

# --- Команды ---
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("➕ Добавить товар", callback_data="add_item")
    )
    await message.answer("Привет! Я буду следить за ценами на Wildberries для тебя.", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data == "add_item")
async def add_item(callback_query: types.CallbackQuery):
    await bot.send_message(callback_query.from_user.id, "Введите ссылку на товар:")
    user_tasks[callback_query.from_user.id] = {"step": "waiting_for_link"}
    await bot.answer_callback_query(callback_query.id)

@dp.message_handler()
async def handle_message(message: types.Message):
    user_id = message.from_user.id
    if user_id in user_tasks:
        step = user_tasks[user_id].get("step")
        if step == "waiting_for_link":
            user_tasks[user_id]["link"] = message.text
            user_tasks[user_id]["step"] = "waiting_for_price"
            await message.answer("Введите желаемую цену:")
        elif step == "waiting_for_price":
            try:
                price = float(message.text)
                link = user_tasks[user_id]["link"]
                sheet.append_row([str(user_id), link, price])
                await message.answer("✅ Товар добавлен! Я начну отслеживание.")
                del user_tasks[user_id]
            except ValueError:
                await message.answer("Пожалуйста, введите число.")
    else:
        await message.answer("Нажмите /start, чтобы начать.")

# --- Проверка цен ---
async def check_prices():
    records = sheet.get_all_records()
    for row in records:
        user_id = row["user_id"]
        link = row["link"]
        target_price = float(row["price"])

        current_price = await get_wb_price(link)
        if current_price is not None and current_price <= target_price:
            try:
                await bot.send_message(user_id, f"💸 Цена на товар упала до {current_price}!\n{link}")
            except Exception as e:
                logging.warning(f"Ошибка при отправке сообщения пользователю {user_id}: {e}")

# --- Получение цены с Wildberries ---
async def get_wb_price(link):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(link, timeout=10) as resp:
                text = await resp.text()
                import re
                match = re.search(r'"price":\s?(\d+)', text)
                if match:
                    return float(match.group(1)) / 100
    except Exception as e:
        logging.warning(f"Ошибка при получении цены: {e}")
    return None

# --- Планировщик ---
scheduler = AsyncIOScheduler()
scheduler.add_job(check_prices, "interval", minutes=1)
scheduler.start()

# --- Webhook ---
async def handle_webhook(request):
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.process_update(update)
        return web.Response(text="OK")
    except Exception as e:
        logging.exception("Ошибка обработки webhook")
        return web.Response(status=500)

# --- Ping (для UptimeRobot) ---
async def handle_ping(request):
    return web.Response(text="pong")

# --- Запуск приложения ---
async def main():
    app = web.Application()
    app.router.add_post(f"/webhook/{TELEGRAM_TOKEN}", handle_webhook)
    app.router.add_get("/ping", handle_ping)

    await bot.set_webhook(f"{WEBHOOK_URL}/webhook/{TELEGRAM_TOKEN}")

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBAPP_HOST, WEBAPP_PORT)
    await site.start()

    logging.info("Бот запущен.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())