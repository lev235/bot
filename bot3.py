import os
import logging
import json
import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.executor import start_webhook
from aiohttp import web, ClientSession
from dotenv import load_dotenv

import gspread
from oauth2client.service_account import ServiceAccountCredentials

load_dotenv()

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
WEBHOOK_HOST = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", default=8000))

# === INIT ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
Bot.set_current(bot)

logging.basicConfig(level=logging.INFO)

# === Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
SHEET = client.open("WB Price Tracker").sheet1  # создайте вручную с заголовками: user_id, article, target_price, current_price, updated_at

# === CONSTANTS ===
WB_API_URL = "https://search.wb.ru/exactmatch/ru/common/v5/search"

# === Команды ===
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer("Привет! Отправь /add чтобы добавить товар по артикулу для отслеживания цены.")

@dp.message_handler(commands=["add"])
async def cmd_add(message: types.Message):
    await message.answer("Введи артикул товара:")
    await Add.article.set()

@dp.message_handler(commands=["list"])
async def cmd_list(message: types.Message):
    try:
        records = await asyncio.to_thread(SHEET.get_all_records)
        user_items = [r for r in records if str(r['user_id']) == str(message.from_user.id)]
        if not user_items:
            await message.answer("У тебя пока нет добавленных товаров.")
            return
        text = "\n\n".join([
            f"📦 Артикул: {r['article']}\n🎯 Цель: {r['target_price']} ₽\n📉 Текущая: {r['current_price']} ₽"
            for r in user_items
        ])
        await message.answer(f"🧾 Твои товары:\n\n{text}")
    except Exception as e:
        logging.exception(e)
        await message.answer("Ошибка при получении списка.")

@dp.message_handler(commands=["remove"])
async def cmd_remove(message: types.Message):
    await message.answer("Введи артикул товара, который нужно удалить:")
    await Remove.article.set()

# === FSM ===
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
dp.storage = MemoryStorage()

class Add(StatesGroup):
    article = State()
    price = State()

class Remove(StatesGroup):
    article = State()

@dp.message_handler(state=Add.article)
async def add_article(message: types.Message, state: FSMContext):
    await state.update_data(article=message.text.strip())
    await message.answer("Теперь введи желаемую цену:")
    await Add.price.set()

@dp.message_handler(state=Add.price)
async def add_price(message: types.Message, state: FSMContext):
    data = await state.get_data()
    article = data['article']
    try:
        price = float(message.text.strip())
        await asyncio.to_thread(SHEET.append_row, [message.from_user.id, article, price, "", ""])
        await message.answer("✅ Товар добавлен!")
    except Exception:
        await message.answer("Некорректная цена. Попробуй снова.")
    await state.finish()

@dp.message_handler(state=Remove.article)
async def remove_article(message: types.Message, state: FSMContext):
    article = message.text.strip()
    try:
        records = await asyncio.to_thread(SHEET.get_all_records)
        for idx, rec in enumerate(records, start=2):
            if str(rec['user_id']) == str(message.from_user.id) and rec['article'] == article:
                await asyncio.to_thread(SHEET.delete_row, idx)
                await message.answer("✅ Товар удалён.")
                break
        else:
            await message.answer("❌ Товар не найден.")
    except Exception:
        await message.answer("Произошла ошибка при удалении.")
    await state.finish()

# === Admin: рассылка ===
@dp.message_handler(commands=["broadcast"])
async def admin_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Отправь сообщение, фото или видео для рассылки.")
    await Broadcast.waiting.set()

class Broadcast(StatesGroup):
    waiting = State()

@dp.message_handler(content_types=types.ContentTypes.ANY, state=Broadcast.waiting)
async def send_broadcast(message: types.Message, state: FSMContext):
    try:
        records = await asyncio.to_thread(SHEET.get_all_records)
        user_ids = list(set(r['user_id'] for r in records))
        sent = 0
        for uid in user_ids:
            try:
                if message.text:
                    await bot.send_message(uid, message.text)
                elif message.photo:
                    await bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption)
                elif message.video:
                    await bot.send_video(uid, message.video.file_id, caption=message.caption)
                sent += 1
            except Exception:
                continue
        await message.answer(f"📬 Разослано {sent} пользователям.")
    except Exception as e:
        logging.exception(e)
        await message.answer("Ошибка при рассылке.")
    await state.finish()

# === Пинг для UptimeRobot ===
@dp.message_handler(commands=["ping"])
async def cmd_ping(message: types.Message):
    await message.answer("pong")

# === Цикл проверки цен ===
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
                    async with ClientSession() as session:
                        async with session.get(WB_API_URL, params=params, timeout=10) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                            product = data["data"]["products"][0]
                            current = product["priceU"] / 100

                    if current <= target:
                        await bot.send_message(user_id, f"📉 Цена на {article} снизилась до {current} ₽!")

                    await asyncio.to_thread(SHEET.update, f"D{idx}:E{idx}", [[current, datetime.utcnow().isoformat()]])
                except Exception as e:
                    logging.warning(f"Ошибка на строке {idx}: {e}")
        except Exception as e:
            logging.error(f"Ошибка основного цикла: {e}")
        await asyncio.sleep(60)  # каждые 30 минут

# === Webhook setup ===
async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    asyncio.create_task(check_prices_loop())

async def on_shutdown(dp):
    logging.warning("Выключение...")
    await bot.delete_webhook()

# === Запуск ===
if __name__ == "__main__":
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
    )