import logging
import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.executor import start_webhook
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import os

API_TOKEN = os.getenv("BOT_TOKEN")  # задать в Render
ADMIN_IDS = [int(uid) for uid in os.getenv("ADMINS", "").split(",") if uid]
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

WB_API_URL = "https://search.wb.ru/exactmatch/ru/common/v5/search"

logging.basicConfig(level=logging.INFO)

# Google Sheets init
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
SHEET = client.open("WB Price Tracker").sheet1  # Название таблицы

bot = Bot(token=API_TOKEN)
bot.set_current(bot)
dp = Dispatcher(bot)

# ========== Команды ==========

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer("Добро пожаловать! Используйте /add для добавления товара и /list для просмотра.")

@dp.message_handler(commands=["ping"])
async def cmd_ping(message: types.Message):
    await message.answer("✅ Бот активен")

@dp.message_handler(commands=["add"])
async def cmd_add(message: types.Message):
    await message.answer("📦 Введите артикул Wildberries:")
    dp.register_message_handler(process_article, state="awaiting_article", user_id=message.from_user.id)

async def process_article(message: types.Message):
    article = message.text.strip()
    await message.answer("💰 Теперь введите желаемую цену:")
    dp.register_message_handler(lambda m: process_price(m, article), state="awaiting_price", user_id=message.from_user.id)

async def process_price(message: types.Message, article):
    try:
        price = float(message.text.replace(",", "."))
        await asyncio.to_thread(SHEET.append_row, [message.from_user.id, article, price, "", ""])
        await message.answer(f"✅ Товар {article} добавлен с ценой {price} ₽")
    except Exception as e:
        logging.error(e)
        await message.answer("❌ Неверная цена. Попробуйте снова.")

@dp.message_handler(commands=["list"])
async def cmd_list(message: types.Message):
    try:
        rows = await asyncio.to_thread(SHEET.get_all_records)
        user_rows = [r for r in rows if str(r['user_id']) == str(message.from_user.id)]
        if not user_rows:
            return await message.answer("📭 У вас нет добавленных товаров.")
        msg = "\n".join([f"🛒 {r['article']} — цель: {r['target_price']} ₽, текущая: {r.get('last_price', '—')}" for r in user_rows])
        await message.answer(msg)
    except Exception as e:
        logging.error(e)
        await message.answer("❌ Ошибка получения списка.")

@dp.message_handler(commands=["remove"])
async def cmd_remove(message: types.Message):
    await message.answer("🔢 Введите артикул, который хотите удалить:")
    dp.register_message_handler(process_remove, state="awaiting_remove", user_id=message.from_user.id)

async def process_remove(message: types.Message):
    article = message.text.strip()
    try:
        rows = await asyncio.to_thread(SHEET.get_all_values)
        for idx, row in enumerate(rows[1:], start=2):
            if row[0] == str(message.from_user.id) and row[1] == article:
                await asyncio.to_thread(SHEET.delete_rows, idx)
                await message.answer("✅ Удалено.")
                return
        await message.answer("❌ Не найдено.")
    except Exception as e:
        logging.error(e)
        await message.answer("❌ Ошибка при удалении.")

# ========== Админ рассылка ==========

@dp.message_handler(commands=["broadcast"])
async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return await message.answer("⛔ Нет доступа.")
    await message.answer("✏️ Введите текст сообщения для рассылки:")
    dp.register_message_handler(start_broadcast, state="awaiting_broadcast_text", user_id=message.from_user.id)

async def start_broadcast(message: types.Message):
    text = message.text
    await message.answer("📎 Пришлите фото или видео, или /skip если не нужно медиа.")

    async def process_media(msg):
        try:
            users = await asyncio.to_thread(SHEET.col_values, 1)
            users = list(set([int(u) for u in users if u.isdigit()]))

            for uid in users:
                try:
                    if msg.photo:
                        await bot.send_photo(uid, msg.photo[-1].file_id, caption=text)
                    elif msg.video:
                        await bot.send_video(uid, msg.video.file_id, caption=text)
                    else:
                        await bot.send_message(uid, text)
                except Exception as e:
                    logging.error(f"Ошибка рассылки {uid}: {e}")
            await msg.answer("✅ Рассылка завершена.")
        except Exception as e:
            logging.error(f"Broadcast error: {e}")
            await msg.answer("❌ Ошибка рассылки.")

    dp.register_message_handler(process_media, content_types=["photo", "video", "text"], user_id=message.from_user.id, state="awaiting_media")

# ========== Цикл проверки цен ==========

async def check_prices_loop():
    await asyncio.sleep(5)
    while True:
        try:
            records = await asyncio.to_thread(SHEET.get_all_records)
            for idx, rec in enumerate(records, start=2):
                try:
                    user_id = int(rec["user_id"])
                    article = rec["article"]
                    target = float(rec["target_price"])

                    async with aiohttp.ClientSession() as session:
                        async with session.get(WB_API_URL, params={"nm": article}, timeout=10) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                            current = data["data"]["products"][0]["priceU"] / 100

                    if current <= target:
                        await bot.send_message(user_id, f"📉 Цена на {article} сейчас {current} ₽ (цель: {target})")

                    await asyncio.to_thread(SHEET.update, f"D{idx}:E{idx}", [[current, datetime.utcnow().isoformat()]])
                except Exception as e:
                    logging.error(f"[Цикл] Ошибка строки {idx}: {e}")
        except Exception as e:
            logging.critical(f"[Цикл] Общая ошибка: {e}")
        await asyncio.sleep(1800)  # 30 минут

# ========== Webhook Startup ==========

async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    asyncio.create_task(check_prices_loop())
    logging.info("Бот запущен")

async def on_shutdown(dp):
    await bot.delete_webhook()
    logging.info("Бот остановлен")

if __name__ == "__main__":
    from aiogram.contrib.middlewares.logging import LoggingMiddleware
    dp.middleware.setup(LoggingMiddleware())
    executor.start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
    )