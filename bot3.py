import logging
import os
import sys
import asyncio
import aiohttp

from aiogram.types import Update
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiohttp import web

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO)

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = 6882817679

RENDER_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")
if not RENDER_HOST:
    logging.error("RENDER_EXTERNAL_HOSTNAME не задан")
    sys.exit(1)

WEBHOOK_HOST = f"https://{RENDER_HOST}"
WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 8443))

# Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
gc = gspread.authorize(credentials)
sheet = gc.open("wb_tracker").sheet1

executor = ThreadPoolExecutor(max_workers=4)

bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

user_state = {}

# === Асинхронные обёртки для gspread ===
async def async_append_row(values):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, sheet.append_row, values)

async def async_update_cell(row, col, value):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, sheet.update_cell, row, col, value)

async def async_get_all_records():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, sheet.get_all_records)

async def async_delete_rows(idx):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(executor, sheet.delete_rows, idx)

async def async_row_values(idx):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, sheet.row_values, idx)

# === Получение цены с WB ===
async def get_price(nm):
    try:
        url = f'https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm}'
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                products = data.get('data', {}).get('products')
                if products:
                    item = products[0]
                    return item.get('priceU', 0) // 100, item.get('salePriceU', item.get('priceU', 0)) // 100
    except Exception as e:
        logging.warning(f"Ошибка при получении цены: {e}")
    return None, None

# === Обработчики сообщений ===

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    logging.info(f"Получено /start от {message.from_user.id}")
    await message.reply("Привет! Я отслеживаю цену товара на Wildberries.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def add_item_start(message: types.Message):
    user_state[message.from_user.id] = {'step': 'await_artikel'}
    await message.reply("Введите артикул товара (nm ID):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get("step") == "await_artikel")
async def step_artikel(message: types.Message):
    user_state[message.from_user.id]['artikel'] = message.text.strip()
    user_state[message.from_user.id]['step'] = 'await_price'
    await message.reply("Введите цену в рублях:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get("step") == "await_price")
async def step_price(message: types.Message):
    try:
        price = float(message.text.strip())
    except:
        return await message.reply("Неверный формат. Введите число.")
    data = user_state.pop(message.from_user.id)
    await async_append_row([message.from_user.id, data['artikel'], price, '', 'FALSE'])
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
    idx = int(callback.data.split('_')[1])
    try:
        await async_delete_rows(idx)
        await callback.answer("Удалено.")
        try:
            await callback.message.delete()
        except Exception as e:
            logging.warning(f"Не удалось удалить сообщение: {e}")
        await show_items(callback.message)
    except Exception as e:
        logging.warning(f"Ошибка удаления: {e}")
        await callback.answer("Ошибка при удалении.")

@dp.callback_query_handler(lambda c: c.data.startswith("edit_"))
async def edit_item(callback: types.CallbackQuery):
    idx = int(callback.data.split('_')[1])
    row = await async_row_values(idx)
    user_state[callback.from_user.id] = {'step': 'edit_price', 'row_idx': idx}
    await callback.answer()
    await callback.message.answer(f"Новая цена для {row[1]} (была: {row[2]}₽):")

# === Проверка цен ===

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
            await asyncio.sleep(0.2)
        except Exception as e:
            logging.warning(f"Ошибка в check_prices: {e}")

async def periodic_check_prices():
    iteration = 0
    while True:
        try:
            logging.info("Запуск проверки цен...")
            await check_prices()
            iteration += 1
            logging.info(f"Проверка цен выполнена {iteration} раз")
        except Exception:
            logging.exception("Ошибка в цикле проверки цен")
        await asyncio.sleep(3600)

# === AIOHTTP Webhook ===

async def on_startup(app):
    logging.info("Установка webhook...")
    await bot.set_webhook(WEBHOOK_URL)

async def on_shutdown(app):
    logging.info("Снятие webhook...")
    await bot.delete_webhook()
    await bot.session.close()

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

# === 🔁 Пинг-обработчик для Render / UptimeRobot ===
async def handle_ping(request):
    return web.Response(text="pong")

# === Создание и запуск приложения ===
app = web.Application()
app.router.add_post(WEBHOOK_PATH, handle_webhook)
app.router.add_get("/ping", handle_ping)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)
app.on_startup.append(lambda app: asyncio.create_task(periodic_check_prices()))
async def handle_root(request):
    return web.Response(text="Bot is running")

app.router.add_get("/", handle_root)

if __name__ == "__main__":
    logging.info("Запускаю aiohttp сервер...")
    logging.info(f"Открытие сервера на порту: {WEBAPP_PORT}")
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)