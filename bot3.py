import os
import logging
import requests
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiohttp import web
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

API_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SPREADSHEET_NAME = 'wb_tracker'
GOOGLE_CREDS_JSON = 'credentials.json'

# Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_JSON, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SPREADSHEET_NAME).sheet1

# Telegram Bot
bot = Bot(token=API_TOKEN)
Bot.set_current(bot)
dp = Dispatcher(bot)

main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

user_state = {}
admin_state = {}

def get_price(nm):
    nm_clean = str(nm).strip()
    if not nm_clean.isdigit():
        logging.warning(f"[WB] Некорректный nm: '{nm}'")
        return None, None
    url = f'https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&spp=0&nm={nm_clean}'
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            logging.error(f"[WB] Статус !=200 ({resp.status_code}), nm={nm_clean}")
            return None, None
        text = resp.text.strip()
        if not text:
            logging.error(f"[WB] Пустой ответ nm={nm_clean}")
            return None, None
        try:
            data = resp.json()
        except json.JSONDecodeError:
            logging.error(f"[WB] Невалидный JSON nm={nm_clean}, ответ={text[:200]}")
            return None, None
        products = data.get('data', {}).get('products')
        if not products:
            logging.warning(f"[WB] Нет products nm={nm_clean}")
            return None, None
        item = products[0]
        return item.get('priceU', 0)//100, item.get('salePriceU', item.get('priceU', 0))//100
    except Exception as e:
        logging.exception(f"[WB] Ошибка запроса nm={nm_clean}: {e}")
        return None, None

# --- handlers same as before for add/list/edit ...

@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    await msg.answer("Привет! Я отслеживаю цену товара на Wildberries.", reply_markup=main_kb)

# ... (➕ Добавить, ввод артикула и цены, Список, edit/delete) omitted for brevity but identical to your version

# Admin broadcast (same structure)

@dp.message_handler(lambda m: m.from_user.id == ADMIN_ID and m.text == "/broadcast")
async def admin_broadcast_start(message: types.Message):
    admin_state[ADMIN_ID] = {'step': 'await_content'}
    await message.answer("Отправьте текст, фото или видео для рассылки:")

@dp.message_handler(lambda m: admin_state.get(ADMIN_ID, {}).get('step') == 'await_content', content_types=types.ContentTypes.ANY)
async def admin_collect_content(message: types.Message):
    admin_state[ADMIN_ID] = {
        'step': 'confirm',
        'content_type': message.content_type,
        'text': message.caption or message.text or "",
        'file_id': (
            (message.photo[-1].file_id if message.photo else
             message.video.file_id if message.video else None)
        )
    }
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Отправить", callback_data="send_broadcast"),
        InlineKeyboardButton("❌ Отменить", callback_data="cancel_broadcast"),
        InlineKeyboardButton("✏️ Изменить", callback_data="edit_broadcast")
    )
    if message.content_type == "photo":
        await message.answer_photo(photo=admin_state[ADMIN_ID]['file_id'],
                                   caption=admin_state[ADMIN_ID]['text'],
                                   reply_markup=markup)
    elif message.content_type == "video":
        await message.answer_video(video=admin_state[ADMIN_ID]['file_id'],
                                   caption=admin_state[ADMIN_ID]['text'],
                                   reply_markup=markup)
    else:
        await message.answer(admin_state[ADMIN_ID]['text'], reply_markup=markup)

@dp.callback_query_handler(lambda c: c.data in ["send_broadcast", "cancel_broadcast", "edit_broadcast"])
async def handle_broadcast_actions(c: types.CallbackQuery):
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
        success = fail = 0
        st = admin_state[ADMIN_ID]
        for uid in users:
            try:
                if st['content_type'] == 'photo':
                    await bot.send_photo(uid, st['file_id'], caption=st['text'])
                elif st['content_type'] == 'video':
                    await bot.send_video(uid, st['file_id'], caption=st['text'])
                else:
                    await bot.send_message(uid, st['text'])
                success += 1
            except Exception:
                fail += 1
        admin_state.pop(ADMIN_ID, None)
        try:
            await c.message.edit_text(f"✅ Рассылка завершена.\nУспешно: {success}\nОшибки: {fail}")
        except Exception:
            await bot.send_message(ADMIN_ID, f"✅ Рассылка завершена.\nУспешно: {success}\nОшибки: {fail}")

# Price checking job

async def check_prices():
    rows = sheet.get_all_records()
    for i, row in enumerate(rows, start=2):
        uid = int(row['UserID'])
        nm = row['Artikel']
        target = float(row['TargetPrice'])
        notified = row.get('Notified') == 'TRUE'
        price, sale = get_price(nm)
        if price is None:
            continue
        sheet.update_cell(i, 4, price)
        if price <= target and not notified:
            try:
                await bot.send_message(uid, f"🔔 {nm} подешевел до {price}₽!\nhttps://www.wildberries.ru/catalog/{nm}/detail.aspx")
                sheet.update_cell(i, 5, 'TRUE')
            except Exception as e:
                logging.error(f"Ошибка уведомления {nm} -> {uid}: {e}")
        elif price > target and notified:
            sheet.update_cell(i, 5, 'FALSE')

# Webhook setup

app = web.Application()

async def webhook_handler(request):
    data = await request.json()
    upd = types.Update(**data)
    await dp.process_update(upd)
    return web.Response()

async def ping(request): return web.Response(text="ok")

app.router.add_post("/webhook", webhook_handler)
app.router.add_get("/ping", ping)

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    sched = AsyncIOScheduler()
    sched.add_job(check_prices, "interval", minutes=1)
    sched.start()
    logging.info("Webhook установлен")

async def on_shutdown(app):
    await bot.delete_webhook()
    logging.info("Webhook удалён")

app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv("PORT", "8080"))
    web.run_app(app, port=port)