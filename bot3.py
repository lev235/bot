import os
import sys
import logging
import asyncio
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.contrib.fsm_storage.memory import MemoryStorage

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO)

# Переменные окружения
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
RENDER_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")
PORT = int(os.getenv("PORT", 10000))

if not TELEGRAM_TOKEN or not RENDER_HOST:
    logging.error("TELEGRAM_TOKEN или RENDER_EXTERNAL_HOSTNAME не заданы")
    sys.exit(1)

WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
WEBHOOK_URL = f"https://{RENDER_HOST}{WEBHOOK_PATH}"

# Google Sheets авторизация
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
gc = gspread.authorize(credentials)
sheet = gc.open("wb_tracker").sheet1
executor = ThreadPoolExecutor(max_workers=4)

# Инициализация бота и диспетчера
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

main_kb = ReplyKeyboardMarkup(resize_keyboard=True).add(
    KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список")
)

user_state = {}

# Глобальная сессия aiohttp для повторного использования
session: aiohttp.ClientSession = None

# Асинхронные обёртки для работы с Google Sheets через ThreadPoolExecutor
async def async_append_row(values):
    await asyncio.get_event_loop().run_in_executor(executor, sheet.append_row, values)

async def async_update_cell(row, col, value):
    await asyncio.get_event_loop().run_in_executor(executor, sheet.update_cell, row, col, value)

async def async_get_all_records():
    return await asyncio.get_event_loop().run_in_executor(executor, sheet.get_all_records)

async def async_delete_rows(idx):
    await asyncio.get_event_loop().run_in_executor(executor, sheet.delete_rows, idx)

async def async_row_values(idx):
    return await asyncio.get_event_loop().run_in_executor(executor, sheet.row_values, idx)

# Получение цены товара с Wildberries (с использованием глобальной сессии)
async def get_price(nm):
    url = f'https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm}'
    try:
        async with session.get(url, timeout=10) as resp:
            data = await resp.json()
            products = data.get('data', {}).get('products')
            if products:
                item = products[0]
                price_u = item.get('priceU', 0)
                sale_price_u = item.get('salePriceU', price_u)
                return price_u // 100, sale_price_u // 100
    except Exception as e:
        logging.warning(f"Ошибка при получении цены: {e}")
    return None, None

