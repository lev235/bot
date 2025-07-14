import os
import logging
import json
import gspread
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from oauth2client.service_account import ServiceAccountCredentials

# === Настройки ===
API_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SPREADSHEET_NAME = 'wb_tracker'
GOOGLE_CREDS_JSON = 'credentials.json'

logging.basicConfig(level=logging.INFO)

# === Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_JSON, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SPREADSHEET_NAME).sheet1

# === Telegram Bot ===
bot = Bot(token=API_TOKEN)
Bot.set_current(bot)
dp = Dispatcher(bot)

# === Клавиатура ===
main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

user_state = {}
admin_state = {}

# === Получение цен с альтернативного WB API ===
async def get_price(nm):
    try:
        url = f'https://search.wb.ru/exactmatch/ru/common/v5/search?query={nm}&resultset=catalog'
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logging.error(f"[WB search] Статус != 200: {resp.status}, nm={nm}")
                    return None, None
                data = await resp.json()
                products = data.get("data", {}).get("products", [])
                if not products:
                    logging.warning(f"[WB search] Товар не найден: nm={nm}")
                    return None, None
                item = products[0]
                return item.get("priceU", 0) // 100, item.get("salePriceU", item.get("priceU", 0)) // 100
    except Exception as e:
        logging.exception(f"[WB search] Ошибка при получении цены nm={nm}: {e}")
        return None, None

