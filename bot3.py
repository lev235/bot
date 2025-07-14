import os
import logging
import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
import aiohttp
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# === Настройки ===
API_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
GOOGLE_CREDS = "credentials.json"
SPREADSHEET_NAME = "wb_tracker"

logging.basicConfig(level=logging.INFO)

# === Google Sheets ===
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS, scope)
gc = gspread.authorize(creds)
sheet = gc.open(SPREADSHEET_NAME).sheet1

# === Telegram Bot ===
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# === Клавиатура ===
main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("➕ Добавить"), KeyboardButton("📋 Список"))

user_state = {}
admin_state = {}

# === GSpread в asyncio
async def gspread_call(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

# === Получение цены WB
async def get_price(nm):
    nm = str(nm)
    vol = nm[:3]
    part = nm[:5]
    servers = ["basket-01.wb.ru", "basket-02.wb.ru", "basket-03.wb.ru"]
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        for s in servers:
            url = f"https://{s}/vol{vol}/part{part}/info/{nm}.json"
            try:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        p = data.get("price", {})
                        return p.get("priceU", 0) // 100, p.get("salePriceU", p.get("priceU", 0)) // 100
            except Exception:
                continue
    return None, None

# === Хендлеры
@dp.message_handler(commands=["start"])
async def start(msg: types.Message):
    await msg.answer("👋 Я отслеживаю цены на Wildberries!", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "➕ Добавить")
async def add(msg: types.Message):
    user_state[msg.from_user.id] = {"step": "await_article"}
    await msg.answer("Введите артикул:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get("step") == "await_article")
async def get_article(msg: types.Message):
    user_state[msg.from_user.id].update({"artikel": msg.text.strip(), "step": "await_price"})
    await msg.answer("Введите целевую цену:")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get("step") == "await_price")
async def get_target(msg: types.Message):
    try:
        price = float(msg.text.strip())
    except:
        return await msg.answer("Неверный формат цены.")
    data = user_state.pop(msg.from_user.id)
    await gspread_call(sheet.append_row, [msg.from_user.id, data["artikel"], price, "", "FALSE"])
    await msg.answer("✅ Товар добавлен!", reply_markup=main_kb)

@dp.message_handler(lambda m: m.text == "📋 Список")
async def show_items(msg: types.Message):
    rows = await gspread_call(sheet.get_all_records)
    reply, markup = [], InlineKeyboardMarkup(row_width=2)
    for i, row in enumerate(rows, start=2):
        if str(row["UserID"]) == str(msg.from_user.id):
            reply.append(f"{row['Artikel']} → ≤{row['TargetPrice']}₽ (посл.: {row['LastPrice'] or '–'})")
            markup.add(
                InlineKeyboardButton("✏️", callback_data=f"edit_{i}"),
                InlineKeyboardButton("🗑", callback_data=f"del_{i}")
            )
    if reply:
        await msg.answer("\n".join(reply), reply_markup=markup)
    else:
        await msg.answer("Список пуст.")

@dp.callback_query_handler(lambda c: c.data.startswith("del_"))
async def delete_item(c: types.CallbackQuery):
    row = int(c.data.split("_")[1])
    await gspread_call(sheet.delete_rows, row)
    await c.answer("Удалено")
    await c.message.delete()

@dp.callback_query_handler(lambda c: c.data.startswith("edit_"))
async def edit_item(c: types.CallbackQuery):
    idx = int(c.data.split("_")[1])
    row = await gspread_call(sheet.row_values, idx)
    user_state[c.from_user.id] = {"step": "edit_price", "idx": idx, "artikel": row[1]}
    await c.answer()
    await c.message.answer(f"Новая цена для {row[1]} (была {row[2]}):")

@dp.message_handler(lambda m: user_state.get(m.from_user.id, {}).get("step") == "edit_price")
async def apply_edit(msg: types.Message):
    try:
        new_price = float(msg.text.strip())
    except:
        return await msg.answer("Неверно. Введите число.")
    s = user_state.pop(msg.from_user.id)
    await gspread_call(sheet.update_cell, s["idx"], 3, new_price)
    await gspread_call(sheet.update_cell, s["idx"], 5, "FALSE")
    await msg.answer("Обновлено.", reply_markup=main_kb)

# === Рассылка
@dp.message_handler(lambda m: m.from_user.id == ADMIN_ID and m.text == "/broadcast")
async def bc_start(msg: types.Message):
    admin_state[ADMIN_ID] = {"step": "await"}
    await msg.answer("Пришли текст, фото или видео для рассылки:")

@dp.message_handler(lambda m: admin_state.get(ADMIN_ID, {}).get("step") == "await", content_types=types.ContentTypes.ANY)
async def bc_collect(msg: types.Message):
    content = {
        "step": "confirm",
        "type": msg.content_type,
        "text": msg.caption or msg.text,
        "file_id": (
            msg.photo[-1].file_id if msg.photo else
            msg.video.file_id if msg.video else None
        )
    }
    admin_state[ADMIN_ID] = content
    kb = InlineKeyboardMarkup().add(
        InlineKeyboardButton("✅", callback_data="send_bc"),
        InlineKeyboardButton("❌", callback_data="cancel_bc")
    )
    if content["type"] == "photo":
        await msg.answer_photo(content["file_id"], caption=content["text"], reply_markup=kb)
    elif content["type"] == "video":
        await msg.answer_video(content["file_id"], caption=content["text"], reply_markup=kb)
    else:
        await msg.answer(content["text"], reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data in ("send_bc", "cancel_bc"))
async def bc_action(c: types.CallbackQuery):
    await c.answer()
    if c.data == "cancel_bc":
        admin_state.pop(ADMIN_ID, None)
        return await c.message.edit_text("❌ Отменено.")
    rows = await gspread_call(sheet.get_all_values)
    users = set(r[0] for r in rows[1:])
    ok, err = 0, 0
    for uid in users:
        try:
            if admin_state[ADMIN_ID]["type"] == "photo":
                await bot.send_photo(uid, admin_state[ADMIN_ID]["file_id"], caption=admin_state[ADMIN_ID]["text"])
            elif admin_state[ADMIN_ID]["type"] == "video":
                await bot.send_video(uid, admin_state[ADMIN_ID]["file_id"], caption=admin_state[ADMIN_ID]["text"])
            else:
                await bot.send_message(uid, admin_state[ADMIN_ID]["text"])
            ok += 1
        except Exception as e:
            err += 1
    await bot.send_message(ADMIN_ID, f"✅ Отправлено: {ok}\n⚠️ Ошибок: {err}")
    admin_state.pop(ADMIN_ID, None)

# === Проверка цен
async def check_prices():
    try:
        rows = await gspread_call(sheet.get_all_records)
        for i, row in enumerate(rows, start=2):
            uid, art = row["UserID"], row["Artikel"]
            target = float(row["TargetPrice"])
            notified = row["Notified"] == "TRUE"
            price, _ = await get_price(art)
            if price is None:
                continue
            await gspread_call(sheet.update_cell, i, 4, price)
            if price <= target and not notified:
                url = f"https://www.wildberries.ru/catalog/{art}/detail.aspx"
                await bot.send_message(uid, f"🔔 {art} подешевел до {price}₽\n{url}")
                await gspread_call(sheet.update_cell, i, 5, "TRUE")
            elif price > target and notified:
                await gspread_call(sheet.update_cell, i, 5, "FALSE")
        logging.info("✅ check_prices завершена")
    except Exception as e:
        logging.exception("❌ Ошибка в check_prices")

# === Webhook + App ===
app = web.Application()

async def webhook_handler(request):
    try:
        Bot.set_current(bot)  # <== ВАЖНО
        data = await request.json()
        update = types.Update(**data)
        await dp.process_update(update)
    except Exception as e:
        logging.exception("Ошибка webhook")
    return web.Response()

async def ping_handler(request):
    return web.Response(text="pong")

app.router.add_post("/webhook", webhook_handler)
app.router.add_get("/ping", ping_handler)

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_prices, "interval", minutes=1)
    scheduler.start()
    logging.info("🚀 Бот запущен")

async def on_shutdown(app):
    await bot.delete_webhook()
    logging.info("🛑 Webhook удалён")

app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    web.run_app(app, port=port)