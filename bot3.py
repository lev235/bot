import logging
import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.types import InputFile
from aiogram.utils.executor import start_webhook
import gspread
from datetime import datetime
from oauth2client.service_account import ServiceAccountCredentials
import os

# Логгирование
logging.basicConfig(level=logging.INFO)

# === Константы ===
API_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 123456789))  # замените на ваш Telegram ID
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")  # например, https://your-app.onrender.com
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 8000))
WB_API_URL = "https://search.wb.ru/exactmatch/ru/common/v5/search"

# === Инициализация бота и диспетчера ===
bot = Bot(token=API_TOKEN)
Bot.set_current(bot)
dp = Dispatcher(bot)

# === Подключение к Google Sheets ===
scope = ['https://spreadsheets.google.com/feeds',
         'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
gc = gspread.authorize(creds)
SHEET = gc.open("WB Price Tracker").sheet1  # Имя таблицы

# === Команды ===
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.reply("Привет! Отправь /add, чтобы добавить товар по артикулу.")

@dp.message_handler(commands=['add'])
async def cmd_add(message: types.Message):
    await message.reply("📦 Введи артикул товара:")
    dp.register_message_handler(get_article, state='awaiting_article')

async def get_article(message: types.Message):
    article = message.text.strip()
    await message.reply("🎯 Введи желаемую цену:")
    dp.register_message_handler(lambda m: get_price(m, article), state='awaiting_price')

async def get_price(message: types.Message, article):
    try:
        target_price = float(message.text.strip().replace(",", "."))
        SHEET.append_row([str(message.from_user.id), article, target_price, "", "", datetime.utcnow().isoformat()])
        await message.reply(f"✅ Товар {article} добавлен с желаемой ценой {target_price} ₽.")
    except ValueError:
        await message.reply("⚠️ Неверный формат цены. Попробуйте снова.")

@dp.message_handler(commands=['list'])
async def cmd_list(message: types.Message):
    records = SHEET.get_all_records()
    text = "📋 Ваши товары:\n"
    count = 0
    for r in records:
        if str(r['user_id']) == str(message.from_user.id):
            count += 1
            text += f"• {r['article']}: цель {r['target_price']} ₽, последняя цена {r['last_price']} ₽\n"
    if count == 0:
        text = "У вас нет отслеживаемых товаров."
    await message.reply(text)

@dp.message_handler(commands=['remove'])
async def cmd_remove(message: types.Message):
    await message.reply("✂️ Введите артикул товара, который нужно удалить:")
    dp.register_message_handler(lambda m: process_remove(m), state='awaiting_remove')

async def process_remove(message: types.Message):
    article = message.text.strip()
    data = SHEET.get_all_records()
    deleted = False
    for i, row in enumerate(data, start=2):
        if str(row['user_id']) == str(message.from_user.id) and row['article'] == article:
            SHEET.delete_rows(i)
            await message.reply(f"🗑️ Товар {article} удалён.")
            deleted = True
            break
    if not deleted:
        await message.reply("❌ Товар не найден.")

@dp.message_handler(commands=['ping'])
async def cmd_ping(message: types.Message):
    await message.reply("✅ Бот активен!")

# === Рассылка ===
@dp.message_handler(commands=['broadcast'])
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.reply("✉️ Отправьте текст, фото, видео или их комбинацию для рассылки:")
    dp.register_message_handler(process_broadcast, content_types=types.ContentTypes.ANY, state='awaiting_broadcast')

async def process_broadcast(message: types.Message):
    records = SHEET.get_all_records()
    sent = set()
    for r in records:
        user_id = r['user_id']
        if user_id in sent:
            continue
        try:
            if message.text:
                await bot.send_message(user_id, message.text)
            if message.photo:
                await bot.send_photo(user_id, message.photo[-1].file_id, caption=message.caption)
            if message.video:
                await bot.send_video(user_id, message.video.file_id, caption=message.caption)
            sent.add(user_id)
        except Exception as e:
            logging.error(f"Ошибка при отправке пользователю {user_id}: {e}")
    await message.reply("✅ Рассылка завершена.")

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
                        SHEET.update, f"D{idx}:E{idx}", [[current, datetime.utcnow().isoformat()]]
                    )
                except Exception as e:
                    logging.error(f"[Цикл] Ошибка для строки {idx}: {e}")
        except Exception as e:
            logging.critical(f"[Цикл] Общая ошибка: {e}")
        await asyncio.sleep(60 * 30)

# === Webhook и запуск ===
async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    asyncio.create_task(check_prices_loop())

async def on_shutdown(dp):
    logging.warning("Отключение...")
    await bot.delete_webhook()

if __name__ == '__main__':
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
    )