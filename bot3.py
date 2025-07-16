import logging
import asyncio
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.utils.executor import start_webhook
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# Настройки
API_TOKEN = '7695770485:AAHzdIlBP2Az1i13Em2c26_7C6h22dS0y2A'
ADMIN_ID = 6882817679  # замените на ваш Telegram user_id
WEBHOOK_HOST = 'https://bot-ulgt.onrender.com'
WEBHOOK_PATH = '/webhook'
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH
WEBAPP_HOST = '0.0.0.0'
WEBAPP_PORT = 10000
WB_API_URL = "https://search.wb.ru/exactmatch/ru/common/v5/search"

# Логирование
logging.basicConfig(level=logging.INFO)

# Telegram bot
bot = Bot(token=API_TOKEN)
bot.set_current(bot)
dp = Dispatcher(bot)

# Подключение к Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
SHEET = client.open("WB Tracker").sheet1

# Команды
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer("👋 Привет! Отправь /add, чтобы отслеживать цену товара на Wildberries.")

@dp.message_handler(commands=["add"])
async def cmd_add(message: types.Message):
    await message.answer("📦 Введите артикул товара:")
    dp.register_message_handler(get_article, state="get_article")

async def get_article(message: types.Message):
    article = message.text.strip()
    await message.answer("💰 Укажите желаемую цену:")
    dp.register_message_handler(lambda m: save_product(m, article), state="get_price")

async def save_product(message: types.Message, article: str):
    try:
        price = float(message.text.strip())
        SHEET.append_row([str(message.from_user.id), article, price, "", ""])
        await message.answer("✅ Товар добавлен! Я сообщу, когда цена упадёт ниже.")
    except Exception as e:
        await message.answer(f"⚠️ Ошибка: {e}")
    finally:
        dp.unregister_message_handler(None, state="get_price")

@dp.message_handler(commands=["list"])
async def cmd_list(message: types.Message):
    try:
        user_id = str(message.from_user.id)
        records = await asyncio.to_thread(SHEET.get_all_records)
        items = [f"{r['article']} → {r['target_price']} ₽" for r in records if r['user_id'] == user_id]
        if items:
            await message.answer("📋 Ваши товары:\n" + "\n".join(items))
        else:
            await message.answer("📭 У вас нет добавленных товаров.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message_handler(commands=["remove"])
async def cmd_remove(message: types.Message):
    await message.answer("✏️ Введите артикул для удаления:")
    dp.register_message_handler(delete_product, state="delete_article")

async def delete_product(message: types.Message):
    try:
        article = message.text.strip()
        user_id = str(message.from_user.id)
        records = await asyncio.to_thread(SHEET.get_all_values)
        for idx, row in enumerate(records[1:], start=2):  # пропускаем заголовок
            if row[0] == user_id and row[1] == article:
                await asyncio.to_thread(SHEET.delete_row, idx)
                await message.answer("🗑️ Удалено!")
                break
        else:
            await message.answer("❌ Не найдено.")
    except Exception as e:
        await message.answer(f"❗ Ошибка: {e}")
    finally:
        dp.unregister_message_handler(None, state="delete_article")

@dp.message_handler(commands=["broadcast"])
async def cmd_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("🚫 Доступ запрещён.")
    await message.answer("📨 Отправьте текст рассылки (или фото/видео с подписью).")
    dp.register_message_handler(handle_broadcast, content_types=types.ContentTypes.ANY, state="broadcast")

async def handle_broadcast(message: types.Message):
    try:
        users = set(row[0] for row in SHEET.get_all_values()[1:])
        for uid in users:
            try:
                if message.photo:
                    await bot.send_photo(uid, message.photo[-1].file_id, caption=message.caption or "")
                elif message.video:
                    await bot.send_video(uid, message.video.file_id, caption=message.caption or "")
                elif message.text:
                    await bot.send_message(uid, message.text)
            except Exception as e:
                logging.error(f"Ошибка рассылки пользователю {uid}: {e}")
        await message.answer("✅ Рассылка завершена.")
    except Exception as e:
        await message.answer(f"❌ Ошибка рассылки: {e}")
    finally:
        dp.unregister_message_handler(None, state="broadcast")

@dp.message_handler(commands=["ping"])
async def cmd_ping(message: types.Message):
    await message.answer("✅ Бот работает.")

# Цикл отслеживания цен
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
                        async with session.get(WB_API_URL, params={"nm": article}) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json()
                            current = data["data"]["products"][0]["priceU"] / 100

                    if current <= target:
                        await bot.send_message(user_id, f"📉 Цена на товар {article} упала до {current} ₽")

                    await asyncio.to_thread(SHEET.update, f"D{idx}:E{idx}", [[current, datetime.utcnow().isoformat()]])
                except Exception as e:
                    logging.warning(f"Ошибка для {rec}: {e}")
        except Exception as e:
            logging.critical(f"Ошибка чтения таблицы: {e}")
        await asyncio.sleep(60 * 30)

# Webhook
async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    asyncio.create_task(check_prices_loop())

async def on_shutdown(dp):
    await bot.delete_webhook()

if __name__ == "__main__":
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
    )