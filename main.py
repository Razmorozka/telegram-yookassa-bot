import os
import time
import uuid
import sqlite3
import asyncio
from decimal import Decimal
import requests
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------------- ENV ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")
try:
    GROUP_ID = int(os.getenv("GROUP_ID", "0"))
except:
    GROUP_ID = 0

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

# Контакты
ADMIN_USERNAME = "kairos_007" # Тех. поддержка
EXPERT_USERNAME = "Liya_Sharova" # Лия (Эксперт)
SECRET_WORD = "лапки-лапки"

# ---------------- DB ----------------
DB_FILE = "bot_database.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, email TEXT, step TEXT, last_invoice_id TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS orders (invoice_id TEXT PRIMARY KEY, user_id INTEGER, plan_id TEXT, amount TEXT, status TEXT, payment_id TEXT, created_at INTEGER)")
        conn.commit()

def db_get_all_users():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT user_id FROM users")
        return [row[0] for row in cur.fetchall()]

def db_get_user(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return {"user_id": row[0], "name": row[1], "email": row[2], "step": row[3], "last_invoice_id": row[4]} if row else None

def db_upsert_user(user_id: int, **kwargs):
    current = db_get_user(user_id) or {}
    data = {**current, "user_id": user_id, **kwargs}
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT OR REPLACE INTO users VALUES (:user_id, :name, :email, :step, :last_invoice_id)", data)

def db_create_order(invoice_id, user_id, plan_id, amount, status, payment_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?)", (invoice_id, user_id, plan_id, str(amount), status, payment_id, int(time.time())))

def db_get_order(invoice_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute("SELECT * FROM orders WHERE invoice_id = ?", (invoice_id,))
        row = cur.fetchone()
        return {"invoice_id": row[0], "user_id": row[1], "plan_id": row[2], "amount": row[3], "status": row[4], "payment_id": row[5]} if row else None

def db_update_order_status(invoice_id: str, status: str):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE orders SET status = ? WHERE invoice_id = ?", (status, invoice_id))

# ---------------- Настройки ----------------
PLANS = {
    "test": {"title": "🧪 Тест за 1 ₽", "amount": Decimal("1.00"), "description": "Тестовый доступ"},
    "basic": {"title": "Войти в группу", "amount": Decimal("2400.00"), "description": 'Доступ к материалам "Самодисциплина без стресса"'},
    "pro": {"title": "С сопровождением", "amount": Decimal("5400.00"), "description": 'Материалы + личное сопровождение Лии Шаровой'}
}

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

# ---------------- Клавиатуры ----------------
def kb_main():
    return InlineKeyboardBuilder().button(text="✅ Выбрать пакет", callback_data="choose_plan").button(text="❓ Поддержка", callback_data="support").adjust(1).as_markup()

def kb_plans():
    kb = InlineKeyboardBuilder()
    for pid, p in PLANS.items(): kb.button(text=f"{p['title']} — {p['amount']} ₽", callback_data=f"plan:{pid}")
    return kb.button(text="⬅️ Назад", callback_data="back").adjust(1).as_markup()

def kb_pay(url, inv_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить", url=url)
    kb.button(text="✅ Проверить оплату", callback_data=f"check:{inv_id}")
    kb.button(text="📩 Тех. поддержка", url=f"https://t.me/{ADMIN_USERNAME}")
    return kb.adjust(1).as_markup()

# ---------------- Логика выдачи ----------------
async def issue_link():
    try:
        res = await bot.create_chat_invite_link(chat_id=GROUP_ID, member_limit=1)
        return res.invite_link
    except Exception as e: return f"Ошибка API: {str(e)}"

async def grant_access(inv_id):
    order = db_get_order(inv_id)
    if not order or order["status"] == "paid": return
    db_update_order_status(inv_id, "paid")
    
    user = db_get_user(order["user_id"])
    link = await issue_link()
    name = user.get("name", "Друг")
    plan_id = order.get("plan_id")

    if not link.startswith("https"):
        await bot.send_message(order["user_id"], f"✅ Оплата подтверждена! Заказ `{inv_id}`. Но возникла ошибка ссылки: {link}. Напишите @{ADMIN_USERNAME}")
        return

    # Формируем сообщение для пересылки
    msg = (
        f"Ура, {name}! 🎉 Оплата подтверждена.\n"
        f"🆔 Номер заказа: `{inv_id}`\n\n"
        f"⬇️ **ПЕРЕШЛИТЕ ЭТО СООБЩЕНИЕ РЕБЕНКУ** ⬇️\n\n"
        f"Привет! Твой доступ к курсу готов:\n"
        f"1️⃣ Вступай в закрытую группу: {link}\n"
    )

    # Если PRO или ТЕСТ — добавляем сопровождение
    if plan_id in ["pro", "test"]:
        msg += (
            f"2️⃣ Твой пакет включает **личное сопровождение**.\n"
            f"Напиши эксперту Лие Шаровой: @{EXPERT_USERNAME}\n"
            f"Отправь ей секретное слово: `{SECRET_WORD}`\n"
            f"И свой номер заказа: `{inv_id}`\n"
        )
    
    msg += "\n⚠️ Ссылка одноразовая и действует 24 часа. До встречи!"

    await bot.send_message(order["user_id"], msg)

async def reminder_task(inv_id):
    await asyncio.sleep(3600)
    order = db_get_order(inv_id)
    if order and order["status"] == "pending":
        try: await bot.send_message(order["user_id"], "Заметили, что вы не завершили оплату. 😊\nНужна помощь? Пишите @{ADMIN_USERNAME}")
        except: pass

# ---------------- Handlers ----------------
@dp.message(CommandStart())
async def start(m: Message):
    db_upsert_user(m.from_user.id, step="name")
    await m.answer("Привет! 🙂 Я помогу попасть в закрытую группу.\n\nКак тебя зовут?")

@dp.message(Command("test_link"))
async def test(m: Message):
    await m.answer(f"Тест ссылки: {await issue_link()}")

@dp.message(Command("broadcast"))
async def broadcast(m: Message):
    if m.from_user.username != ADMIN_USERNAME: return
    text = m.text.replace("/broadcast", "").strip()
    if not text: return await m.answer("Введите текст")
    users = db_get_all_users()
    count = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            count += 1
            await asyncio.sleep(0.05)
        except: continue
    await m.answer(f"📢 Рассылка завершена. Получили {count} чел.")

@dp.message()
async def flow(m: Message):
    u = db_get_user(m.from_user.id)
    if not u: return
    if u["step"] == "name":
        db_upsert_user(m.from_user.id, name=m.text, step="email")
        await m.answer(f"Приятно познакомиться, {m.text}! 😊 Теперь укажи email:")
    elif u["step"] == "email":
        if "@" not in m.text: return await m.answer("Введи корректный email")
        db_upsert_user(m.from_user.id, email=m.text, step="done")
        await m.answer(f"Готово! Выбирай пакет:", reply_markup=kb_main())

@dp.callback_query(F.data == "choose_plan")
async def plans_cb(cb: CallbackQuery): await cb.message.edit_text("Пакеты:", reply_markup=kb_plans())

@dp.callback_query(F.data.startswith("plan:"))
async def pay_cb(cb: CallbackQuery):
    pid = cb.data.split(":")[1]
    u = db_get_user(cb.from_user.id)
    inv_id = f"inv_{cb.from_user.id}_{int(time.time())}"
    
    try:
        res = requests.post(
            "https://api.yookassa.ru/v3/payments",
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            headers={"Idempotence-Key": str(uuid.uuid4()), "Content-Type": "application/json"},
            json={
                "amount": {"value": f"{PLANS[pid]['amount']:.2f}", "currency": "RUB"},
                "capture": True,
                "confirmation": {"type": "redirect", "return_url": f"{PUBLIC_BASE_URL}/return/{inv_id}"},
                "description": PLANS[pid]["description"],
                "metadata": {"invoice_id": inv_id},
                "receipt": {"customer": {"email": u["email"]}, "items": [{"description": PLANS[pid]["description"], "quantity": "1.00", "amount": {"value": f"{PLANS[pid]['amount']:.2f}", "currency": "RUB"}, "vat_code": 1}]}
            }
        ).json()
        
        db_create_order(inv_id, cb.from_user.id, pid, PLANS[pid]["amount"], "pending", res["id"])
        asyncio.create_task(reminder_task(inv_id))
        await cb.message.edit_text(f"{u['name']}, К оплате: {PLANS[pid]['amount']} ₽", reply_markup=kb_pay(res["confirmation"]["confirmation_url"], inv_id))
    except Exception as e:
        await cb.answer("Ошибка связи с банком. Попробуйте позже.", show_alert=True)

@dp.callback_query(F.data.startswith("check:"))
async def check_cb(cb: CallbackQuery):
    oid = cb.data.split(":")[1]
    order = db_get_order(oid)
    r = requests.get(f"https://api.yookassa.ru/v3/payments/{order['payment_id']}", auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)).json()
    if r.get("status") == "succeeded": await grant_access(oid)
    else: await cb.answer("Оплата еще не дошла ⏳", show_alert=True)

@dp.callback_query(F.data == "support")
async def supp_cb(cb: CallbackQuery): await cb.message.answer(f"Тех. поддержка: @{ADMIN_USERNAME}")

@dp.callback_query(F.data == "back")
async def back_cb(cb: CallbackQuery): await cb.message.edit_text("Меню:", reply_markup=kb_main())

# ---------------- Webhooks ----------------
@app.post("/telegram/webhook")
async def tg_wh(r: Request):
    await dp.feed_raw_update(bot, await r.json())
    return {"ok": True}

@app.post("/webhook/yookassa")
async def yk_wh(r: Request):
    d = await r.json()
    if d.get("event") == "payment.succeeded":
        inv = d["object"].get("metadata", {}).get("invoice_id")
        if inv: await grant_access(inv)
    return {"ok": True}

@app.on_event("startup")
async def on_startup():
    init_db()
    await bot.set_webhook(f"{PUBLIC_BASE_URL}/telegram/webhook")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
