import os
import logging
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.utils.exceptions import BotBlocked, ChatNotFound
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiohttp

# --- Настройки и проверка окружения ---
API_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")  # Например https://mybot.onrender.com
WEBHOOK_PATH = "/webhook"
PORT = int(os.getenv("PORT", "8080"))
ADMIN_ID = os.getenv("ADMIN_ID")

if not API_TOKEN:
    raise RuntimeError("Ошибка: не задана переменная окружения BOT_TOKEN")
if not WEBHOOK_HOST:
    raise RuntimeError("Ошибка: не задана переменная окружения WEBHOOK_HOST")
if not ADMIN_ID:
    raise RuntimeError("Ошибка: не задана переменная окружения ADMIN_ID")

WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH
ADMIN_ID = int(ADMIN_ID)

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Google Sheets setup ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
gspread_client = gspread.authorize(creds)
sheet = gspread_client.open("wb_tracker").sheet1

# --- Инициализация бота и диспетчера ---
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# --- Вспомогательные структуры ---
user_states = {}   # для обработки шагов пользователя
admin_states = {}  # для админ-рассылки

# --- Клавиатуры ---
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

# --- Асинхронный вызов sync функций gspread ---
async def run_sync(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

# --- Функция получения цены товара с WB ---
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

# --- Обработчики команд и сообщений ---

@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я бот для отслеживания цен на Wildberries.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def cmd_add(message: types.Message):
    user_states[message.from_user.id] = {'step': 'await_art'}
    await message.answer("Введите артикул товара (nmId):")

@dp.message_handler(lambda m: user_states.get(m.from_user.id, {}).get('step') == 'await_art')
async def process_art(message: types.Message):
    art = message.text.strip()
    user_states[message.from_user.id] = {'step': 'await_price', 'art': art}
    await message.answer(f"Введите целевую цену для артикула {art} (число):")

@dp.message_handler(lambda m: user_states.get(m.from_user.id, {}).get('step') == 'await_price')
async def process_price(message: types.Message):
    try:
        price = float(message.text.strip())
    except ValueError:
        return await message.answer("Ошибка: введите число для цены.")

    state = user_states.pop(message.from_user.id)
    art = state['art']

    # Добавляем в Google Sheets
    await run_sync(sheet.append_row, [message.from_user.id, art, price, '', 'FALSE'])
    await message.answer(f"✅ Товар {art} добавлен с целевой ценой {price}₽", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "📋 Список")
async def cmd_list(message: types.Message):
    all_rows = await run_sync(sheet.get_all_records)
    user_rows = [r for r in all_rows if int(r["UserID"]) == message.from_user.id]

    if not user_rows:
        return await message.answer("Ваш список пуст.")

    text_lines = []
    markup = InlineKeyboardMarkup(row_width=2)
    for i, row in enumerate(user_rows, start=2):  # с 2-й строки (т.к. первая — заголовок)
        line = f"{row['Artikel']} → ≤{row['TargetPrice']}₽ (посл.: {row.get('LastPrice', '-')})"
        text_lines.append(line)
        markup.add(
            InlineKeyboardButton("Изм.", callback_data=f"edit_{i}"),
            InlineKeyboardButton("Уд.", callback_data=f"del_{i}")
        )

    await message.answer("\n".join(text_lines), reply_markup=markup)

# --- Обработчики callback_query для редактирования и удаления ---

@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def process_delete(call: types.CallbackQuery):
    idx = int(call.data.split("_")[1])
    await run_sync(sheet.delete_rows, idx)
    await call.answer("Удалено")
    await call.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_"))
async def process_edit(call: types.CallbackQuery):
    idx = int(call.data.split("_")[1])
    row = await run_sync(sheet.row_values, idx)
    user_states[call.from_user.id] = {'step': 'edit_price', 'idx': idx, 'art': row[1]}
    await call.answer()
    await call.message.answer(f"Введите новую целевую цену для артикула {row[1]} (было {row[2]}):")

@dp.message_handler(lambda m: user_states.get(m.from_user.id, {}).get('step') == 'edit_price')
async def process_new_price(message: types.Message):
    try:
        price = float(message.text.strip())
    except ValueError:
        return await message.answer("Ошибка: введите число.")

    state = user_states.pop(message.from_user.id)
    idx = state['idx']

    await run_sync(sheet.update_cell, idx, 3, price)  # обновляем цену
    await run_sync(sheet.update_cell, idx, 5, 'FALSE')  # сбрасываем уведомление
    await message.answer("✅ Обновлено", reply_markup=main_kb)

# --- Админ: рассылка ---

@dp.message_handler(lambda m: m.from_user.id == ADMIN_ID and m.text == "/broadcast")
async def admin_bc_start(message: types.Message):
    admin_states[ADMIN_ID] = {'step': 'await_msg'}
    await message.answer("Отправьте сообщение для рассылки всем пользователям.")

@dp.message_handler(lambda m: admin_states.get(ADMIN_ID, {}).get('step') == 'await_msg', content_types=types.ContentTypes.ANY)
async def admin_bc_preview(message: types.Message):
    admin_states[ADMIN_ID] = {
        'step': 'confirm',
        'type': message.content_type,
        'text': message.caption or message.text or '',
        'file_id': None
    }
    if message.photo:
        admin_states[ADMIN_ID]['file_id'] = message.photo[-1].file_id
    elif message.video:
        admin_states[ADMIN_ID]['file_id'] = message.video.file_id

    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅ Отправить", callback_data="send_bc"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel_bc")
    )

    if message.content_type == "photo":
        await message.answer_photo(admin_states[ADMIN_ID]['file_id'], caption=admin_states[ADMIN_ID]['text'], reply_markup=kb)
    elif message.content_type == "video":
        await message.answer_video(admin_states[ADMIN_ID]['file_id'], caption=admin_states[ADMIN_ID]['text'], reply_markup=kb)
    else:
        await message.answer(admin_states[ADMIN_ID]['text'], reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data in ("send_bc", "cancel_bc"))
