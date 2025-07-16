import os
import logging
import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.types import InputFile
from aiogram.utils.executor import start_webhook

import gspread
from google.oauth2.service_account import Credentials
import aiohttp

# === Настройки ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = "/webhook"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 8000))

WB_API_URL = "https://card.wb.ru/cards/detail"

# === Бот и диспетчер ===
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# === Google Sheets ===
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
GC = gspread.service_account(filename="credentials.json")
SHEET = GC.open_by_key(SPREADSHEET_ID).sheet1

# === Команды ===

@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    await message.reply("👋 Добро пожаловать! Используй /add, /list, /remove для работы с товарами.")

@dp.message_handler(commands=["add"])
async def add_product(message: types.Message):
    try:
        _, article, target = message.text.strip().split()
        target_price = float(target)
    except:
        await message.reply("❗ Использование: /add <артикул> <целевая_цена>")
        return
    SHEET.append_row([message.from_user.id, article, target_price, "", ""])
    await message.reply(f"✅ Артикул {article} добавлен с целью {target_price} ₽")

@dp.message_handler(commands=["list"])
async def list_products(message: types.Message):
    user_id = str(message.from_user.id)
    records = SHEET.get_all_records()
    user_records = [r for r in records if str(r['user_id']) == user_id]
    if not user_records:
        await message.reply("🗃️ У вас нет добавленных товаров.")
        return
    reply = "📦 Ваши товары:\n\n"
    for i, r in enumerate(user_records, 1):
        reply += f"{i}. Артикул: {r['article']}, Цель: {r['target_price']} ₽, Последняя: {r.get('last_price', '-')}\n"
    await message.reply(reply)

@dp.message_handler(commands=["remove"])
async def remove_product(message: types.Message):
    try:
        _, article = message.text.strip().split()
    except:
        await message.reply("❗ Использование: /remove <артикул>")
        return
    user_id = str(message.from_user.id)
    data = SHEET.get_all_values()
    header = data[0]
    rows = data[1:]
    found = False
    for i, row in enumerate(rows, start=2):
        if row[0] == user_id and row[1] == article:
            SHEET.delete_rows(i)
            found = True
            break
    if found:
        await message.reply(f"🗑️ Артикул {article} удалён.")
    else:
        await message.reply(f"🚫 Артикул {article} не найден.")

@dp.message_handler(commands=["broadcast"])
async def broadcast_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.reply_to_message:
        await message.reply("Команда должна быть ответом на сообщение.")
        return
    reply = message.reply_to_message
    text = reply.text or reply.caption or ""
    records = SHEET.get_all_records()
    user_ids = list(set(str(r["user_id"]) for r in records))
    for uid in user_ids:
        try:
            if reply.photo:
                await bot.send_photo(uid, reply.photo[-1].file_id, caption=text)
            elif reply.video:
                await bot.send_video(uid, reply.video.file_id, caption=text)
            elif text:
                await bot.send_message(uid, text)
        except Exception as e:
            logging.warning(f"Ошибка рассылки пользователю {uid}: {e}")
    await message.reply("📣 Рассылка отправлена.")

# === Цикл проверки цен ===

async def check_prices_loop():
    await asyncio.sleep(5)
    while True:
        try:
            records = SHEET.get_all_records()
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
                        await bot.send_message(user_id, f"📉 Цена на {article} упала до {current} ₽ (цель: {target})")

                    SHEET.update(f"D{idx}:E{idx}", [[current, datetime.utcnow().isoformat()]])
                except Exception as e:
                    logging.error(f"[Цикл] Ошибка для строки {idx}: {e}")
        except Exception as e:
            logging.critical(f"[Цикл] Общая ошибка: {e}")
        await asyncio.sleep(60 * 30)  # каждые 30 минут

# === Webhook ===

async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL + WEBHOOK_PATH)
    asyncio.create_task(check_prices_loop())

async def on_shutdown(dp):
    await bot.delete_webhook()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
    )