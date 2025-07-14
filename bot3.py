import os
import logging
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === Конфигурация ===
API_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Например: https://your-service.onrender.com/webhook
ADMIN_ID = int(os.getenv("ADMIN_ID", 123456789))

SPREADSHEET_NAME = 'wb_tracker'
GOOGLE_CREDS_JSON = 'credentials.json'

# === Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_JSON, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SPREADSHEET_NAME).sheet1

# === Telegram Bot ===
bot = Bot(token=API_TOKEN)
Bot.set_current(bot)  # Важно — сразу установить текущий бот
dp = Dispatcher(bot)

main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

user_state = {}

# === Получение цен с WB ===
def get_price(nm):
    url = f'https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm}'
    try:
        resp = requests.get(url)
        data = resp.json()
        products = data.get('data', {}).get('products')
        if products:
            item = products[0]
            return item.get('priceU', 0) // 100, item.get('salePriceU', 0) // 100
    except Exception as e:
        logging.error(f"Ошибка запроса WB: {e}")
    return None, None

# === Хендлеры ===
@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    await message.answer("Привет! Я отслеживаю цену товара на Wildberries. Используй кнопки ниже.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def handle_add(message: types.Message):
    user_state[message.from_user.id] = {'step': 'await_artikel'}
    await message.answer("Введите артикул товара (nm ID с Wildberries):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_artikel')
async def handle_artikel(message: types.Message):
    user_state[message.from_user.id]['artikel'] = message.text.strip()
    user_state[message.from_user.id]['step'] = 'await_price'
    await message.answer("Введите целевую цену (в рублях):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_price')
async def handle_price(message: types.Message):
    try:
        price = float(message.text.strip())
    except:
        return await message.answer("Неверный формат. Введите число.")
    data = user_state.pop(message.from_user.id)
    sheet.append_row([message.from_user.id, data['artikel'], price, '', 'FALSE'])
    await message.answer(f"Товар {data['artikel']} добавлен!", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "📋 Список")
async def handle_list(message: types.Message):
    rows = sheet.get_all_records()
    items, markup = [], InlineKeyboardMarkup(row_width=2)
    for idx, row in enumerate(rows, start=2):
        if int(row['UserID']) == message.from_user.id:
            items.append(f"📦 {row['Artikel']} → ≤ {row['TargetPrice']}₽ (посл.: {row['LastPrice'] or '–'})")
            markup.insert(InlineKeyboardButton("Изменить", callback_data=f"edit_{idx}"))
            markup.insert(InlineKeyboardButton("Удалить", callback_data=f"del_{idx}"))
    if items:
        await message.answer("\n".join(items), reply_markup=markup)
    else:
        await message.answer("У вас пока нет отслеживаемых товаров.")

@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def handle_delete(callback: types.CallbackQuery):
    idx = int(callback.data.split('_')[1])
    sheet.delete_rows(idx)
    await callback.answer("Удалено.")
    await callback.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_"))
async def handle_edit(callback: types.CallbackQuery):
    idx = int(callback.data.split('_')[1])
    row = sheet.row_values(idx)
    user_state[callback.from_user.id] = {'step': 'edit_price', 'row_idx': idx, 'artikel': row[1]}
    await callback.answer()
    await callback.message.answer(f"Введите новую цену для {row[1]} (текущая: {row[2]}₽):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'edit_price')
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
    Bot.set_current(bot)  # Установка текущего бота в контекст для фоновой задачи
    rows = sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        user_id, artikel = row['UserID'], row['Artikel']
        target_price = float(row['TargetPrice'])
        notified = row['Notified'] == 'TRUE'
        base_price, _ = get_price(artikel)
        if base_price is None:
            continue
        sheet.update_cell(i, 4, base_price)
        if base_price <= target_price and not notified:
            try:
                await bot.send_message(user_id, f"🔔 Товар {artikel} подешевел до {base_price}₽!\nhttps://www.wildberries.ru/catalog/{artikel}/detail.aspx")
                sheet.update_cell(i, 5, 'TRUE')
            except Exception as e:
                logging.error(f"Ошибка при отправке уведомления: {e}")
        elif base_price > target_price and notified:
            sheet.update_cell(i, 5, 'FALSE')

# === aiohttp и Webhook ===
app = web.Application()

async def webhook_handler(request):
    data = await request.json()
    update = types.Update(**data)
    Bot.set_current(bot)  # ВАЖНО: ставим текущий бот в контекст при обработке webhook
    await dp.process_update(update)
    return web.Response()

async def ping_handler(_):
    return web.Response(text="I am alive!")

app.router.add_post("/webhook", webhook_handler)
app.router.add_get("/ping", ping_handler)

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, "interval", minutes=1)
    scheduler.start()
    logging.info("Webhook установлен и бот запущен.")

async def on_shutdown(app):
    await bot.delete_webhook()
    logging.info("Webhook удалён.")

app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(app, port=int(os.getenv("PORT", 8080)))