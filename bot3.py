import logging
import requests
import asyncio
import os

from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.dispatcher.webhook import get_new_configured_app
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = 6882817679  # ← замени на свой user_id

# Webhook конфигурация
WEBHOOK_HOST = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}"
WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.environ.get('PORT', 8000))

# === Google Sheets подключение ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
gc = gspread.authorize(creds)
sheet = gc.open("wb_tracker").sheet1

# === Telegram setup ===
bot = Bot(token=TELEGRAM_TOKEN)
Bot.set_current(bot)  # <<< вот эта строка добавлена
dp = Dispatcher(bot)

main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

user_state = {}
pending_broadcasts = {}

def get_price(nm):
    url = f'https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm}'
    try:
        resp = requests.get(url)
        data = resp.json()
        products = data.get('data', {}).get('products')
        if products:
            item = products[0]
            priceU = item.get('priceU', 0)
            saleU = item.get('salePriceU', priceU)
            return priceU // 100, saleU // 100
    except:
        return None, None
    return None, None

# === Команды ===
@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    await message.reply("Привет! Я отслеживаю цену товара на Wildberries. Используй кнопки ниже.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def add_start(message: types.Message):
    user_state[message.from_user.id] = {'step': 'await_artikel'}
    await message.reply("Введите артикул товара (nm ID с Wildberries):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_artikel')
async def add_artikel(message: types.Message):
    user_state[message.from_user.id]['artikel'] = message.text.strip()
    user_state[message.from_user.id]['step'] = 'await_price'
    await message.reply("Введите цену (в рублях), ниже которой не должен быть товар:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_price')
async def add_price(message: types.Message):
    data = user_state[message.from_user.id]
    try:
        target = float(message.text.strip())
    except:
        return await message.reply("Неверный формат. Введите число.")
    artikel = data['artikel']
    sheet.append_row([message.from_user.id, artikel, target, '', 'FALSE'])
    await message.reply(f"Добавлен товар {artikel} с ценой ≤ {target}₽.", reply_markup=main_kb)
    user_state.pop(message.from_user.id, None)

@dp.message_handler(lambda m: m.text == "📋 Список")
async def show_list(message: types.Message):
    rows = sheet.get_all_records()
    items = []
    markup = InlineKeyboardMarkup(row_width=2)
    for idx, r in enumerate(rows, start=2):
        if int(r['UserID']) == message.from_user.id:
            text = f"📦 {r['Artikel']} → ≤ {r['TargetPrice']}₽ (посл.: {r['LastPrice'] or '–'})"
            items.append(text)
            markup.insert(InlineKeyboardButton("Изменить", callback_data=f"edit_{idx}"))
            markup.insert(InlineKeyboardButton("Удалить", callback_data=f"del_{idx}"))
    if not items:
        await message.reply("У вас пока нет отслеживаемых товаров.", reply_markup=main_kb)
    else:
        await message.reply("\n".join(items), reply_markup=markup)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('del_'))
async def handle_delete(callback_query: types.CallbackQuery):
    idx = int(callback_query.data.split('_')[1])
    sheet.delete_rows(idx)
    await callback_query.answer("Товар удалён.")
    await callback_query.message.delete()

@dp.callback_query_handler(lambda c: c.data and c.data.startswith('edit_'))
async def handle_edit(callback_query: types.CallbackQuery):
    idx = int(callback_query.data.split('_')[1])
    row = sheet.row_values(idx)
    user_state[callback_query.from_user.id] = {'step': 'edit_price', 'row_idx': idx, 'artikel': row[1]}
    await callback_query.answer()
    await callback_query.message.answer(f"Введите новую цену для {row[1]} (была: {row[2]}₽):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'edit_price')
async def edit_price(message: types.Message):
    data = user_state[message.from_user.id]
    try:
        new_price = float(message.text.strip())
    except:
        return await message.reply("Неверный формат.")
    idx = data['row_idx']
    sheet.update_cell(idx, 3, new_price)
    sheet.update_cell(idx, 5, 'FALSE')
    user_state.pop(message.from_user.id, None)
    await message.reply("Цена обновлена.", reply_markup=main_kb)

# === Проверка цен ===
async def check_prices():
    rows = sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        user_id = row['UserID']
        artikel = row['Artikel']
        target_price = float(row['TargetPrice'])
        notified = row['Notified'] == 'TRUE'
        base_price, sale_price = get_price(artikel)
        if base_price is None:
            continue
        sheet.update_cell(i, 4, base_price)
        if base_price <= target_price and not notified:
            try:
                url = f"https://www.wildberries.ru/catalog/{artikel}/detail.aspx"
                await bot.send_message(user_id, f"🔔 Товар {artikel} подешевел до {base_price}₽!\n{url}")
            except Exception as e:
                logging.error(f"Ошибка при уведомлении: {e}")
            sheet.update_cell(i, 5, 'TRUE')
        elif base_price > target_price and notified:
            sheet.update_cell(i, 5, 'FALSE')

# === Webhook запуск ===
async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, 'interval', minutes=1)
    scheduler.start()

async def on_shutdown(app):
    await bot.delete_webhook()

app = web.Application()
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)
async def handle_webhook(request: web.Request):
    data = await request.json()
    update = types.Update(**data)
    await dp.process_update(update)
    return web.Response(text="OK")

app.router.add_post(WEBHOOK_PATH, handle_webhook)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)