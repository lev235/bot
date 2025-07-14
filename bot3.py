import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
import asyncio
import aiohttp
from aiohttp import web
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === Настройки ===
API_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SHEET_NAME = 'wb_tracker'
GOOGLE_CREDS = 'credentials.json'

logging.basicConfig(level=logging.INFO)

# Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS, scope)
gspread_client = gspread.authorize(creds)
sheet = gspread_client.open(SHEET_NAME).sheet1

# Bot и dispatcher
bot = Bot(token=API_TOKEN)
Bot.set_current(bot)
dp = Dispatcher(bot)

# Клавиатура
main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

user_state = {}
admin_state = {}

# Получение цены
async def get_price_wb(nm):
    nm_str = str(nm)
    vol = nm_str[:3]
    part = nm_str[:5]
    urls = [
        f"https://basket-01.wb.ru/vol{vol}/part{part}/info/{nm_str}.json",
        f"https://basket-02.wb.ru/vol{vol}/part{part}/info/{nm_str}.json",
        f"https://basket-03.wb.ru/vol{vol}/part{part}/info/{nm_str}.json",
    ]
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for u in urls:
            try:
                async with session.get(u) as resp:
                    if resp.status == 200:
                        d = await resp.json()
                        pu = d.get("price", {}).get("priceU")
                        su = d.get("price", {}).get("salePriceU")
                        if pu is not None:
                            return pu // 100, (su or pu) // 100
            except:
                pass
    return None, None

# Обёртки gspread в потоки
async def to_thread(f, *a, **kw):
    return await asyncio.to_thread(f, *a, **kw)

# Хендлеры
@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    await msg.answer("Привет! Я отслеживаю цену товара на Wildberries.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def add_item(msg: types.Message):
    user_state[msg.from_user.id] = {'step': 'await_artikel'}
    await msg.answer("Введите артикул товара:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_artikel')
async def recv_artikel(msg: types.Message):
    user_state[msg.from_user.id]['artikel'] = msg.text.strip()
    user_state[msg.from_user.id]['step'] = 'await_price'
    await msg.answer("Введите целевую цену:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_price')
async def recv_price(msg: types.Message):
    try:
        price = float(msg.text.strip())
    except:
        return await msg.answer("Неверный формат. Введите число.")
    data = user_state.pop(msg.from_user.id)
    await to_thread(sheet.append_row, [msg.from_user.id, data['artikel'], price, '', 'FALSE'])
    await msg.answer("Товар добавлен.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "📋 Список")
async def show_list(msg: types.Message):
    rows = await to_thread(sheet.get_all_records)
    items, markup = [], InlineKeyboardMarkup(row_width=2)
    for i, r in enumerate(rows, start=2):
        if int(r['UserID']) == msg.from_user.id:
            items.append(f"{r['Artikel']} → ≤{r['TargetPrice']}₽ (посл.:{r['LastPrice'] or '–'})")
            markup.add(
                InlineKeyboardButton("Изм.", callback_data=f"edit_{i}"),
                InlineKeyboardButton("Уд.", callback_data=f"del_{i}")
            )
    if items:
        await msg.answer("\n".join(items), reply_markup=markup)
    else:
        await msg.answer("Список пуст.")

@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def cb_del(c: types.CallbackQuery):
    idx = int(c.data.split("_")[1])
    await to_thread(sheet.delete_rows, idx)
    await c.answer("Удалено.")
    await c.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_"))
async def cb_edit(c: types.CallbackQuery):
    idx = int(c.data.split("_")[1])
    row = await to_thread(sheet.row_values, idx)
    user_state[c.from_user.id] = {'step': 'edit_price', 'idx': idx, 'artikel': row[1]}
    await c.answer()
    await c.message.answer(f"Новая цена для {row[1]} (была {row[2]}):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'edit_price')
async def msg_edit_price(msg: types.Message):
    try:
        price = float(msg.text.strip())
    except:
        return await msg.answer("Неверно.")
    data = user_state.pop(msg.from_user.id)
    await to_thread(sheet.update_cell, data['idx'], 3, price)
    await to_thread(sheet.update_cell, data['idx'], 5, 'FALSE')
    await msg.answer("Обновлено.", reply_markup=main_kb)

# Админ-рассылка
@dp.message_handler(lambda m: m.from_user.id == ADMIN_ID and m.text == "/broadcast")
async def bc_start(msg: types.Message):
    admin_state[ADMIN_ID] = {'step': 'await'}
    await msg.answer("Отправь контент:")

@dp.message_handler(lambda m: admin_state.get(ADMIN_ID, {}).get('step') == 'await', content_types=types.ContentTypes.ANY)
async def bc_content(msg: types.Message):
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
    act = c.data
    await c.answer()
    if act == "cancel_bc":
        admin_state.pop(ADMIN_ID, None)
        await c.message.edit_text("Отменено.")
    else:
        rows = await to_thread(sheet.get_all_values)
        users = set(r[0] for r in rows[1:])
        cnt, err = 0, 0
        for u in users:
            try:
                if admin_state[ADMIN_ID]['type'] == 'photo':
                    await bot.send_photo(u, admin_state[ADMIN_ID]['file_id'], caption=admin_state[ADMIN_ID]['text'])
                elif admin_state[ADMIN_ID]['type'] == 'video':
                    await bot.send_video(u, admin_state[ADMIN_ID]['file_id'], caption=admin_state[ADMIN_ID]['text'])
                else:
                    await bot.send_message(u, admin_state[ADMIN_ID]['text'])
                cnt += 1
            except:
                err += 1
        admin_state.pop(ADMIN_ID, None)
        await bot.send_message(ADMIN_ID, f"✅ Рассылка: {cnt} успешных, {err} ошибок.")

# Проверка цен
async def check_prices():
    rows = await to_thread(sheet.get_all_records)
    sem = asyncio.Semaphore(5)
    async def proc(i, r):
        try:
            uid = int(r["UserID"])
            art = r["Artikel"]
            target = float(r["TargetPrice"])
            notified = r["Notified"] == "TRUE"
            async with sem:
                price, _ = await get_price_wb(art)
            if price is None:
                return
            await to_thread(sheet.update_cell, i, 4, price)
            if price <= target and not notified:
                url = f"https://www.wildberries.ru/catalog/{art}/detail.aspx"
                await bot.send_message(uid, f"🔔 {art} подешевел до {price}₽\n{url}")
                await to_thread(sheet.update_cell, i, 5, 'TRUE')
            elif price > target and notified:
                await to_thread(sheet.update_cell, i, 5, 'FALSE')
        except:
            logging.exception("Ошибка в check_prices")
    await asyncio.gather(*(proc(i, r) for i, r in enumerate(rows, start=2)))
    logging.info("✅ check_prices завершена")

# Webhook
app = web.Application()

async def webhook_handler(request):
    data = await request.json()
    update = types.Update(**data)
    await dp.process_update(update)
    return web.Response()

async def ping_handler(request):
    return web.Response(text="pong")

app.router.add_post("/webhook", webhook_handler)
app.router.add_get("/ping", ping_handler)

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    sched = AsyncIOScheduler()
    sched.add_job(check_prices, "interval", minutes=1)
    sched.start()
    logging.info("🚀 Бот запущен")

async def on_shutdown(app):
    await bot.delete_webhook()
    logging.info("🛑 Webhook удалён")

app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, port=int(os.getenv("PORT", "8080")))