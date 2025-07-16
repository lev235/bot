import logging
import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, executor, types
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

API_TOKEN = 'YOUR_BOT_TOKEN'
ADMIN_IDS = [123456789]  # ID админов

WEBHOOK_HOST = 'https://your-render-url.onrender.com'
WEBHOOK_PATH = '/webhook'
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

WB_API_URL = "https://search.wb.ru/exactmatch/ru/common/v5/search"

GOOGLE_CREDENTIALS_FILE = 'credentials.json'
SPREADSHEET_NAME = 'WBPriceBot'

# Инициализация
logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
Bot.set_current(bot)

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_FILE, scope)
client = gspread.authorize(creds)
SHEET = client.open(SPREADSHEET_NAME).sheet1

# ======================= Команды =======================

@dp.message_handler(commands=['start'])
async def start_cmd(message: types.Message):
    await message.answer("Привет! Пришли артикул Wildberries, который хочешь отслеживать. Используй /add")

@dp.message_handler(commands=['add'])
async def add_product(message: types.Message):
    await message.answer("Пришли артикул товара:")
    dp.register_message_handler(process_article, state="waiting_article")

async def process_article(message: types.Message):
    article = message.text.strip()
    await message.answer("Теперь введи целевую цену (₽):")
    dp.register_message_handler(lambda m: process_price(m, article), state="waiting_price")
    dp.current_state(user=message.from_user.id).set_state("waiting_price")

async def process_price(message: types.Message, article):
    try:
        price = float(message.text.strip())
        SHEET.append_row([str(message.from_user.id), article, price, '', ''])
        await message.answer(f"✅ Артикул {article} добавлен с целевой ценой {price} ₽.")
    except Exception as e:
        logging.error(f"Ошибка при добавлении: {e}")
        await message.answer("Произошла ошибка. Попробуйте ещё раз.")

@dp.message_handler(commands=['list'])
async def list_products(message: types.Message):
    user_id = str(message.from_user.id)
    try:
        records = SHEET.get_all_records()
        user_products = [f"{r['article']} — цель: {r['target_price']} ₽ (текущая: {r['last_price']} ₽)"
                         for r in records if r['user_id'] == user_id]
        if user_products:
            await message.answer("📋 Твои товары:\n" + '\n'.join(user_products))
        else:
            await message.answer("У тебя пока нет добавленных товаров.")
    except Exception as e:
        logging.error(f"/list ошибка: {e}")
        await message.answer("Ошибка при получении списка.")

@dp.message_handler(commands=['remove'])
async def remove_product(message: types.Message):
    await message.answer("Пришли артикул товара, который хочешь удалить:")
    dp.register_message_handler(process_remove, state="waiting_remove")
    dp.current_state(user=message.from_user.id).set_state("waiting_remove")

async def process_remove(message: types.Message):
    article = message.text.strip()
    user_id = str(message.from_user.id)
    try:
        all_rows = SHEET.get_all_values()
        for idx, row in enumerate(all_rows[1:], start=2):
            if row[0] == user_id and row[1] == article:
                SHEET.delete_row(idx)
                await message.answer(f"✅ Артикул {article} удалён.")
                return
        await message.answer("❌ Товар не найден.")
    except Exception as e:
        logging.error(f"/remove ошибка: {e}")
        await message.answer("Ошибка при удалении товара.")

@dp.message_handler(commands=['ping'])
async def ping(message: types.Message):
    await message.answer("✅ Бот работает.")

# ======================= Админ-рассылка =======================

@dp.message_handler(commands=['broadcast'])
async def broadcast_command(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("Пришли сообщение (текст / текст + фото / видео), которое разослать.")
    dp.register_message_handler(handle_broadcast, content_types=types.ContentTypes.ANY, state="waiting_broadcast")
    dp.current_state(user=message.from_user.id).set_state("waiting_broadcast")

async def handle_broadcast(message: types.Message):
    try:
        users = list({r[0] for r in SHEET.get_all_values()[1:] if r[0]})
        sent = 0
        for user_id in users:
            try:
                if message.video:
                    await bot.send_video(user_id, message.video.file_id, caption=message.caption or "")
                elif message.photo:
                    await bot.send_photo(user_id, message.photo[-1].file_id, caption=message.caption or "")
                elif message.text:
                    await bot.send_message(user_id, message.text)
                sent += 1
            except Exception as e:
                logging.warning(f"Ошибка при отправке {user_id}: {e}")
        await message.answer(f"✅ Отправлено {sent} пользователям.")
    except Exception as e:
        logging.error(f"Ошибка в рассылке: {e}")
        await message.answer("Произошла ошибка при рассылке.")

# ======================= Цикл проверки цен =======================

async def check_prices_loop():
    await asyncio.sleep(5)
    while True:
        try:
            records = await asyncio.to_thread(SHEET.get_all_records)
            for idx, rec in enumerate(records, start=2):
                try:
                    user_id = int(rec['user_id'])
                    article = rec['article']
                    target = float(rec['target_price'])

                    params = {"nm": article}
                    async with aiohttp.ClientSession() as session:
                        async with session.get(WB_API_URL, params=params, timeout=10) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                            current = data["data"]["products"][0]["priceU"] / 100

                    if current <= target:
                        await bot.send_message(user_id, f"📉 Цена на товар {article} упала до {current} ₽ (цель: {target} ₽)")

                    await asyncio.to_thread(SHEET.update, f"D{idx}:E{idx}", [[current, datetime.utcnow().isoformat()]])
                except Exception as e:
                    logging.error(f"Ошибка в записи {idx}: {e}")
        except Exception as e:
            logging.critical(f"[Loop] Ошибка основного цикла: {e}")
        await asyncio.sleep(1800)  # 30 мин

# ======================= Вебхук =======================

from aiohttp import web

async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    asyncio.create_task(check_prices_loop())

async def on_shutdown(dp):
    logging.warning("Shutting down...")
    await bot.delete_webhook()

app = web.Application()
from aiogram.contrib.middlewares.logging import LoggingMiddleware
dp.middleware.setup(LoggingMiddleware())

app.router.add_post(WEBHOOK_PATH, lambda request: dp.process_updates(request))
app.router.add_get("/ping", lambda request: web.Response(text="pong"))

if __name__ == '__main__':
    from aiogram import executor
    executor.set_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        app=app,
    )