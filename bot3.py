import logging
import asyncio
import os
from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InputFile
from aiogram.utils.executor import start_webhook
import aiohttp
import gspread
from datetime import datetime
from oauth2client.service_account import ServiceAccountCredentials
from aiohttp import web

# === CONFIG ===
API_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(i) for i in os.getenv("ADMINS", "").split(",")]  # ENV ADMINS="123456789,987654321"

# === Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
SHEET = client.open("WB Price Bot").sheet1

# === Webhook ===
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL")  # в Render
WEBHOOK_PATH = f"/webhook/{API_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 5000))

# === Logging ===
logging.basicConfig(level=logging.INFO)

# === Init ===
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
Bot.set_current(bot)

# === WB API ===
WB_API_URL = "https://search.wb.ru/exactmatch/ru/common/v5/search"

# === Helpers ===

def get_user_records(user_id):
    records = SHEET.get_all_records()
    return [(i+2, rec) for i, rec in enumerate(records) if str(rec['user_id']) == str(user_id)]

# === Handlers ===

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer("👋 Отправь /add, чтобы добавить товар по артикулу и цене отслеживания.")

@dp.message_handler(commands=["add"])
async def cmd_add(message: types.Message):
    await message.answer("📦 Введите артикул товара:")
    dp.register_message_handler(process_article, state="add_article", user_id=message.from_user.id)

async def process_article(message: types.Message):
    article = message.text.strip()
    await message.answer("💰 Укажите желаемую цену:")
    dp.register_message_handler(lambda msg: process_price(msg, article), state="add_price", user_id=message.from_user.id)

async def process_price(message: types.Message, article):
    try:
        price = float(message.text.strip())
        row = [message.from_user.id, article, price, "", ""]
        SHEET.append_row(row)
        await message.answer(f"✅ Добавлено: {article} — {price}₽")
    except Exception:
        await message.answer("❌ Неверный формат цены.")

@dp.message_handler(commands=["list"])
async def cmd_list(message: types.Message):
    items = get_user_records(message.from_user.id)
    if not items:
        await message.answer("🔍 У вас нет отслеживаемых товаров.")
        return
    msg = "\n".join([f"{i+1}. {rec['article']} — цель: {rec['target_price']}₽, текущая: {rec.get('current_price','')}" for i, (_, rec) in enumerate(items)])
    await message.answer("📋 Ваши товары:\n" + msg)

@dp.message_handler(commands=["remove"])
async def cmd_remove(message: types.Message):
    items = get_user_records(message.from_user.id)
    if not items:
        await message.answer("❌ У вас нет товаров для удаления.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for idx, (_, rec) in enumerate(items):
        markup.add(f"{idx+1}. {rec['article']}")
    await message.answer("❓ Какой товар удалить?", reply_markup=markup)
    dp.register_message_handler(lambda msg: confirm_remove(msg, items), state="remove")

async def confirm_remove(message: types.Message, items):
    try:
        index = int(message.text.split(".")[0]) - 1
        row_idx, _ = items[index]
        SHEET.delete_rows(row_idx)
        await message.answer("🗑️ Удалено.", reply_markup=types.ReplyKeyboardRemove())
    except Exception:
        await message.answer("❌ Ошибка удаления.", reply_markup=types.ReplyKeyboardRemove())

@dp.message_handler(commands=["broadcast"])
async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("📨 Отправьте текст рассылки или фото/видео с подписью.")
    dp.register_message_handler(process_broadcast, state="broadcast", user_id=message.from_user.id)

async def process_broadcast(message: types.Message):
    users = list({rec["user_id"] for rec in SHEET.get_all_records()})
    sent, failed = 0, 0
    for uid in users:
        try:
            if message.photo:
                await bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption or "")
            elif message.video:
                await bot.send_video(uid, message.video.file_id, caption=message.caption or "")
            else:
                await bot.send_message(uid, message.text)
            sent += 1
        except Exception:
            failed += 1
    await message.answer(f"✅ Отправлено: {sent}, ❌ Ошибок: {failed}")

@dp.message_handler(commands=["ping"])
async def cmd_ping(message: types.Message):
    await message.answer("✅ Бот жив.")

# === Background Task ===

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

                    async with aiohttp.ClientSession() as session:
                        async with session.get(WB_API_URL, params={"nm": article}, timeout=10) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                            current = data["data"]["products"][0]["priceU"] / 100

                    if current <= target:
                        await bot.send_message(user_id, f"📉 Цена на товар {article} упала до {current} ₽ (цель: {target} ₽)")

                    await asyncio.to_thread(SHEET.update, f"D{idx}:E{idx}", [[current, datetime.utcnow().isoformat()]])
                except Exception as e:
                    logging.error(f"[Ошибка в цикле] {e}")
        except Exception as e:
            logging.critical(f"[Цикл] Общая ошибка: {e}")
        await asyncio.sleep(60 * 30)

# === Startup / Webhook ===

async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    asyncio.create_task(check_prices_loop())

async def on_shutdown(dp):
    await bot.delete_webhook()

# === Ping route for uptime robot ===
async def handle_ping(request):
    return web.Response(text="pong")

if __name__ == "__main__":
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, lambda request: dp._webhook.handle(request))
    app.router.add_get("/ping", handle_ping)

    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
        web_app=app
    )