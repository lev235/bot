import os
import logging
import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import Update

import gspread
import aiohttp
from aiohttp import web
from google.oauth2.service_account import Credentials

# === Настройки ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # https://your-app.onrender.com
WEBHOOK_PATH = "/webhook"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 8000))

WB_API_URL = "https://card.wb.ru/cards/detail"

# === Логгирование ===
logging.basicConfig(level=logging.INFO)

# === Инициализация бота ===
bot = Bot(token=BOT_TOKEN)
Bot.set_current(bot)  # 🛠️ ВАЖНО: установка текущего экземпляра
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# === Google Sheets ===
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
        await asyncio.to_thread(SHEET.append_row, [message.from_user.id, article, target_price, "", ""])
        await message.reply(f"✅ Артикул {article} добавлен с целью {target_price} ₽")
    except Exception:
        await message.reply("❗ Использование: /add <артикул> <целевая_цена>")

@dp.message_handler(commands=["list"])
async def list_products(message: types.Message):
    try:
        user_id = str(message.from_user.id)
        records = await asyncio.to_thread(SHEET.get_all_records)
        user_records = [r for r in records if str(r['user_id']) == user_id]
        if not user_records:
            await message.reply("🗃️ У вас нет добавленных товаров.")
            return
        reply = "📦 Ваши товары:\n\n"
        for i, r in enumerate(user_records, 1):
            reply += f"{i}. Артикул: {r['article']}, Цель: {r['target_price']} ₽, Последняя: {r.get('last_price', '-')}\n"
        await message.reply(reply)
    except Exception as e:
        logging.error(f"/list ошибка: {e}")
        await message.reply("❗ Ошибка при получении списка товаров.")

@dp.message_handler(commands=["remove"])
async def remove_product(message: types.Message):
    try:
        _, article = message.text.strip().split()
        user_id = str(message.from_user.id)
        data = await asyncio.to_thread(SHEET.get_all_values)
        rows = data[1:]
        for i, row in enumerate(rows, start=2):
            if row[0] == user_id and row[1] == article:
                await asyncio.to_thread(SHEET.delete_rows, i)
                await message.reply(f"🗑️ Артикул {article} удалён.")
                return
        await message.reply(f"🚫 Артикул {article} не найден.")
    except Exception:
        await message.reply("❗ Использование: /remove <артикул>")

@dp.message_handler(commands=["broadcast"])
async def broadcast_cmd(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.reply_to_message:
        await message.reply("Команда должна быть ответом на сообщение.")
        return

    reply = message.reply_to_message
    text = reply.text or reply.caption or ""
    try:
        records = await asyncio.to_thread(SHEET.get_all_records)
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
    except Exception as e:
        logging.error(f"[broadcast] Ошибка рассылки: {e}")
        await message.reply("❗ Ошибка при рассылке.")

# === Фоновая проверка цен ===

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
                        await bot.send_message(user_id, f"📉 Цена на {article} упала до {current} ₽ (цель: {target})")

                    await asyncio.to_thread(
                        SHEET.update,
                        f"D{idx}:E{idx}",
                        [[current, datetime.utcnow().isoformat()]]
                    )
                except Exception as e:
                    logging.error(f"[Цикл] Ошибка для строки {idx}: {e}")
        except Exception as e:
            logging.critical(f"[Цикл] Общая ошибка: {e}")
        await asyncio.sleep(60 * 30)

# === Webhook-обработчики ===

async def ping(request):
    return web.Response(text="pong")

async def handle_webhook(request):
    try:
        req_data = await request.json()
        update = Update.to_object(req_data)
        Bot.set_current(bot)  # 🛠️ Устанавливаем bot перед обработкой update
        await dp.process_update(update)
        return web.Response()
    except Exception as e:
        logging.error(f"Ошибка обработки webhook: {e}")
        return web.Response(status=500)

# === Запуск ===

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL + WEBHOOK_PATH)
    asyncio.create_task(check_prices_loop())

async def on_shutdown(app):
    await bot.delete_webhook()

def main():
    app = web.Application()
    app.router.add_get("/ping", ping)
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)

if __name__ == "__main__":
    main()