async def admin_bc_action(call: types.CallbackQuery):
    await call.answer()
    if call.data == "cancel_bc":
        admin_states.pop(ADMIN_ID, None)
        await call.message.edit_text("🚫 Рассылка отменена.")
        return

    rows = await run_sync(sheet.get_all_values)
    users = set(row[0] for row in rows[1:])  # все user_id, кроме заголовка

    success = 0
    fail = 0
    bc = admin_states.get(ADMIN_ID)
    if not bc:
        await call.message.edit_text("Ошибка: данные рассылки потеряны.")
        return

    for uid in users:
        try:
            uid_int = int(uid)
            if bc['type'] == "photo":
                await bot.send_photo(uid_int, bc['file_id'], caption=bc['text'])
            elif bc['type'] == "video":
                await bot.send_video(uid_int, bc['file_id'], caption=bc['text'])
            else:
                await bot.send_message(uid_int, bc['text'])
            success += 1
        except (BotBlocked, ChatNotFound):
            fail += 1
        except Exception as e:
            logger.error(f"Ошибка при рассылке пользователю {uid}: {e}")
            fail += 1

    await bot.send_message(ADMIN_ID, f"Рассылка завершена: отправлено {success}, ошибок {fail}.")
    admin_states.pop(ADMIN_ID, None)
    await call.message.edit_text("✅ Рассылка завершена.")

# --- Фоновая проверка цен с ограничением параллелизма ---

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
                    await bot.send_message(uid, f"🔔 Товар {art} подешевел до {price}₽\nhttps://www.wildberries.ru/catalog/{art}/detail.aspx")
                    await run_sync(sheet.update_cell, i, 5, 'TRUE')  # Notified
                elif price > target and notified:
                    await run_sync(sheet.update_cell, i, 5, 'FALSE')
            except Exception:
                logger.exception(f"Ошибка в check_prices для строки {i}")

        await asyncio.gather(*(process_row(i, row) for i, row in enumerate(rows, start=2)))
        logger.info("check_prices выполнена успешно")
    except Exception:
        logger.exception("Ошибка при выполнении check_prices")

# --- aiohttp веб-сервер для webhook и ping ---

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
    logger.info(f"Устанавливаем webhook {WEBHOOK_URL}")
    await bot.set_webhook(WEBHOOK_URL)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, "interval", minutes=1)
    scheduler.start()
    app['scheduler'] = scheduler
    logger.info("Бот запущен, scheduler запущен")

async def on_shutdown(app):
    logger.info("Удаляем webhook и останавливаем scheduler")
    await bot.delete_webhook()
    app['scheduler'].shutdown()

app = web.Application()
app.router.add_post(WEBHOOK_PATH, webhook_handler)
app.router.add_get("/ping", ping_handler)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    logger.info(f"Запуск aiohttp на порту {PORT}")
    web.run_app(app, port=PORT)