# === Хендлеры пользователя ===
@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    await msg.answer("Привет! Я отслеживаю цену товара на Wildberries.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def add_item(msg: types.Message):
    user_state[msg.from_user.id] = {'step': 'await_artikel'}
    await msg.answer("Введите артикул товара:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_artikel')
async def receive_artikel(msg: types.Message):
    user_state[msg.from_user.id]['artikel'] = msg.text.strip()
    user_state[msg.from_user.id]['step'] = 'await_price'
    await msg.answer("Введите целевую цену в рублях:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_price')
async def receive_price(msg: types.Message):
    try:
        price = float(msg.text.strip())
    except:
        return await msg.answer("Неверный формат. Введите число.")
    data = user_state.pop(msg.from_user.id)
    sheet.append_row([msg.from_user.id, data['artikel'], price, '', 'FALSE'])
    await msg.answer(f"Товар {data['artikel']} добавлен!", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "📋 Список")
async def show_list(msg: types.Message):
    rows = sheet.get_all_records()
    items, markup = [], InlineKeyboardMarkup(row_width=2)
    for idx, row in enumerate(rows, start=2):
        if int(row['UserID']) == msg.from_user.id:
            items.append(f"📦 {row['Artikel']} → ≤ {row['TargetPrice']}₽ (посл.: {row['LastPrice'] or '–'})")
            markup.add(
                InlineKeyboardButton("Изменить", callback_data=f"edit_{idx}"),
                InlineKeyboardButton("Удалить", callback_data=f"del_{idx}")
            )
    if items:
        await msg.answer("\n".join(items), reply_markup=markup)
    else:
        await msg.answer("У вас пока нет отслеживаемых товаров.")

@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def handle_delete(c: types.CallbackQuery):
    idx = int(c.data.split("_")[1])
    sheet.delete_rows(idx)
    await c.answer("Удалено.")
    await c.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_"))
async def handle_edit(c: types.CallbackQuery):
    idx = int(c.data.split("_")[1])
    row = sheet.row_values(idx)
    user_state[c.from_user.id] = {'step': 'edit_price', 'row_idx': idx, 'artikel': row[1]}
    await c.answer()
    await c.message.answer(f"Введите новую цену для {row[1]} (текущая: {row[2]}₽):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'edit_price')
async def handle_edit_price(msg: types.Message):
    try:
        price = float(msg.text.strip())
    except:
        return await msg.answer("Неверный формат.")
    data = user_state.pop(msg.from_user.id)
    sheet.update_cell(data['row_idx'], 3, price)
    sheet.update_cell(data['row_idx'], 5, 'FALSE')
    await msg.answer("Цена обновлена.", reply_markup=main_kb)

# === Админ-рассылка ===
@dp.message_handler(lambda m: m.from_user.id == ADMIN_ID and m.text == "/broadcast")
async def start_broadcast(msg: types.Message):
    admin_state[ADMIN_ID] = {'step': 'await_content'}
    await msg.answer("Отправьте текст, фото или видео:")

@dp.message_handler(lambda m: admin_state.get(ADMIN_ID, {}).get('step') == 'await_content', content_types=types.ContentTypes.ANY)
async def collect_broadcast(msg: types.Message):
    admin_state[ADMIN_ID] = {
        'step': 'confirm',
        'content_type': msg.content_type,
        'text': msg.caption or msg.text or "",
        'file_id': (
            msg.photo[-1].file_id if msg.photo else
            msg.video.file_id if msg.video else None
        )
    }
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Отправить", callback_data="send_broadcast"),
        InlineKeyboardButton("❌ Отменить", callback_data="cancel_broadcast"),
        InlineKeyboardButton("✏️ Изменить", callback_data="edit_broadcast")
    )
    if msg.content_type == "photo":
        await msg.answer_photo(admin_state[ADMIN_ID]['file_id'], caption=admin_state[ADMIN_ID]['text'], reply_markup=markup)
    elif msg.content_type == "video":
        await msg.answer_video(admin_state[ADMIN_ID]['file_id'], caption=admin_state[ADMIN_ID]['text'], reply_markup=markup)
    else:
        await msg.answer(admin_state[ADMIN_ID]['text'], reply_markup=markup)

@dp.callback_query_handler(lambda c: c.data.startswith("send_") or c.data.startswith("cancel_") or c.data.startswith("edit_"))
async def broadcast_actions(c: types.CallbackQuery):
    action = c.data
    await c.answer()
    if action == "cancel_broadcast":
        admin_state.pop(ADMIN_ID, None)
        await c.message.edit_text("❌ Рассылка отменена.")
    elif action == "edit_broadcast":
        admin_state[ADMIN_ID]['step'] = 'await_content'
        await c.message.edit_text("✏️ Отправьте новое сообщение:")
    elif action == "send_broadcast":
        users = set(row[0] for row in sheet.get_all_values()[1:])
        s, f = 0, 0
        for uid in users:
            try:
                if admin_state[ADMIN_ID]['content_type'] == 'photo':
                    await bot.send_photo(uid, admin_state[ADMIN_ID]['file_id'], caption=admin_state[ADMIN_ID]['text'])
                elif admin_state[ADMIN_ID]['content_type'] == 'video':
                    await bot.send_video(uid, admin_state[ADMIN_ID]['file_id'], caption=admin_state[ADMIN_ID]['text'])
                else:
                    await bot.send_message(uid, admin_state[ADMIN_ID]['text'])
                s += 1
            except:
                f += 1
        admin_state.pop(ADMIN_ID, None)
        await bot.send_message(ADMIN_ID, f"✅ Рассылка завершена.\nУспешно: {s}\nОшибки: {f}")

# === Проверка цен ===
import asyncio
import logging

sem = asyncio.Semaphore(5)  # одновременно не более 5 запросов к WB

async def get_price_safe(nm):
    async with sem:
        return await get_price(nm)

async def check_prices():
    rows = sheet.get_all_records()
    tasks = []
    for i, row in enumerate(rows, start=2):
        nm = row['Artikel']
        tasks.append((i, int(row['UserID']), nm, float(row['TargetPrice']), row.get('Notified') == 'TRUE'))

    async def process_row(i, uid, nm, target, notified):
        price, _ = await get_price_safe(nm)
        if price is None:
            logging.warning(f"[WB search] Товар не найден или ошибка: nm={nm}")
        return
    sheet.update_cell(i, 4, price)  # Обновляем колонку с ценой
    if price <= target and not notified:
        try:
            await bot.send_message(uid, f"🔔 Товар {nm} подешевел до {price}₽!\nhttps://www.wildberries.ru/catalog/{nm}/detail.aspx")
            sheet.update_cell(i, 5, 'TRUE')  # Отмечаем, что уведомление отправлено
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления {nm} → {uid}: {e}")
    elif price > target and notified:
        sheet.update_cell(i, 5, 'FALSE')

# === aiohttp Webhook ===
app = web.Application()

async def webhook_handler(request):
    data = await request.json()
    update = types.Update(**data)
    await dp.process_update(update)
    return web.Response()

async def ping(request): return web.Response(text="pong")

app.router.add_post("/webhook", webhook_handler)
app.router.add_get("/ping", ping)

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, "interval", minutes=1)
    scheduler.start()
    logging.info("Бот и webhook запущены.")

async def on_shutdown(app):
    await bot.delete_webhook()
    logging.info("Webhook удалён")

app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    web.run_app(app, port=port)