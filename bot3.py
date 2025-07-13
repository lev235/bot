import logging
import requests
import asyncio
import os
import sys

from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = 6882817679  # замени на свой user_id

WEBHOOK_HOST = os.getenv('RENDER_EXTERNAL_HOSTNAME')
if not WEBHOOK_HOST:
    logging.error("Ошибка: переменная окружения RENDER_EXTERNAL_HOSTNAME не задана!")
    sys.exit(1)
WEBHOOK_HOST = f"https://{WEBHOOK_HOST}"
WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.environ.get("PORT", 8000))

# === Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
gc = None
sheet = None

# === Telegram bot ===
bot = Bot(token=TELEGRAM_TOKEN)
Bot.set_current(bot)
dp = Dispatcher(bot)
main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

user_state = {}

# === Получение цены ===
def get_price(nm):
    try:
        url = f'https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm}'
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
@dp.message_handler(commands=["start"])
async def start(message: types.Message):
    await message.reply("Привет! Я отслеживаю цену товара на Wildberries.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def add_item_start(message: types.Message):
    user_state[message.from_user.id] = {'step': 'await_artikel'}
    await message.reply("Введите артикул товара (nm ID):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_artikel')
async def add_item_artikel(message: types.Message):
    user_state[message.from_user.id]['artikel'] = message.text.strip()
    user_state[message.from_user.id]['step'] = 'await_price'
    await message.reply("Введите цену в рублях:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_price')
async def add_item_price(message: types.Message):
    try:
        price = float(message.text.strip())
    except:
        return await message.reply("Неверный формат. Введите число.")
    data = user_state[message.from_user.id]
    global sheet
    sheet.append_row([message.from_user.id, data['artikel'], price, '', 'FALSE'])
    await message.reply("Товар добавлен.", reply_markup=main_kb)
    user_state.pop(message.from_user.id, None)

@dp.message_handler(lambda m: m.text == "📋 Список")
async def show_items(message: types.Message):
    global sheet
    rows = sheet.get_all_records()
    markup = InlineKeyboardMarkup(row_width=2)
    items = []
    for idx, row in enumerate(rows, start=2):
        if int(row['UserID']) == message.from_user.id:
            items.append(f"📦 {row['Artikel']} ≤ {row['TargetPrice']}₽ (посл.: {row['LastPrice'] or '–'})")
            markup.insert(InlineKeyboardButton("✏️", callback_data=f"edit_{idx}"))
            markup.insert(InlineKeyboardButton("🗑", callback_data=f"del_{idx}"))
    if not items:
        await message.reply("Нет отслеживаемых товаров.", reply_markup=main_kb)
    else:
        await message.reply("\n".join(items), reply_markup=markup)

@dp.callback_query_handler(lambda c: c.data.startswith('del_'))
async def delete_item(callback: types.CallbackQuery):
    idx = int(callback.data.split('_')[1])
    global sheet
    sheet.delete_rows(idx)
    await callback.answer("Удалено.")
    await callback.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith('edit_'))
async def edit_item(callback: types.CallbackQuery):
    idx = int(callback.data.split('_')[1])
    global sheet
    row = sheet.row_values(idx)
    user_state[callback.from_user.id] = {'step': 'edit_price', 'row_idx': idx, 'artikel': row[1]}
    await callback.answer()
    await callback.message.answer(f"Новая цена для {row[1]} (была: {row[2]}₽):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'edit_price')
async def update_price(message: types.Message):
    try:
        new_price = float(message.text.strip())
    except:
        return await message.reply("Неверный формат.")
    state = user_state.pop(message.from_user.id)
    global sheet
    sheet.update_cell(state['row_idx'], 3, new_price)
    sheet.update_cell(state['row_idx'], 5, 'FALSE')
    await message.reply("Цена обновлена.", reply_markup=main_kb)

# === Проверка цен ===
async def check_prices():
    global sheet
    rows = sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        try:
            uid = int(row["UserID"])
            artikel = row["Artikel"]
            target = float(row["TargetPrice"])
            notified = row["Notified"] == "TRUE"
            price, _ = get_price(artikel)
            if price is None:
                continue
            sheet.update_cell(i, 4, price)
            if price <= target and not notified:
                url = f"https://www.wildberries.ru/catalog/{artikel}/detail.aspx"
                await bot.send_message(uid, f"🔔 {artikel} подешевел до {price}₽\n{url}")
                sheet.update_cell(i, 5, 'TRUE')
            elif price > target and notified:
                sheet.update_cell(i, 5, 'FALSE')
        except Exception as e:
            logging.warning(f"Ошибка: {e}")

# === Рассылка ===
@dp.message_handler(commands=['broadcast'])
async def broadcast_start(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    user_state[message.from_user.id] = {'step': 'await_broadcast'}
    await message.reply("Отправь текст, фото или видео:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_broadcast', content_types=types.ContentType.ANY)
async def preview_broadcast(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    state = {'step': 'confirm_broadcast'}
    markup = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅ Отправить", callback_data='broadcast_confirm'),
        InlineKeyboardButton("❌ Отменить", callback_data='broadcast_cancel')
    )
    if message.text:
        state.update({'type': 'text', 'text': message.text})
        await message.reply(f"Предпросмотр:\n{message.text}", reply_markup=markup)
    elif message.photo:
        state.update({'type': 'photo', 'file_id': message.photo[-1].file_id, 'caption': message.caption or ''})
        await bot.send_photo(message.chat.id, message.photo[-1].file_id, caption=message.caption, reply_markup=markup)
    elif message.video:
        state.update({'type': 'video', 'file_id': message.video.file_id, 'caption': message.caption or ''})
        await bot.send_video(message.chat.id, message.video.file_id, caption=message.caption, reply_markup=markup)
    user_state[message.from_user.id] = state

@dp.callback_query_handler(lambda c: c.data in ['broadcast_confirm', 'broadcast_cancel'])
async def handle_broadcast_confirm(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    state = user_state.get(callback.from_user.id)
    if callback.data == 'broadcast_cancel':
        user_state.pop(callback.from_user.id, None)
        await callback.message.edit_text("Рассылка отменена.")
        return

    sent, failed = 0, 0
    global sheet
    rows = sheet.get_all_records()
    for row in rows:
        uid = int(row["UserID"])
        try:
            if state["type"] == "text":
                await bot.send_message(uid, state["text"])
            elif state["type"] == "photo":
                await bot.send_photo(uid, state["file_id"], caption=state["caption"])
            elif state["type"] == "video":
                await bot.send_video(uid, state["file_id"], caption=state["caption"])
            sent += 1
        except Exception as e:
            logging.warning(f"Ошибка рассылки {uid}: {e}")
            failed += 1

    # Попытка обновить сообщение корректно в зависимости от типа содержимого
    try:
        if callback.message.text:
            await callback.message.edit_text(f"Рассылка завершена.\n✅ Успешно: {sent}\n❌ Ошибки: {failed}")
        else:
            # Если текста нет, то пробуем редактировать подпись (caption)
            await callback.message.edit_caption(f"Рассылка завершена.\n✅ Успешно: {sent}\n❌ Ошибки: {failed}")
    except Exception as e:
        logging.warning(f"Не удалось обновить сообщение: {e}")
        # Если редактировать не удалось — отправляем новое сообщение с результатом
        await callback.message.answer(f"Рассылка завершена.\n✅ Успешно: {sent}\n❌ Ошибки: {failed}")

    user_state.pop(callback.from_user.id, None)

    # Попытка обновить сообщение с результатом
    try:
        if callback.message.text:
            await callback.message.edit_text(f"Рассылка завершена.\n✅ Успешно: {sent}\n❌ Ошибки: {failed}")
        else:
            await callback.message.edit_caption(f"Рассылка завершена.\n✅ Успешно: {sent}\n❌ Ошибки: {failed}")
    except Exception as e:
        logging.warning(f"Не удалось обновить сообщение: {e}")
        await callback.message.answer(f"Рассылка завершена.\n✅ Успешно: {sent}\n❌ Ошибки: {failed}")

    user_state.pop(callback.from_user.id, None)

# === Webhook и сервер ===
async def on_startup(app):
    global gc, sheet
    logging.info(f"Устанавливаю webhook: {WEBHOOK_URL}")
    await bot.set_webhook(WEBHOOK_URL)
    
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    gc = gspread.authorize(creds)
    sheet = gc.open("wb_tracker").sheet1
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, "interval", minutes=1)
    scheduler.start()

async def on_shutdown(app):
    await bot.delete_webhook()

app = web.Application()
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

async def handle_webhook(request: web.Request):
    try:
        data = await request.json()
        logging.info(f"Получено обновление: {data}")
        Bot.set_current(bot)
        update = types.Update(**data)
        await dp.process_update(update)
    except Exception as e:
        logging.error(f"Ошибка обновления: {e}")
        return web.Response(status=500)
    return web.Response(text="OK")

app.router.add_post(WEBHOOK_PATH, handle_webhook)

# Пинг от UptimeRobot
async def ping(request):
    return web.Response(text="OK")

app.router.add_get("/ping", ping)

# === Запуск ===
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)