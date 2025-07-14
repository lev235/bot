import logging
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os

# === Конфигурация ===
API_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # пример: https://your-app.onrender.com/webhook
ADMIN_ID = int(os.getenv("ADMIN_ID"))

SPREADSHEET_NAME = 'wb_tracker'
GOOGLE_CREDS_JSON = 'credentials.json'

# === Google Sheets подключение ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_JSON, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SPREADSHEET_NAME).sheet1

# === Telegram setup ===
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

user_state = {}
broadcast_state = {}

# === Получение цен с WB ===
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

# === Хендлеры ===
@dp.message(commands=["start"])
async def start_cmd(message: types.Message):
    await message.answer("Привет! Я отслеживаю цену товара на Wildberries. Используй кнопки ниже.", reply_markup=main_kb)

@dp.message(lambda m: m.text == "➕ Добавить")
async def handle_add(message: types.Message):
    user_state[message.from_user.id] = {'step': 'await_artikel'}
    await message.answer("Введите артикул товара (nm ID с Wildberries):")

@dp.message(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_artikel')
async def handle_artikel(message: types.Message):
    user_state[message.from_user.id]['artikel'] = message.text.strip()
    user_state[message.from_user.id]['step'] = 'await_price'
    await message.answer("Введите цену до скидки (в рублях):")

@dp.message(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_price')
async def handle_price(message: types.Message):
    try:
        target = float(message.text.strip())
    except:
        return await message.answer("Неверный формат. Введите число.")
    data = user_state[message.from_user.id]
    sheet.append_row([message.from_user.id, data['artikel'], target, '', 'FALSE'])
    user_state.pop(message.from_user.id, None)
    await message.answer(f"Товар {data['artikel']} с целевой ценой ≤ {target}₽ добавлен.", reply_markup=main_kb)

@dp.message(lambda m: m.text == "📋 Список")
async def handle_list(message: types.Message):
    rows = sheet.get_all_records()
    markup = InlineKeyboardMarkup(row_width=2)
    items = []
    for idx, r in enumerate(rows, start=2):
        if int(r['UserID']) == message.from_user.id:
            items.append(f"📦 {r['Artikel']} → ≤ {r['TargetPrice']}₽ (посл.: {r['LastPrice'] or '–'})")
            markup.insert(InlineKeyboardButton("Изменить", callback_data=f"edit_{idx}"))
            markup.insert(InlineKeyboardButton("Удалить", callback_data=f"del_{idx}"))
    if not items:
        await message.answer("У вас пока нет товаров.")
    else:
        await message.answer("\n".join(items), reply_markup=markup)

@dp.callback_query(lambda c: c.data.startswith("del_"))
async def handle_delete(callback: types.CallbackQuery):
    idx = int(callback.data.split('_')[1])
    sheet.delete_rows(idx)
    await callback.answer("Удалено.")
    await callback.message.delete()

@dp.callback_query(lambda c: c.data.startswith("edit_"))
async def handle_edit(callback: types.CallbackQuery):
    idx = int(callback.data.split('_')[1])
    row = sheet.row_values(idx)
    user_state[callback.from_user.id] = {'step': 'edit_price', 'row_idx': idx, 'artikel': row[1]}
    await callback.answer()
    await callback.message.answer(f"Новая целевая цена для {row[1]} (текущая: {row[2]}₽):")

@dp.message(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'edit_price')
async def handle_edit_price(message: types.Message):
    try:
        price = float(message.text.strip())
    except:
        return await message.answer("Неверный формат.")
    data = user_state.pop(message.from_user.id)
    sheet.update_cell(data['row_idx'], 3, price)
    sheet.update_cell(data['row_idx'], 5, 'FALSE')
    await message.answer("Цена обновлена.", reply_markup=main_kb)

# === Проверка цен ===
async def check_prices():
    rows = sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        user_id = row['UserID']
        artikel = row['Artikel']
        target = float(row['TargetPrice'])
        notified = row['Notified'] == 'TRUE'
        base_price, _ = get_price(artikel)
        if base_price is None:
            continue
        sheet.update_cell(i, 4, base_price)
        if base_price <= target and not notified:
            url = f"https://www.wildberries.ru/catalog/{artikel}/detail.aspx"
            try:
                await bot.send_message(user_id, f"🔔 Товар {artikel} подешевел до {base_price}₽\n{url}")
                sheet.update_cell(i, 5, 'TRUE')
            except Exception as e:
                logging.error(f"Ошибка: {e}")
        elif base_price > target and notified:
            sheet.update_cell(i, 5, 'FALSE')

# === aiohttp сервер ===
app = web.Application()

# Webhook обработчик
async def handle_webhook(request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot, update)
    return web.Response()

# Пинг для UptimeRobot
async def handle_ping(request):
    return web.Response(text="I am alive!")

app.router.add_post("/webhook", handle_webhook)
app.router.add_get("/ping", handle_ping)

# === Запуск ===
async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, "interval", minutes=1)
    scheduler.start()
    logging.info("Webhook установлен и бот запущен.")

async def on_shutdown(app):
    await bot.delete_webhook()
    logging.warning("Webhook удалён.")

app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(app, port=int(os.getenv("PORT", 8080)))