# Хендлеры команд
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    await message.reply("Привет! Я отслеживаю цену товара на Wildberries.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def add_item_start(message: types.Message):
    user_state[message.from_user.id] = {"step": "await_artikel"}
    await message.reply("Введите артикул товара (nm ID):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get("step") == "await_artikel")
async def step_artikel(message: types.Message):
    user_state[message.from_user.id]["artikel"] = message.text.strip()
    user_state[message.from_user.id]["step"] = "await_price"
    await message.reply("Введите цену в рублях:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get("step") == "await_price")
async def step_price(message: types.Message):
    try:
        price = float(message.text.strip())
    except:
        return await message.reply("Неверный формат. Введите число.")
    data = user_state.pop(message.from_user.id)
    await async_append_row([message.from_user.id, data["artikel"], price, '', 'FALSE'])
    await message.reply("Товар добавлен.", reply_markup=main_kb)

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get("step") == "edit_price")
async def step_edit_price(message: types.Message):
    try:
        new_price = float(message.text.strip())
    except:
        return await message.reply("Неверный формат.")
    data = user_state.pop(message.from_user.id)
    await async_update_cell(data['row_idx'], 3, new_price)
    await async_update_cell(data['row_idx'], 5, 'FALSE')
    await message.reply("Цена обновлена.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "📋 Список")
async def show_items(message: types.Message):
    rows = await async_get_all_records()
    markup = InlineKeyboardMarkup(row_width=2)
    items = []
    for idx, row in enumerate(rows, start=2):
        if int(row['UserID']) == message.from_user.id:
            items.append(f"📦 {row['Artikel']} ≤ {row['TargetPrice']}₽ (посл.: {row['LastPrice'] or '–'})")
            markup.add(
                InlineKeyboardButton("✏️", callback_data=f"edit_{idx}"),
                InlineKeyboardButton("🗑", callback_data=f"del_{idx}")
            )
    if items:
        await message.reply("\n".join(items), reply_markup=markup)
    else:
        await message.reply("Нет отслеживаемых товаров.", reply_markup=main_kb)

@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def delete_item(callback: types.CallbackQuery):
    idx = int(callback.data.split("_")[1])
    try:
        await async_delete_rows(idx)
        await callback.answer("Удалено.")
        await callback.message.delete()
        await show_items(callback.message)
    except Exception as e:
        logging.warning(f"Ошибка удаления: {e}")
        await callback.answer("Ошибка при удалении.")

@dp.callback_query_handler(lambda c: c.data.startswith("edit_"))
async def edit_item(callback: types.CallbackQuery):
    idx = int(callback.data.split("_")[1])
    row = await async_row_values(idx)
    user_state[callback.from_user.id] = {'step': 'edit_price', 'row_idx': idx}
    await callback.answer()
    await callback.message.answer(f"Новая цена для {row[1]} (была: {row[2]}₽):")

# Проверка цен
async def check_prices():
    rows = await async_get_all_records()
    for i, row in enumerate(rows, start=2):
        try:
            uid = int(row["UserID"])
            artikel = row["Artikel"]
            target = float(row["TargetPrice"])
            notified = row["Notified"] == "TRUE"
            price, _ = await get_price(artikel)
            if price is None:
                continue
            await async_update_cell(i, 4, price)
            if price <= target and not notified:
                url = f"https://www.wildberries.ru/catalog/{artikel}/detail.aspx"
                await bot.send_message(uid, f"🔔 {artikel} подешевел до {price}₽\n{url}")
                await async_update_cell(i, 5, 'TRUE')
            elif price > target and notified:
                await async_update_cell(i, 5, 'FALSE')
            # Небольшая пауза чтобы не перегрузить API и не заблокировать event loop
            await asyncio.sleep(0.3)
        except Exception as e:
            logging.warning(f"Ошибка в check_prices: {e}")

async def periodic_check_prices():
    while True:
        try:
            logging.info("Запуск проверки цен...")
            await check_prices()
            logging.info("Проверка цен выполнена")
        except Exception as e:
            logging.error(f"Ошибка в periodic_check_prices: {e}")
        # Пауза в 1 час
        await asyncio.sleep(3600)

# Webhook и пинг
async def handle_webhook(request):
    try:
        data = await request.json()
        update = Update(**data)
        Bot.set_current(bot)
        await dp.process_update(update)
    except Exception as e:
        logging.exception("Ошибка в webhook")
        return web.Response(status=500)
    return web.Response(text="OK")

async def handle_ping(request):
    return web.Response(text="pong")

async def handle_root(request):
    return web.Response(text="Bot is running")

async def on_startup(app):
    global session
    logging.info("Запускаю aiohttp.ClientSession...")
    session = aiohttp.ClientSession()
    logging.info("Установка webhook...")
    await bot.set_webhook(WEBHOOK_URL)
    app['price_checker'] = asyncio.create_task(periodic_check_prices())

async def on_shutdown(app):
    logging.info("Снятие webhook и завершение...")
    await bot.delete_webhook()
    await bot.session.close()
    if session:
        await session.close()
    if 'price_checker' in app:
        app['price_checker'].cancel()
        try:
            await app['price_checker']
        except asyncio.CancelledError:
            pass

app = web.Application()
app.router.add_post(WEBHOOK_PATH, handle_webhook)
app.router.add_get("/ping", handle_ping)
app.router.add_get("/", handle_root)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    logging.info("Запускаю aiohttp сервер...")
    logging.info(f"Открытие сервера на порту: {PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)