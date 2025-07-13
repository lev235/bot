import logging
import asyncio
import os
import sys
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === Настройки ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = 6882817679  # замените на свой ID
WEBHOOK_HOST = os.getenv('RENDER_EXTERNAL_HOSTNAME')
if not WEBHOOK_HOST:
    logging.error("Ошибка: переменная RENDER_EXTERNAL_HOSTNAME не задана")
    sys.exit(1)
WEBHOOK_URL = f"https://{WEBHOOK_HOST}/webhook/{TELEGRAM_TOKEN}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.environ.get("PORT", 8000))

# === Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
gc = None
sheet = None

# === Telegram ===
bot = Bot(token=TELEGRAM_TOKEN)
Dispatcher.set_current = bot  # noqa
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

user_state = {}

# === Получение цены ===
async def get_price(nm):
    url = f'https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm}'
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
        pr = data.get('data', {}).get('products')
        if pr:
            item = pr[0]
            return item.get('priceU', 0)//100, item.get('salePriceU', item.get('priceU', 0))//100
    except Exception as e:
        logging.warning("Ошибка запроса цены: %s", e)
    return None, None

# === Хендлеры ===
@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    await msg.reply("Привет! Я отслеживаю цену товара на Wildberries.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def cmd_add_start(m: types.Message):
    user_state[m.from_user.id] = {'step': 'await_artikel'}
    await m.reply("Введите артикул товара (nm ID):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_artikel')
async def cmd_add_artikel(m: types.Message):
    st = user_state[m.from_user.id]
    st['artikel'] = m.text.strip()
    st['step'] = 'await_price'
    await m.reply("Введите целевую цену в рублях:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_price')
async def cmd_add_price(m: types.Message):
    try:
        price = float(m.text.strip())
    except ValueError:
        return await m.reply("Неверный формат, введите число.")
    st = user_state.pop(m.from_user.id)
    sheet.append_row([m.from_user.id, st['artikel'], price, '', 'FALSE'])
    await m.reply("Товар добавлен.", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "📋 Список")
async def cmd_list(m: types.Message):
    rows = sheet.get_all_records()
    kb = InlineKeyboardMarkup(row_width=2)
    msgs = []
    for i, r in enumerate(rows, start=2):
        if int(r['UserID']) == m.from_user.id:
            msgs.append(f"📦 {r['Artikel']} ≤ {r['TargetPrice']}₽ (посл.: {r.get('LastPrice') or '–'})")
            kb.insert(InlineKeyboardButton("✏️", callback_data=f"edit_{i}"))
            kb.insert(InlineKeyboardButton("🗑", callback_data=f"del_{i}"))
    if not msgs:
        await m.reply("Нет отслеживаемых товаров.", reply_markup=main_kb)
    else:
        await m.reply("\n".join(msgs), reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def cb_del(c: types.CallbackQuery):
    idx = int(c.data.split("_")[1])
    try:
        sheet.delete_rows(idx)
        await c.answer("Удалено.")
        # можно заново показать список
    except Exception as e:
        logging.warning("Delete error: %s", e)
        await c.answer("Ошибка удаления.")

@dp.callback_query_handler(lambda c: c.data.startswith("edit_"))
async def cb_edit(c: types.CallbackQuery):
    idx = int(c.data.split("_")[1])
    row = sheet.row_values(idx)
    user_state[c.from_user.id] = {'step': 'edit_price', 'row': idx}
    await c.answer()
    await c.message.reply(f"Новая цена для {row[1]} (было {row[2]}₽):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'edit_price')
async def cmd_edit_price(m: types.Message):
    try:
        newp = float(m.text.strip())
    except ValueError:
        return await m.reply("Неверный формат.")
    st = user_state.pop(m.from_user.id)
    sheet.update_cell(st['row'], 3, newp)
    sheet.update_cell(st['row'], 5, 'FALSE')
    await m.reply("Цена обновлена.", reply_markup=main_kb)

# === Проверка цен ===
async def check_prices():
    rows = sheet.get_all_records()
    for i, r in enumerate(rows, start=2):
        try:
            uid = int(r['UserID'])
            target = float(r['TargetPrice'])
            notified = r.get('Notified') == 'TRUE'
            price, _ = await get_price(r['Artikel'])
            if price is None:
                continue
            sheet.update_cell(i, 4, price)
            if price <= target and not notified:
                await bot.send_message(uid, f"🔔 {r['Artikel']} сейчас {price}₽ — ниже или равна целевой!")
                sheet.update_cell(i, 5, 'TRUE')
            elif price > target and notified:
                sheet.update_cell(i, 5, 'FALSE')
            await asyncio.sleep(0.2)
        except Exception as e:
            logging.warning("check_prices error: %s", e)

# === Рассылка ===
@dp.message_handler(commands=["broadcast"])
async def cmd_broadcast(m: types.Message):
    if m.from_user.id != ADMIN_ID:
        return
    user_state[m.from_user.id] = {'step': 'await_broadcast'}
    await m.reply("Пришлите текст/фото/видео:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get('step') == 'await_broadcast', content_types=types.ContentType.ANY)
async def cmd_broadcast_preview(m: types.Message):
    st = {'step': 'confirm', 'type': None}
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅ Отправить", callback_data="broadcast_ok"),
        InlineKeyboardButton("❌ Отменить", callback_data="broadcast_cancel"),
    )
    if m.text:
        st.update({'type': 'text', 'text': m.text})
        await m.reply(f"Предпросмотр:\n{m.text}", reply_markup=kb)
    elif m.photo:
        st.update({'type': 'photo', 'file_id': m.photo[-1].file_id, 'caption': m.caption or ''})
        await bot.send_photo(m.chat.id, st['file_id'], caption=st['caption'], reply_markup=kb)
    elif m.video:
        st.update({'type': 'video', 'file_id': m.video.file_id, 'caption': m.caption or ''})
        await bot.send_video(m.chat.id, st['file_id'], caption=st['caption'], reply_markup=kb)
    user_state[m.from_user.id] = st

@dp.callback_query_handler(lambda c: c.data in ["broadcast_ok", "broadcast_cancel"])
async def cb_broadcast_confirm(c: types.CallbackQuery):
    st = user_state.pop(c.from_user.id, None)
    if not st:
        return
    if c.data == "broadcast_cancel":
        await c.message.reply("Рассылка отменена.")
        return
    rows = sheet.get_all_records()
    sent = failed = 0
    for r in rows:
        try:
            uid = int(r['UserID'])
            if st['type'] == 'text':
                await bot.send_message(uid, st['text'])
            elif st['type'] == 'photo':
                await bot.send_photo(uid, st['file_id'], caption=st['caption'])
            elif st['type'] == 'video':
                await bot.send_video(uid, st['file_id'], caption=st['caption'])
            sent += 1
        except Exception as e:
            logging.warning("broadcast send to %s failed: %s", r['UserID'], e)
            failed += 1
    await c.message.reply(f"Рассылка завершена.\n✅ Успех: {sent}, ❌ Ошибки: {failed}")

# === Webhook & сервер ===
async def on_startup(app):
    global gc, sheet
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
app.router.add_post(f"/webhook/{TELEGRAM_TOKEN}", lambda req: web.Response(text="OK") if asyncio.create_task(dp.process_update(types.Update(**(await req.json())))) else None)
app.router.add_get("/ping", lambda req: web.Response(text="OK"))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)