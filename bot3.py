import logging
import requests
from aiohttp import web
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.dispatcher.webhook import get_new_configured_app
from aiogram.utils.executor import start_webhook

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === Настройки ===
API_TOKEN = '7695770485:AAHzdIlBP2Az1i13Em2c26_7C6h22dS0y2A'
WEBHOOK_HOST = 'https://имя-проекта.onrender.com'
WEBHOOK_PATH = '/webhook'
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

WEBAPP_HOST = '0.0.0.0'
WEBAPP_PORT = 10000

SPREADSHEET_NAME = 'wb_tracker'
GOOGLE_CREDS_JSON = 'credentials.json'
ADMIN_ID = 6882817679  # ← твой ID

# === Telegram setup ===
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# === Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_JSON, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SPREADSHEET_NAME).sheet1

main_kb = ReplyKeyboardMarkup(resize_keyboard=True).add(
    KeyboardButton("➕ Добавить"),
    KeyboardButton("📋 Список")
)

user_state = {}
pending_broadcasts = {}

# === Получение цены ===
def get_price(nm):
    url = f'https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm}'
    try:
        data = requests.get(url).json()
        product = data.get('data', {}).get('products', [{}])[0]
        price = product.get('priceU', 0) // 100
        return price
    except:
        return None

# === Хендлеры команд ===
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я отслеживаю цену товара на Wildberries.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def add_start(message: types.Message):
    user_state[message.from_user.id] = {'step': 'await_artikel'}
    await message.answer("Введите артикул товара:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_artikel')
async def add_artikel(message: types.Message):
    user_state[message.from_user.id]['artikel'] = message.text.strip()
    user_state[message.from_user.id]['step'] = 'await_price'
    await message.answer("Введите желаемую цену:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_price')
async def add_price(message: types.Message):
    try:
        price = float(message.text.strip())
    except:
        return await message.reply("Неверный формат.")
    artikel = user_state[message.from_user.id]['artikel']
    sheet.append_row([message.from_user.id, artikel, price, '', 'FALSE'])
    user_state.pop(message.from_user.id, None)
    await message.answer("Товар добавлен!", reply_markup=main_kb)

# === Проверка цен ===
async def check_prices():
    rows = sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        try:
            uid, art, target, last, notified = row['UserID'], row['Artikel'], float(row['TargetPrice']), row['LastPrice'], row['Notified']
            price = get_price(art)
            if price is None:
                continue
            sheet.update_cell(i, 4, price)
            if price <= target and notified != 'TRUE':
                url = f"https://www.wildberries.ru/catalog/{art}/detail.aspx"
                await bot.send_message(uid, f"🔔 Товар {art} подешевел до {price}₽\n{url}")
                sheet.update_cell(i, 5, 'TRUE')
            elif price > target and notified == 'TRUE':
                sheet.update_cell(i, 5, 'FALSE')
        except Exception as e:
            logging.error(f"Ошибка при проверке цен: {e}")

# === Пинг для UptimeRobot ===
@dp.message_handler(commands=["ping"])
async def ping(message: types.Message):
    await message.reply("✅ Бот активен!")

# === Webhook и запуск ===
async def on_startup(dispatcher):
    await bot.set_webhook(WEBHOOK_URL)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, 'interval', minutes=1)
    scheduler.start()

async def on_shutdown(dispatcher):
    await bot.delete_webhook()

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, get_new_configured_app(dispatcher=dp))
    loop = asyncio.get_event_loop()
    loop.create_task(on_startup(dp))
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)