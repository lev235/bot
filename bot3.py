import os
import logging
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.utils.executor import start_webhook
import aiohttp
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === Настройки из окружения ===
API_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")  # например https://bot-ulgt.onrender.com
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH
PORT = int(os.getenv("PORT", "8080"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# === Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
gspread_client = gspread.authorize(creds)
sheet = gspread_client.open("wb_tracker").sheet1

# === Инициализация бота и диспетчера ===
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# === Состояния пользователей и админа ===
user_state = {}
admin_state = {}

# === Клавиатура ===
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

# === Помощник для запуска sync функций в async ===
async def run_sync(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

# === Получение цены с Wildberries (async) ===
async def get_price(nm):
    nm_str = str(nm)
    vol = nm_str[:3]
    part = nm_str[:5]
    urls = [
        f"https://basket-01.wb.ru/vol{vol}/part{part}/info/{nm_str}.json",
        f"https://basket-02.wb.ru/vol{vol}/part{part}/info/{nm_str}.json",
        f"https://basket-03.wb.ru/vol{vol}/part{part}/info/{nm_str}.json"
    ]
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in urls:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pu = data.get("price", {}).get("priceU")
                        su = data.get("price", {}).get("salePriceU")
                        if pu:
                            return pu // 100, (su or pu) // 100
            except Exception:
                continue
    return None, None

# === Хендлеры бота ===

@dp.message_handler(commands=["start"])
async def start(msg: types.Message):
    await msg.answer("Привет! Я отслеживаю цену товара на Wildberries.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def add(msg: types.Message):
    user_state[msg.from_user.id] = {'step': 'await_art'}
    await msg.answer("Введите артикул:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_art')
async def get_art(msg: types.Message):
    user_state[msg.from_user.id]['art'] = msg.text.strip()
    user_state[msg.from_user.id]['step'] = 'await_price'
    await msg.answer("Введите целевую цену:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_price')
async def get_price_target(msg: types.Message):
    try:
        price = float(msg.text.strip())
    except:
        return await msg.answer("Неверно. Введите число.")
    data = user_state.pop(msg.from_user.id)
    await run_sync(sheet.append_row, [msg.from_user.id, data['art'], price, '', 'FALSE'])
    await msg.answer("✅ Добавлено", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "📋 Список")
async def show_list(msg: types.Message):
    rows = await run_sync(sheet.get_all_records)
    items, markup = [], InlineKeyboardMarkup(row_width=2)
    for i, row in enumerate(rows, start=2):
        if int(row["UserID"]) == msg.from_user.id:
            items.append(f"{row['Artikel']} → ≤{row['TargetPrice']}₽ (посл.:{row['LastPrice'] or '–'})")
            markup.add(
                InlineKeyboardButton("Изм.", callback_data=f"edit_{i}"),
                InlineKeyboardButton("Уд.", callback_data=f"del_{i}")
            )
    if items:
        await msg.answer("\n".join(items), reply_markup=markup)
    else:
        await msg.answer("Список пуст.")

@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def delete_item(c: types.CallbackQuery):
    idx = int(c.data.split("_")[1])
    await run_sync(sheet.delete_rows, idx)
    await c.answer("Удалено.")
    await c.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_"))
async def edit_item(c: types.CallbackQuery):
    idx = int(c.data.split("_")[1])
    row = await run_sync(sheet.row_values, idx)
    user_state[c.from_user.id] = {'step': 'edit_price', 'idx': idx, 'art': row[1]}
    await c.answer()
    await c.message.answer(f"Новая цена для {row[1]} (была {row[2]}):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'edit_price')
async def new_price(msg: types.Message):
    try:
        price = float(msg.text.strip())
    except:
        return await msg.answer("Неверно. Введите число.")
    data = user_state.pop(msg.from_user.id)
    await run_sync(sheet.update_cell, data['idx'], 3, price)
    await run_sync(sheet.update_cell, data['idx'], 5, 'FALSE')
    await msg.answer("✅ Обновлено", reply_markup=main_kb)

# === Админ-рассылка ===

@dp.message_handler(lambda m: m.from_user.id == ADMIN_ID and m.text == "/broadcast")
async def bc_start(msg: types.Message):
    admin_state[ADMIN_ID] = {'step': 'await'}
    await msg.answer("Отправьте сообщение для рассылки:")

@dp.message_handler(lambda m: admin_state.get(ADMIN_ID, {}).get('step') == 'await', content_types=types.ContentTypes.ANY)
async def bc_preview(msg: types.Message):
    admin_state[ADMIN_ID] = {
        'step': 'conf',
        'type': msg.content_type,
        'text': msg.caption or msg.text or '',
        'file_id': (msg.photo[-1].file_id if msg.photo else (msg.video.file_id if msg.video else None))
    }
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅", callback_data="send_bc"),
        InlineKeyboardButton("❌", callback_data="cancel_bc")
    )
    if msg.content_type == "photo":
        await msg.answer_photo(admin_state[ADMIN_ID]['file_id'], caption=admin_state[ADMIN_ID]['text'], reply_markup=kb)
    elif msg.content_type == "video":
        await msg.answer_video(admin_state[ADMIN_ID]['file_id'], caption=admin_state[ADMIN_ID]['text'], reply_markup=kb)
    else:
        await msg.answer(admin_state[ADMIN_ID]['text'], reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data in ("send_bc", "cancel_bc"))
async def bc_action(c: types.CallbackQuery):
    await c.answer()
    if c.data == "cancel_bc":
        admin_state.pop(ADMIN_ID, None)
        await c.message.edit_text("🚫 Отменено.")
        return
    rows = await run_sync(sheet.get_all_values)
    users = set(r[0] for r in rows[1:])
    success, fail = 0, 0
    for uid in users:
        try:
            if admin_state[ADMIN_ID]['type'] == "photo":
                await bot.send_photo(uid, admin_state[ADMIN_ID]['file_id'], caption=admin_state[ADMIN_ID]['text'])
            elif admin_state[ADMIN_ID]['type'] == "video":
                await bot.send_video(uid, admin_state[ADMIN_ID]['file_id'], caption=admin_state[ADMIN_ID]['text'])
            else:
                await bot.send_message(uid, admin_state[ADMIN_ID]['text'])
            success += 1
        except:
            fail += 1
    await bot.send_message(ADMIN_ID, f"✅ {success} отправлено, ❌ {fail} ошибок.")
    admin_state.pop(ADMIN_ID, None)

# === Проверка цен (запускается периодически APScheduler) ===

async def check_prices():
    try:
        rows = await run_sync(sheet.get_all_records)
        sem = asyncio.Semaphore(5)

        async def process_row(i, row):
            try:
                uid = int(row['UserID'])
                art = row['Artikel']
                target = float(row['TargetPrice'])
                notified = row.get('Notified', "FALSE") == "TRUE"

                async with sem:
                    price, _ = await get_price(art)
                if price is None:
                    return
                await run_sync(sheet.update_cell, i, 4, price)  # LastPrice

                if price <= target and not notified:
                    await bot.send_message(uid, f"🔔 {art} подешевел до {price}₽\nhttps://www.wildberries.ru/catalog/{art}/detail.aspx")
                    await run_sync(sheet.update_cell, i, 5, 'TRUE')  # Notified
                elif price > target and notified:
                    await run_sync(sheet.update_cell, i, 5, 'FALSE')
            except Exception:
                logger.exception(f"Ошибка в check_prices для строки {i}")

        await asyncio.gather(*(process_row(i, row) for i, row in enumerate(rows, start=2)))
        logger.info("✅ check_prices завершена")
    except Exception:
        logger.exception("Ошибка в check_prices")

# === aiohttp сервер с webhook и ping ===

async def webhook_handler(request):
    try:
        data = await request.json()
        update = types.Update.to_object(data)
        await dp.process_update(update)
    except Exception:
        logger.exception("Ошибка при обработке webhook")
    return web.Response()

async def ping_handler(request):
    return web.Response(text="pong")

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, "interval", minutes=1)
    scheduler.start()
    app["scheduler"] = scheduler
    logger.info("🚀 Бот запущен")

async def on_shutdown(app):
    await bot.delete_webhook()
    app["scheduler"].shutdown()
    logger.info("🛑 Webhook удалён")

app = web.Application()
app.router.add_post(WEBHOOK_PATH, webhook_handler)
app.router.add_get("/ping", ping_handler)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, port=PORT)